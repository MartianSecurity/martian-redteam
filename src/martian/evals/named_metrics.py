"""Named measurements and their weighted summaries across cases."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class CaseMetric(BaseModel):
    """A case's score for one metric and the weight that produced it, when recorded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: Finite
    weight: Finite | None = None


def recorded_metrics(scores: Mapping[str, Any], weights: Mapping[str, Any]) -> dict[str, CaseMetric]:
    """Read scores from an archive without inventing weights for older results."""
    found = {}
    for name, score in scores.items():
        if name:
            try:
                found[name] = CaseMetric(score=score, weight=weights.get(name))
            except ValidationError:
                continue
    return found


def measure(contributions: Iterable[tuple[str, float, float]]) -> dict[str, CaseMetric]:
    """Combine weighted components of a single case or assertion set."""
    totals: dict[str, tuple[float, float]] = {}
    for name, score, weight in contributions:
        points, denominator = totals.get(name, (0.0, 0.0))
        totals[name] = (points + score * weight, denominator + weight)
    found = {}
    for name, (points, weight) in totals.items():
        if not (math.isfinite(points) and math.isfinite(weight)):
            raise ArithmeticError("a named metric exceeds the finite numeric range")
        score = points / weight if weight > 0 else 0.0
        if not math.isfinite(score):
            raise ArithmeticError("a named metric exceeds the finite numeric range")
        found[name] = CaseMetric(score=score, weight=weight)
    return found


@dataclass
class _Total:
    cases: int = 0
    score_total: float = 0.0
    weight: float = 0.0
    complete: bool = True

    def add(self, measurement: CaseMetric) -> None:
        self.cases += 1
        if measurement.weight is None:
            self.complete = False
            return
        self.score_total += measurement.score * measurement.weight
        self.weight += measurement.weight
        self.complete = self.complete and math.isfinite(self.score_total) and math.isfinite(self.weight)

    def report(self, name: str) -> dict[str, Any]:
        score = self.score_total / self.weight if self.complete and self.weight > 0 else None
        return {
            "name": name,
            "cases": self.cases,
            "score": score if score is not None and math.isfinite(score) else None,
            "score_total": self.score_total if self.complete else None,
            "weight": self.weight if self.complete else None,
        }


def summarize(rows: Iterable[Mapping[str, CaseMetric]]) -> list[dict[str, Any]]:
    """Aggregate measured cases. Without every weight, report coverage without a weighted mean."""
    totals: dict[str, _Total] = {}
    for row in rows:
        for name, metric in row.items():
            totals.setdefault(name, _Total()).add(metric)
    return [totals[name].report(name) for name in sorted(totals)]
