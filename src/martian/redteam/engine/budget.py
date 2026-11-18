"""The budget ledger: what a run, each probe and each attempt spent, against the run file's ``budget``.

Every call of the target, the attacker and the judge is reserved before it is made
(:meth:`Ledger.reserve`). A call that would go past a limit is not made: the ledger raises
:class:`BudgetExhausted`, and from then on every reservation fails with the same limit, so the run
stops cleanly and each attempt not yet decided is reported as stopped.

The count limits (``target_queries``, ``model_calls``) are exact: a run never makes more calls than
they allow. ``tokens`` and ``cost_usd`` are known only after a call returns, so they are checked
before each model call against what was spent so far: the last call may cross them, and no call is
made after. ``max_seconds`` is checked before every call.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

from martian.redteam.model import BudgetLimit, Spend
from martian.redteam.runfile import Budget
from martian_redteam.types import ModelUsage

CallKind = Literal["target", "attacker", "judge"]


class BudgetExhausted(Exception):
    """A budget limit was reached; ``limit`` names it."""

    def __init__(self, limit: BudgetLimit) -> None:
        super().__init__(f"the budget's {limit} ran out")
        self.limit: BudgetLimit = limit


class Ledger:
    """Spending of one run against ``budget``. Not thread-safe: the engine reserves on its event loop,
    where nothing runs between the check and the count."""

    def __init__(self, budget: Budget, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget
        self._clock = clock
        self._started = clock()
        self.spent = Spend()
        self.stopped_by: BudgetLimit | None = None
        self.by_probe: dict[str, Spend] = {}

    def _exceeded(self, kind: CallKind) -> BudgetLimit | None:
        budget, spent = self.budget, self.spent
        if budget.max_seconds is not None and self._clock() - self._started >= budget.max_seconds:
            return "max_seconds"
        if kind == "target":
            if budget.target_queries is not None and spent.target_queries + 1 > budget.target_queries:
                return "target_queries"
            return None
        if budget.model_calls is not None and spent.model_calls + 1 > budget.model_calls:
            return "model_calls"
        if budget.tokens is not None and spent.tokens >= budget.tokens:
            return "tokens"
        if budget.cost_usd is not None and spent.cost_usd >= budget.cost_usd:
            return "cost_usd"
        return None

    def check(self) -> None:
        """Raise when the run has already stopped."""
        if self.stopped_by is not None:
            raise BudgetExhausted(self.stopped_by)

    def reserve(self, kind: CallKind, probe_id: str) -> Spend:
        """Count one call of ``kind`` for ``probe_id`` before it is made, and return its spend so far.
        Raises :class:`BudgetExhausted` instead when the call would go past a limit."""
        self.check()
        limit = self._exceeded(kind)
        if limit is not None:
            self.stopped_by = limit
            raise BudgetExhausted(limit)
        one = Spend(
            target_queries=int(kind == "target"),
            attacker_calls=int(kind == "attacker"),
            judge_calls=int(kind == "judge"),
        )
        self._add(probe_id, one)
        return one

    def record(self, probe_id: str, usage: ModelUsage | None) -> Spend:
        """Add what a model call reported it used."""
        if usage is None:
            return Spend()
        used = Spend(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cost_usd=usage.cost_usd or 0.0,
        )
        self._add(probe_id, used)
        return used

    def credit(self, probe_id: str, spend: Spend) -> None:
        """Count what an attempt spent in an earlier session of this run, when it is resumed."""
        self._add(probe_id, spend)

    def _add(self, probe_id: str, spend: Spend) -> None:
        self.spent = self.spent + spend
        self.by_probe[probe_id] = self.by_probe.get(probe_id, Spend()) + spend

    def caps(self) -> dict[str, float | int]:
        names = ("target_queries", "model_calls", "tokens", "cost_usd", "max_seconds")
        return {name: value for name in names if (value := getattr(self.budget, name)) is not None}

    def summary(self) -> dict[str, object]:
        """The run's ``budget`` record: the limits, what was spent and the limit that stopped it."""
        return {
            "limits": self.caps(),
            "spent": self.spent.model_dump(),
            "stopped_by": self.stopped_by,
            "by_probe": {probe: spend.model_dump() for probe, spend in sorted(self.by_probe.items())},
        }


__all__ = ["BudgetExhausted", "CallKind", "Ledger"]
