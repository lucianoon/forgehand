"""Provider Anthropic.

Saída estruturada (regra 2): tool use forçado com o JSON Schema do modelo
Pydantic. Mais confiável que pedir JSON no prompt — o modelo é obrigado a
preencher o schema, e ainda validamos com Pydantic antes de devolver.

Modelos e preços: verificar sempre contra a documentação oficial
(https://platform.claude.com/docs/en/about-claude/models/overview e a página
de preços). Os IDs abaixo são os vigentes em jul/2026; a tabela de preços
vem de settings, nunca daqui.
"""

from __future__ import annotations

from typing import Any

import anthropic

from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    NonRetryableProviderError,
    RetryableProviderError,
    StructuredOutputError,
    Usage,
)

# IDs de referência (jul/2026) — a escolha real vem do registry/settings
MODEL_FAST = "claude-haiku-4-5"
MODEL_STANDARD = "claude-sonnet-4-6"
MODEL_STRONG = "claude-opus-4-8"

_STRUCTURED_TOOL_NAME = "emit_structured_output"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self, *args: Any, client: anthropic.AsyncAnthropic | None = None, **kwargs: Any
    ):
        super().__init__(*args, **kwargs)
        # SDK lê ANTHROPIC_API_KEY do ambiente; retry do SDK desligado —
        # o retry é responsabilidade da base (um único lugar, um único log)
        self._client = client or anthropic.AsyncAnthropic(max_retries=0)

    async def _do_complete(self, request: CompletionRequest) -> CompletionResult:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [m.model_dump() for m in request.messages],
        }
        if request.system:
            kwargs["system"] = request.system

        if request.response_schema is not None:
            kwargs["tools"] = [
                {
                    "name": _STRUCTURED_TOOL_NAME,
                    "description": ("Emita o resultado final exatamente neste schema."),
                    "input_schema": request.response_schema.model_json_schema(),
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": _STRUCTURED_TOOL_NAME}

        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise RetryableProviderError(str(exc), self.name, 429) from exc
        except anthropic.InternalServerError as exc:
            raise RetryableProviderError(str(exc), self.name, 500) from exc
        except anthropic.APIConnectionError as exc:
            raise RetryableProviderError(str(exc), self.name) from exc
        except anthropic.APIStatusError as exc:
            raise NonRetryableProviderError(
                str(exc), self.name, exc.status_code
            ) from exc

        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0)
            or 0,
            cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0)
            or 0,
        )

        text_parts: list[str] = []
        parsed: dict[str, Any] | None = None
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use" and block.name == _STRUCTURED_TOOL_NAME:
                if request.response_schema is None:
                    continue
                parsed = self._validate_structured(
                    dict(block.input),
                    request.response_schema,
                    self.name,
                )

        if request.response_schema is not None and parsed is None:
            raise StructuredOutputError(
                "Schema exigido mas o modelo não emitiu tool_use.",
                provider=self.name,
            )

        return CompletionResult(
            text="\n".join(text_parts),
            parsed=parsed,
            model=response.model,
            provider=self.name,
            usage=usage,
            cost_usd=self._cost_for(request.model, usage),
            latency_ms=0.0,  # preenchido pela base
            stop_reason=response.stop_reason,
        )
