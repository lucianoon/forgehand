"""Provider Anthropic.

Saída estruturada (regra 2): tool use forçado com o JSON Schema do modelo
Pydantic. Mais confiável que pedir JSON no prompt — o modelo é obrigado a
preencher o schema, e ainda validamos com Pydantic antes de devolver.

Prompt caching: `cache_prefix` vira o primeiro bloco do system com
`cache_control: ephemeral`; `system` vira um segundo bloco, também marcado.
Dois breakpoints de propósito — o prefixo (grounding) é compartilhado entre
planner, executor e judge do mesmo workflow; o system é por papel. A API só
cacheia blocos acima do mínimo do modelo (1024/2048 tokens); abaixo disso a
marca é ignorada sem custo.

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
    Message,
    NonRetryableProviderError,
    RetryableProviderError,
    StructuredOutputError,
    ToolCall,
    Usage,
)

# IDs de referência (jul/2026) — a escolha real vem do registry/settings
MODEL_FAST = "claude-haiku-4-5"
MODEL_STANDARD = "claude-sonnet-5"
MODEL_STRONG = "claude-opus-5"

_STRUCTURED_TOOL_NAME = "emit_structured_output"
_CACHE_CONTROL = {"type": "ephemeral"}


def build_tools_and_choice(
    request: CompletionRequest,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Ferramenta de saída estruturada PRIMEIRO (é a resposta final), depois
    as de exploração. tool_choice: forçada quando só há saída estruturada ou
    force_final; `any` quando o modelo pode explorar antes de responder."""
    tools: list[dict[str, Any]] = []
    if request.response_schema is not None:
        tools.append(
            {
                "name": _STRUCTURED_TOOL_NAME,
                "description": "Emita o resultado final exatamente neste schema.",
                "input_schema": request.response_schema.model_json_schema(),
            }
        )
    for spec in request.tools:
        tools.append(
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
        )
    if not tools:
        return [], None
    if request.response_schema is not None and (
        not request.tools or request.force_final
    ):
        return tools, {"type": "tool", "name": _STRUCTURED_TOOL_NAME}
    if request.response_schema is not None:
        return tools, {"type": "any"}
    return tools, {"type": "auto"}


def build_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Mensagens simples viram string (formato original); mensagens com
    tool_calls/tool_results viram blocos."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
        elif message.tool_results:
            blocks = []
            for result in message.tool_results:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": result.tool_call_id,
                    "content": result.content,
                }
                if result.is_error:
                    block["is_error"] = True
                blocks.append(block)
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            out.append({"role": "user", "content": blocks})
        else:
            out.append({"role": message.role, "content": message.content})
    return out


def build_system_blocks(request: CompletionRequest) -> str | list[dict[str, Any]]:
    """Sem prefixo: string simples (comportamento original). Com prefixo:
    lista de blocos, cada um com breakpoint de cache."""
    if not request.cache_prefix:
        return request.system or ""
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": request.cache_prefix, "cache_control": _CACHE_CONTROL}
    ]
    if request.system:
        blocks.append(
            {"type": "text", "text": request.system, "cache_control": _CACHE_CONTROL}
        )
    return blocks


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
            "messages": build_messages(request.messages),
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        system = build_system_blocks(request)
        if system:
            kwargs["system"] = system

        tools, tool_choice = build_tools_and_choice(request)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

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
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                if (
                    block.name == _STRUCTURED_TOOL_NAME
                    and request.response_schema is not None
                ):
                    parsed = self._validate_structured(
                        dict(block.input),
                        request.response_schema,
                        self.name,
                    )
                else:
                    tool_calls.append(
                        ToolCall(
                            id=block.id, name=block.name, arguments=dict(block.input)
                        )
                    )

        if request.response_schema is not None and parsed is None and not tool_calls:
            raise StructuredOutputError(
                "Schema exigido mas o modelo não emitiu tool_use.",
                provider=self.name,
            )

        return CompletionResult(
            text="\n".join(text_parts),
            parsed=parsed,
            tool_calls=tool_calls,
            model=response.model,
            provider=self.name,
            usage=usage,
            cost_usd=self._cost_for(request.model, usage),
            latency_ms=0.0,  # preenchido pela base
            stop_reason=response.stop_reason,
        )
