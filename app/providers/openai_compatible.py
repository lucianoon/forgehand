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
    Message,
    NonRetryableProviderError,
    RetryableProviderError,
    StructuredOutputError,
    ToolCall,
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


_STRUCTURED_TOOL_NAME = "emit_structured_output"


def build_tool_payload(request: CompletionRequest, *, strict: bool) -> dict[str, Any]:
    """Com ferramentas de exploração, a saída estruturada também vira uma
    function (a primeira da lista), em vez de response_format: o modelo escolhe
    entre explorar e responder, e `tool_choice=required` impede texto solto.
    force_final aponta direto para a função de saída."""
    tools: list[dict[str, Any]] = []
    if request.response_schema is not None:
        schema = request.response_schema.model_json_schema()
        function: dict[str, Any] = {
            "name": _STRUCTURED_TOOL_NAME,
            "description": "Emita o resultado final exatamente neste schema.",
            "parameters": _strict_json_schema(schema) if strict else schema,
        }
        if strict:
            function["strict"] = True
        tools.append({"type": "function", "function": function})
    for spec in request.tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_schema,
                },
            }
        )
    payload: dict[str, Any] = {"tools": tools}
    if request.response_schema is not None and request.force_final:
        payload["tool_choice"] = {
            "type": "function",
            "function": {"name": _STRUCTURED_TOOL_NAME},
        }
    elif request.response_schema is not None:
        payload["tool_choice"] = "required"
    else:
        payload["tool_choice"] = "auto"
    return payload


def build_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": message.content or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )
        elif message.tool_results:
            for result in message.tool_results:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.tool_call_id,
                        "content": result.content,
                    }
                )
            if message.content:
                out.append({"role": "user", "content": message.content})
        else:
            out.append({"role": message.role, "content": message.content})
    return out


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
        use_tools = bool(request.tools)

        if (
            request.response_schema is not None
            and not self._supports_json_schema
            and not use_tools
        ):
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
        messages.extend(build_messages(request.messages))

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if use_tools:
            payload.update(
                build_tool_payload(request, strict=self._supports_json_schema)
            )
        elif request.response_schema is not None:
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
        message = choice["message"]
        text = message.get("content") or ""
        usage = parse_usage(data.get("usage") or {})

        parsed, tool_calls = self._parse_tool_calls(message.get("tool_calls"), request)
        if request.response_schema is not None and parsed is None and not tool_calls:
            # Sem function call: o JSON vem no content (response_format ou
            # modelo que ignorou tool_choice=required).
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
            tool_calls=tool_calls,
            model=data.get("model", request.model),
            provider=self.name,
            usage=usage,
            cost_usd=self._cost_for(request.model, usage),
            latency_ms=0.0,
            stop_reason=choice.get("finish_reason"),
        )

    def _parse_tool_calls(
        self, raw_calls: Any, request: CompletionRequest
    ) -> tuple[dict[str, Any] | None, list[ToolCall]]:
        parsed: dict[str, Any] | None = None
        tool_calls: list[ToolCall] = []
        if not isinstance(raw_calls, list):
            return parsed, tool_calls
        for index, raw in enumerate(raw_calls):
            if not isinstance(raw, dict):
                continue
            function = raw.get("function") or {}
            name = str(function.get("name", ""))
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else dict(raw_arguments)
                )
            except json.JSONDecodeError as exc:
                raise StructuredOutputError(
                    f"Argumentos da function {name} não são JSON: "
                    f"{str(raw_arguments)[:200]}",
                    provider=self.name,
                ) from exc
            if not isinstance(arguments, dict):
                raise StructuredOutputError(
                    f"Argumentos da function {name} não são um objeto.",
                    provider=self.name,
                )
            if name == _STRUCTURED_TOOL_NAME and request.response_schema is not None:
                parsed = self._validate_structured(
                    arguments, request.response_schema, self.name
                )
            else:
                tool_calls.append(
                    ToolCall(
                        id=str(raw.get("id") or f"call_{index}"),
                        name=name,
                        arguments=arguments,
                    )
                )
        return parsed, tool_calls
