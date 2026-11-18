"""The budget ledger: what a run, each probe and each attempt spent, against the run file's ``budget``.

Every call of the target, the attacker and the judge is reserved before it is made
(:meth:`Ledger.reserve`). A call that would go past a limit is not made: the ledger raises
:class:`BudgetExhausted`, and from then on every reservation fails with the same limit, so the run
stops cleanly and each attempt not yet decided is reported as stopped.

The count limits (``target_queries``, ``model_calls``) are checked before dispatch.
A token or cost limit permits one unsettled model call at a time. That call may cross the
limit. A call whose required usage is unavailable stops the run.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Literal

from pydantic import ValidationError

from martian.redteam.model import BUDGET_USAGE_UNAVAILABLE, BudgetLimit, RedteamRun, Spend, budget_usage_error
from martian.redteam.runfile import Budget
from martian_redteam.types import ModelUsage

CallKind = Literal["target", "attacker", "judge"]
_TOKEN_USAGE = "calls_without_token_usage"
_COST_USAGE = "calls_without_cost"


class BudgetExhausted(Exception):
    """A budget limit was reached or could not be verified; ``limit`` names it."""

    def __init__(self, limit: BudgetLimit, *, reason: str | None = None) -> None:
        error = budget_usage_error({"stop_reason": reason, "stopped_by": limit})
        super().__init__(error or f"the budget's {limit} ran out")
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
        self.stop_reason: str | None = None
        self.by_probe: dict[str, Spend] = {}
        self.usage: dict[str, int | None] = {_TOKEN_USAGE: 0, _COST_USAGE: 0}
        self._model_lock = asyncio.Lock()

    def model_slot(self) -> AbstractAsyncContextManager[None]:
        """Hold the spend check, dispatch and accounting together when model spend is capped."""
        if self.budget.limits_model_spend:
            return self._model_lock
        return nullcontext()

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
            raise BudgetExhausted(self.stopped_by, reason=self.stop_reason)

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
        """Record one settled model call, retaining observed amounts and missing usage separately."""
        sent = usage.input_tokens if usage is not None else None
        received = usage.output_tokens if usage is not None else None
        cost = usage.cost_usd if usage is not None else None
        if cost is not None and not math.isfinite(cost):
            cost = None
        for key, missing in ((_TOKEN_USAGE, sent is None or received is None), (_COST_USAGE, cost is None)):
            if missing and (known := self.usage[key]) is not None:
                self.usage[key] = known + 1
        used = Spend(
            input_tokens=sent if sent is not None else 0,
            output_tokens=received if received is not None else 0,
            cost_usd=cost if cost is not None else 0.0,
        )
        self._add(probe_id, used)
        self._stop_for_missing_usage()
        return used

    def _stop_for_missing_usage(self) -> None:
        if self.budget.tokens is not None and self.usage[_TOKEN_USAGE] != 0:
            self.stopped_by = "tokens"
        elif self.budget.cost_usd is not None and self.usage[_COST_USAGE] != 0:
            self.stopped_by = "cost_usd"
        else:
            return
        self.stop_reason = BUDGET_USAGE_UNAVAILABLE

    @staticmethod
    def _saved_spend(summary: object) -> Spend | None:
        if not isinstance(summary, dict) or not Spend.model_fields.keys() <= summary.keys():
            return None
        try:
            spend = Spend.model_validate(summary)
        except ValidationError:
            return None
        return spend if math.isfinite(spend.cost_usd) else None

    def restore(self, earlier: RedteamRun) -> None:
        """Restore the earlier session's cumulative spend once, including work that will run again."""
        summary = self._saved_spend(earlier.budget.get("spent"))
        if summary is not None:
            self.spent = summary
            saved_by_probe = earlier.budget.get("by_probe")
            if isinstance(saved_by_probe, dict):
                self.by_probe = {
                    key: spend
                    for key, value in saved_by_probe.items()
                    if (spend := self._saved_spend(value)) is not None
                }
        else:
            for attempt in earlier.attempts:
                if attempt.spend is not None:
                    self._add(attempt.probe_id, attempt.spend)

        # A running checkpoint can omit work dispatched since it was written. Legacy records also
        # omit completeness, so totals for their dispatched model calls cannot be certified on resume.
        coverage = earlier.budget.get("usage")
        for key in self.usage:
            if earlier.state == "running" or earlier.budget.get("history_incomplete") is True or summary is None:
                self.usage[key] = None
            elif isinstance(coverage, dict):
                value = coverage.get(key)
                self.usage[key] = value if type(value) is int and 0 <= value <= self.spent.model_calls else None
            elif self.spent.model_calls:
                self.usage[key] = None
        self._stop_for_missing_usage()

    def limitations(self) -> list[str]:
        notes: list[str] = []
        if self.stop_reason is not None and self.stopped_by is not None:
            notes.append(str(BudgetExhausted(self.stopped_by, reason=self.stop_reason)))
        for key, label in ((_TOKEN_USAGE, "Token usage"), (_COST_USAGE, "Cost")):
            missing = self.usage[key]
            if missing is None:
                notes.append(f"{label} for earlier calls is incomplete. Recorded spend is a lower bound.")
            elif missing:
                notes.append(f"{label} unavailable for {missing} model call(s). Recorded spend is a lower bound.")
        return notes

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
            "stop_reason": self.stop_reason,
            "usage": dict(self.usage),
            "by_probe": {probe: spend.model_dump() for probe, spend in sorted(self.by_probe.items())},
        }


__all__ = ["BudgetExhausted", "CallKind", "Ledger"]
