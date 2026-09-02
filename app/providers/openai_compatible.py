"""Provider OpenAI-compatible.

Uma classe cobre três requisitos do desenho: OpenAI, modelos locais
(Ollama, vLLM, LM Studio) e "qualquer endpoint compatível" — tudo é
/v1/chat/completions com base_url diferente.

Saída estruturada: tenta response_format json_schema (strict); se o
endpoint não suportar (comum em locais), degrada para json_object +
instrução no system — a validação Pydantic na base pega qualquer desvio.

Prompt caching: com `supports_prompt_caching`, o system vira lista de blocos
com `cache_control` (formato aceito pelo OpenRouter para modelos Anthropic;
modelos OpenAI cacheiam prefixo automaticamente e ignoram a marca). Sem
suporte, prefixo e system são concatenados em texto puro. Tokens cacheados
chegam em `usage.prompt_tokens_details.cached_tokens` e já estão contidos em
`prompt_tokens` — por isso são subtraídos de input_tokens antes de precificar.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    NonRetryableProviderError,
    RetryableProviderError,
    StructuredOutputError,
    Usage,
)

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
_CACHE_CONTROL = {"type": "ephemeral"}


def build_system_message(
    system: str, cache_prefix: str | None, *, supports_prompt_caching: bool
) -> dict[str, Any] | None:
    if not cache_prefix:
        return {"role": "system", "content": system} if system else None
    if not supports_prompt_caching:
        joined = f"{cache_prefix}\n\n{system}".strip()
        return {"role": "system", "content": joined}
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": cache_prefix, "cache_control": _CACHE_CONTROL}
    ]
    if system:
        blocks.append({"type": "text", "text": system, "cache_control": _CACHE_CONTROL})
    return {"role": "system", "content": blocks}


def parse_usage(raw_usage: dict[str, Any]) -> Usage:
    prompt_tokens = int(raw_usage.get("prompt_tokens", 0) or 0)
    details = raw_usage.get("prompt_tokens_details") or {}
    cached = (
        int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
    )
    cached = min(cached, prompt_tokens)
    return Usage(
        input_tokens=prompt_tokens - cached,
        output_tokens=int(raw_usage.get("completion_tokens", 0) or 0),
        cache_read_tokens=cached,
    )


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normaliza o schema Pydantic para o contrato strict OpenAI/OpenRouter."""
    normalized = dict(schema)
    properties = normalized.get("properties")
    if isinstance(properties, dict):
        normalized["additionalProperties"] = False
        normalized["required"] = list(properties)
        normalized["properties"] = {
            key: _strict_json_schema(value) if isinstance(value, dict) else value
            for key, value in properties.items()
        }
    for key in ("$defs", "definitions"):
        definitions = normalized.get(key)
        if isinstance(definitions, dict):
            normalized[key] = {
                name: _strict_json_schema(value) if isinstance(value, dict) else value
                for name, value in definitions.items()
            }
    items = normalized.get("items")
    if isinstance(items, dict):
        normalized["items"] = _strict_json_schema(items)
    for key in ("anyOf", "oneOf", "allOf"):
        variants = normalized.get(key)
        if isinstance(variants, list):
            normalized[key] = [
                _strict_json_schema(value) if isinstance(value, dict) else value
                for value in variants
            ]
    return normalized


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        *args: Any,
        base_url: str,
        api_key: str | None = None,
        provider_name: str = "openai_compatible",
        supports_json_schema: bool = True,
        require_parameters: bool = False,
        response_healing: bool = False,
        supports_prompt_caching: bool = False,
        extra_headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.name = provider_name
        self._supports_json_schema = supports_json_schema
        self._require_parameters = require_parameters
        self._response_healing = response_healing
        self._supports_prompt_caching = supports_prompt_caching
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)
        self._client = client or httpx.AsyncClient(base_url=base_url, headers=headers)

    async def _do_complete(self, request: CompletionRequest) -> CompletionResult:
        messages: list[dict[str, Any]] = []
        system = request.system or ""

        if request.response_schema is not None and not self._supports_json_schema:
            system = (
                f"{system}\n\nResponda SOMENTE com JSON válido no schema:\n"
                f"{json.dumps(request.response_schema.model_json_schema())}"
            ).strip()

        system_message = build_system_message(
            system,
            request.cache_prefix,
            supports_prompt_caching=self._supports_prompt_caching,
        )
        if system_message is not None:
            messages.append(system_message)
        messages.extend(m.model_dump() for m in request.messages)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.response_schema is not None:
            if self._supports_json_schema:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.response_schema.__name__,
                        "schema": _strict_json_schema(
                            request.response_schema.model_json_schema()
                        ),
                        "strict": True,
                    },
                }
            else:
                payload["response_format"] = {"type": "json_object"}
            if self._require_parameters:
                payload["provider"] = {"require_parameters": True}
            if self._response_healing:
                payload["plugins"] = [{"id": "response-healing"}]

        try:
            response = await self._client.post("/v1/chat/completions", json=payload)
        except httpx.TransportError as exc:
            raise RetryableProviderError(str(exc), self.name) from exc

        if response.status_code in _RETRYABLE_STATUS:
            raise RetryableProviderError(
                response.text[:500], self.name, response.status_code
            )
        if response.status_code >= 400:
            raise NonRetryableProviderError(
                response.text[:500], self.name, response.status_code
            )

        data = response.json()
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        usage = parse_usage(data.get("usage") or {})

        parsed: dict[str, Any] | None = None
        if request.response_schema is not None:
            try:
                parsed = self._validate_structured(
                    json.loads(text), request.response_schema, self.name
                )
            except json.JSONDecodeError as exc:
                raise StructuredOutputError(
                    f"Resposta não é JSON: {text[:200]}", provider=self.name
                ) from exc

        return CompletionResult(
            text=text,
            parsed=parsed,
            model=data.get("model", request.model),
            provider=self.name,
            usage=usage,
            cost_usd=self._cost_for(request.model, usage),
            latency_ms=0.0,
            stop_reason=choice.get("finish_reason"),
        )
