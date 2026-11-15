"""One start of a run against its limits (:class:`~martian.evals.model.RunLimits`).

Both runners keep their limits with a :class:`Budget`: the evals core's worker for each run and
scoring job (:mod:`martian.evals.worker.execute`), and ``martian evals run`` for a suite file
(:mod:`martian.evals.runner`). Each start or resume of a run has its limits afresh.
"""

from __future__ import annotations

from collections.abc import Mapping
from time import monotonic
from typing import Any, Literal

from martian.evals.model import RunLimits

INPUT_TOKENS = "mlflow.assessment.judgeInputTokens"
OUTPUT_TOKENS = "mlflow.assessment.judgeOutputTokens"
"""Where a judge's feedback keeps the usage its provider reported (MLflow's
``AssessmentMetadataKey.JUDGE_INPUT_TOKENS`` and ``JUDGE_OUTPUT_TOKENS``), as the SDK's judge
transport writes it, for the simulated user's calls too."""

Spender = Literal["judges", "simulator"]
"""Whose model calls used tokens: the run's LLM judges, or the simulator that plays its simulated
users (:mod:`martian.evals.simulation`)."""

_CALLER: dict[Spender, str] = {"judges": "the judge", "simulator": "the simulated user"}


def reported_tokens(metadata: Mapping[str, Any]) -> int | None:
    """The tokens one model call used, input and output together, from its metadata, or None when
    its provider reported none."""
    sent, received = metadata.get(INPUT_TOKENS), metadata.get(OUTPUT_TOKENS)
    if sent is None or received is None:
        return None
    try:
        return int(sent) + int(received)
    except (TypeError, ValueError):
        return None


class Budget:
    """The time since a run started and the model tokens it has used, against its limits.

    Tokens are what the provider reported for each model call on the judges' provider path, input
    and output together: each judgment's and, in ``martian evals run``, each reply of the simulated
    user's. ``max_judge_tokens`` counts them all; :attr:`used` keeps them apart. A call that
    reports none leaves a token limit unkeepable, so a run with one stops then. A call that fails
    reports nothing and counts nothing."""

    def __init__(self, limits: RunLimits) -> None:
        self.limits = limits
        self._started = monotonic()
        self.tokens = 0
        # Each spender's tokens; None once one of its calls reported none, as the count is then unknown.
        self.used: dict[Spender, int | None] = {"judges": 0, "simulator": 0}
        self._unreported: Spender | None = None

    def spend(self, tokens: int | None, by: Spender = "judges") -> None:
        """Count one model call's tokens, or None when its provider reported none."""
        if tokens is None:
            self._unreported = self._unreported or by
            self.used[by] = None
            return
        self.tokens += tokens
        if (counted := self.used[by]) is not None:
            self.used[by] = counted + tokens

    def reached(self) -> dict[str, Any] | None:
        """What the run records of the first limit it has reached (``RunLimits.stop``), or None."""
        limits = self.limits
        elapsed = monotonic() - self._started
        if limits.max_seconds is not None and elapsed >= limits.max_seconds:
            return limits.stop("max_seconds", elapsed)
        if limits.max_judge_tokens is not None and self._unreported is not None:
            return limits.stop("max_judge_tokens", None, unreported=_CALLER[self._unreported])
        if limits.max_judge_tokens is not None and self.tokens >= limits.max_judge_tokens:
            return limits.stop("max_judge_tokens", self.tokens, simulated=self.used["simulator"] or 0)
        return None


__all__ = ["INPUT_TOKENS", "OUTPUT_TOKENS", "Budget", "Spender", "reported_tokens"]
