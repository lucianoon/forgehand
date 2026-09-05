"""Call admission and metering, independent of providers and graph implementations.

A reservation is a conservative estimate, not a vendor billing guarantee. Failed
or cancelled calls with unknown usage retain their estimate separately from
measured usage, so a retry cannot silently reuse possibly spent allowance.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator


class BudgetAdmissionError(Exception):
    """The next complete request cannot be covered by the remaining allowance."""


@dataclass
class CallBudget:
    max_tokens: int
    max_cost_usd: float
    tokens: int = 0
    cost_usd: float = 0.0
    unconfirmed_tokens: int = 0
    unconfirmed_cost_usd: float = 0.0
    reserved_tokens: int = 0
    reserved_cost_usd: float = 0.0
    blocked_reason: str | None = None
    parent: CallBudget | None = None

    def reserve(self, tokens: int, cost_usd: float) -> Reservation:
        if self.blocked_reason is not None:
            raise BudgetAdmissionError(self.blocked_reason)
        available_tokens = (
            self.max_tokens
            - self.tokens
            - self.unconfirmed_tokens
            - self.reserved_tokens
        )
        available_cost = (
            self.max_cost_usd
            - self.cost_usd
            - self.unconfirmed_cost_usd
            - self.reserved_cost_usd
        )
        if tokens > available_tokens or cost_usd > available_cost:
            self.blocked_reason = (
                "llm_budget_admission: próximo pedido exige reserva de "
                f"{tokens} tokens / US$ {cost_usd:.6f}; saldo disponível "
                f"{max(0, available_tokens)} tokens / US$ {max(0.0, available_cost):.6f}"
            )
            raise BudgetAdmissionError(self.blocked_reason)
        # No await between check and reservation: concurrent calls in this
        # event loop observe the same outstanding reservations.
        parent_reservation = (
            self.parent.reserve(tokens, cost_usd) if self.parent else None
        )
        self.reserved_tokens += tokens
        self.reserved_cost_usd += cost_usd
        return Reservation(self, tokens, cost_usd, parent_reservation)

    def usage(self) -> dict[str, float]:
        usage: dict[str, float] = {"tokens": self.tokens, "cost_usd": self.cost_usd}
        if self.unconfirmed_tokens or self.unconfirmed_cost_usd:
            usage.update(
                unconfirmed_tokens=self.unconfirmed_tokens,
                unconfirmed_cost_usd=self.unconfirmed_cost_usd,
            )
        return usage


@dataclass
class Reservation:
    budget: CallBudget
    tokens: int
    cost_usd: float
    parent: Reservation | None = None

    def release(self) -> None:
        self.budget.reserved_tokens -= self.tokens
        self.budget.reserved_cost_usd -= self.cost_usd
        if self.parent is not None:
            self.parent.release()

    def settle(self, tokens: int | None = None, cost_usd: float | None = None) -> None:
        self.budget.reserved_tokens -= self.tokens
        self.budget.reserved_cost_usd -= self.cost_usd
        if self.parent is not None:
            self.parent.settle(tokens, cost_usd)
        if tokens is None or cost_usd is None:
            self.budget.unconfirmed_tokens += self.tokens
            self.budget.unconfirmed_cost_usd += self.cost_usd
        else:
            self.budget.tokens += tokens
            self.budget.cost_usd += cost_usd
            if tokens > self.tokens or cost_usd > self.cost_usd:
                self.budget.blocked_reason = (
                    "llm_budget_estimate_exceeded: consumo medido excedeu a reserva"
                )


_active: ContextVar[CallBudget | None] = ContextVar("llm_call_budget", default=None)


def active_call_budget() -> CallBudget | None:
    return _active.get()


@contextmanager
def call_budget_scope(budget: CallBudget) -> Iterator[CallBudget]:
    parent = _active.get()
    if parent is not None and parent is not budget:
        budget.parent = parent
    token = _active.set(budget)
    try:
        yield budget
    finally:
        if budget.blocked_reason is not None and budget.parent is not None:
            budget.parent.blocked_reason = budget.blocked_reason
        _active.reset(token)
