"""One start of a run against its limits (:class:`~martian.evals.model.RunLimits`).

Both runners keep their limits with a :class:`Budget`: the evals core's worker for each run and
scoring job (:mod:`martian.evals.worker.execute`), and ``martian evals run`` for a suite file
(:mod:`martian.evals.runner`). Each start or resume of a run has its limits afresh.
"""

from __future__ import annotations

from collections.abc import Mapping
from time import monotonic
from typing import Any

from martian.evals.model import RunLimits

INPUT_TOKENS = "mlflow.assessment.judgeInputTokens"
OUTPUT_TOKENS = "mlflow.assessment.judgeOutputTokens"
"""Where a judge's feedback keeps the usage its provider reported (MLflow's
``AssessmentMetadataKey.JUDGE_INPUT_TOKENS`` and ``JUDGE_OUTPUT_TOKENS``), as the SDK's judge
transport writes it."""


def reported_tokens(metadata: Mapping[str, Any]) -> int | None:
    """The tokens one judgment used, input and output together, from its feedback's metadata, or
    None when its provider reported none."""
    sent, received = metadata.get(INPUT_TOKENS), metadata.get(OUTPUT_TOKENS)
    if sent is None or received is None:
        return None
    try:
        return int(sent) + int(received)
    except (TypeError, ValueError):
        return None


class Budget:
    """The time since a run started and the judge tokens it has used, against its limits.

    Tokens are what the judge's provider reported for each judgment, input and output together. A
    judgment that reports none leaves a token limit unkeepable, so a run with one stops then. A
    judge call that fails reports nothing and counts nothing."""

    def __init__(self, limits: RunLimits) -> None:
        self.limits = limits
        self._started = monotonic()
        self.tokens = 0
        self._unreported = False

    def spend(self, tokens: int | None) -> None:
        """Count one judgment's tokens, or None when its provider reported none."""
        if tokens is None:
            self._unreported = True
        else:
            self.tokens += tokens

    def reached(self) -> dict[str, Any] | None:
        """What the run records of the first limit it has reached (``RunLimits.stop``), or None."""
        limits = self.limits
        elapsed = monotonic() - self._started
        if limits.max_seconds is not None and elapsed >= limits.max_seconds:
            return limits.stop("max_seconds", elapsed)
        if limits.max_judge_tokens is not None and self._unreported:
            return limits.stop("max_judge_tokens", None)
        if limits.max_judge_tokens is not None and self.tokens >= limits.max_judge_tokens:
            return limits.stop("max_judge_tokens", self.tokens)
        return None


__all__ = ["INPUT_TOKENS", "OUTPUT_TOKENS", "Budget", "reported_tokens"]
