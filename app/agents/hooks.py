"""Operator-owned, declarative tool lifecycle policy. Never executes user code."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Iterator, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.infrastructure.audit import AuditEvent, build_audit_event

HookEvent = Literal["pre_tool", "post_tool", "tool_error"]
HookAction = Literal["audit", "deny", "suppress"]


class ToolHookRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    event: HookEvent
    action: HookAction = "audit"
    tool: str = Field(default="*", min_length=1, max_length=100)
    agent: str = Field(default="*", min_length=1, max_length=100)
    output_exceeds_chars: int | None = Field(default=None, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.action == "deny" and self.event != "pre_tool":
            raise ValueError("deny requires pre_tool")
        if self.action == "suppress" and self.event != "post_tool":
            raise ValueError("suppress requires post_tool")
        if self.output_exceeds_chars is not None and self.event != "post_tool":
            raise ValueError("output_exceeds_chars requires post_tool")
        return self


def parse_tool_hooks(raw: str) -> tuple[ToolHookRule, ...]:
    if len(raw) > 65_536:
        raise ValueError("TOOL_HOOKS_JSON exceeds 65536 characters")
    rules = TypeAdapter(list[ToolHookRule]).validate_json(raw)
    if len(rules) > 64:
        raise ValueError("TOOL_HOOKS_JSON exceeds 64 rules")
    if len({rule.id for rule in rules}) != len(rules):
        raise ValueError("Tool hook IDs must be unique")
    return tuple(rules)


@dataclass(frozen=True)
class HookScope:
    workflow_id: str | None = None
    project_id: str | None = None
    client_id: str | None = None


_scope: ContextVar[HookScope] = ContextVar("tool_hook_scope", default=HookScope())


@contextmanager
def tool_hook_scope(scope: HookScope) -> Iterator[None]:
    """Bind only trusted service metadata; reset on success, failure or cancellation."""
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


class HookAuditSink(Protocol):
    async def record(self, event: AuditEvent) -> None: ...


class ToolHookFailure(RuntimeError):
    """Safe failure body: no sink exception, tool arguments or output."""


@dataclass(frozen=True)
class ToolHookCall:
    run_id: str
    ordinal: int
    tool: str
    agent: str
    task_id: str | None = None


class ToolHookDispatcher:
    def __init__(
        self,
        rules: tuple[ToolHookRule, ...],
        audit: HookAuditSink,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        if not 0 < timeout_seconds <= 10:
            raise ValueError("Hook audit timeout must be between 0 and 10 seconds")
        self._rules = rules
        self._audit = audit
        self._timeout = timeout_seconds

    async def dispatch(
        self,
        event: HookEvent,
        call: ToolHookCall,
        *,
        output_chars: int = 0,
        outcome: str = "allowed",
    ) -> bool:
        """Return whether delivery/execution may continue; restrictive rules win."""
        if not self._rules:
            return True
        try:
            matches = [
                rule
                for rule in self._rules
                if rule.event == event
                and fnmatchcase(call.tool, rule.tool)
                and fnmatchcase(call.agent, rule.agent)
                and (
                    rule.output_exceeds_chars is None
                    or output_chars > rule.output_exceeds_chars
                )
            ]
            allowed = all(rule.action == "audit" for rule in matches)
            if not allowed:
                outcome = "denied" if event == "pre_tool" else "suppressed"
            scope = _scope.get()
            record = build_audit_event(
                action=f"tool.{event}",
                outcome=outcome,
                workflow_id=scope.workflow_id,
                project_id=scope.project_id,
                client_id=scope.client_id,
                detail=json.dumps(
                    {
                        "run_id": call.run_id,
                        "ordinal": call.ordinal,
                        "tool": call.tool,
                        "agent": call.agent,
                        "task_id": call.task_id,
                        "rules": [rule.id for rule in matches],
                    },
                    separators=(",", ":"),
                ),
            )
            await asyncio.wait_for(self._audit.record(record), timeout=self._timeout)
            return allowed
        except Exception:
            # CancelledError inherits BaseException and must propagate unchanged.
            raise ToolHookFailure(
                "Tool hook failed; execution stopped safely."
            ) from None
