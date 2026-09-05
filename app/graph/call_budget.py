"""Persist provider-boundary metering for each checkpointed graph step."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.graph.contracts import ExecutionPayload
from app.graph.state import WorkflowState
from app.infrastructure.llm_budget import (
    BudgetAdmissionError,
    CallBudget,
    call_budget_scope,
)
from app.models.task import TaskStatus


def with_call_budget(
    node: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def bounded(state: WorkflowState | ExecutionPayload) -> dict[str, Any]:
        if isinstance(state, ExecutionPayload):
            # Missing allowances indicate an old queued Send. Fail closed for
            # provider calls rather than guessing the global remaining budget.
            task_budget = state.task.budget
            max_tokens = min(
                state.token_allowance or 0,
                task_budget.max_tokens
                - task_budget.consumed_tokens
                - task_budget.unconfirmed_tokens,
            )
            max_cost = min(
                state.cost_allowance_usd or 0.0,
                task_budget.max_cost_usd
                - task_budget.consumed_cost_usd
                - task_budget.unconfirmed_cost_usd,
            )
        else:
            max_tokens = (
                state.budget.max_tokens
                - int(state.usage.get("tokens", 0))
                - int(state.usage.get("unconfirmed_tokens", 0))
            )
            max_cost = (
                state.budget.max_cost_usd
                - state.usage.get("cost_usd", 0.0)
                - state.usage.get("unconfirmed_cost_usd", 0.0)
            )
        budget = CallBudget(
            max_tokens=max(0, max_tokens), max_cost_usd=max(0.0, max_cost)
        )
        with call_budget_scope(budget):
            try:
                update = await node(state)
            except BudgetAdmissionError as exc:
                update = {"error": str(exc)}
            except Exception as exc:
                if not any(budget.usage().values()):
                    raise
                # The call was paid or may have been paid. Persist that evidence
                # and stop at the gate instead of discarding it with the exception.
                budget.blocked_reason = f"llm_call_failed: {type(exc).__name__}: {exc}"
                update = {"error": budget.blocked_reason}
        # Agents may report their aggregate on success; the provider meter also
        # covers successful calls before an exception and internal retry attempts.
        # Choose each measured total once, never sum both reporting paths.
        reported = update.get("usage", {})
        measured = budget.usage()
        if any(measured.values()):
            update["usage"] = {
                key: max(reported.get(key, 0), measured.get(key, 0))
                for key in reported.keys() | measured.keys()
            }
        if budget.blocked_reason is not None:
            update["budget_blocked_reason"] = budget.blocked_reason
        if isinstance(state, ExecutionPayload):
            charged = state.task.budget.charge(
                budget.tokens,
                budget.cost_usd,
                unconfirmed_tokens=budget.unconfirmed_tokens,
                unconfirmed_cost_usd=budget.unconfirmed_cost_usd,
            )
            tasks = update.get("plan", [])
            for index, task in enumerate(tasks):
                if task.id != state.task.id:
                    continue
                counts = {
                    key: max(getattr(task.budget, key), getattr(charged, key))
                    for key in (
                        "consumed_tokens",
                        "consumed_cost_usd",
                        "unconfirmed_tokens",
                        "unconfirmed_cost_usd",
                    )
                }
                changes: dict[str, Any] = {
                    "budget": task.budget.model_copy(update=counts)
                }
                if budget.blocked_reason is not None:
                    changes["status"] = TaskStatus.ESCALATED
                tasks[index] = task.model_copy(update=changes)
        return update

    return bounded
