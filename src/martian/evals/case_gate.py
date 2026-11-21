"""Weighted case verdicts calculated from completed suite checks."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from martian.evals.named_metrics import CaseMetric, measure

CASE_GATE = "case_gate"
GateNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
MetricName = Annotated[str, Field(min_length=1)]


class AssertionSet(BaseModel):
    """Checks whose score and verdict count as one assertion in the case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    checks: list[str]
    threshold: GateNumber | None = None
    weight: GateNumber = 1.0
    metric: MetricName | None = None

    @model_serializer(mode="wrap")
    def _without_empty_metric(self, handler: SerializerFunctionWrapHandler) -> Any:
        written = handler(self)
        if isinstance(written, dict) and self.metric is None:
            written.pop("metric", None)
        return written


@dataclass(frozen=True)
class _Weighted:
    score: float
    passed: bool
    weight: float


def _aggregate(members: Sequence[_Weighted], threshold: float | None, *, weight: float = 1.0) -> _Weighted:
    points = 0.0
    total = 0.0
    for member in members:
        points += member.score * member.weight
        total += member.weight
    if not (math.isfinite(total) and math.isfinite(points)):
        raise ArithmeticError("the weighted sum exceeds the finite numeric range")
    score = points / total if total > 0 else 0.0
    if not math.isfinite(score):
        raise ArithmeticError("the weighted score exceeds the finite numeric range")
    passed = score >= threshold if threshold is not None else all(member.passed for member in members)
    return _Weighted(score, passed, weight)


class CaseGate(BaseModel):
    """A threshold and optional weighted checks and assertion sets for a suite's cases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold: GateNumber | None = None
    weights: dict[str, GateNumber] = Field(default_factory=dict)
    assertion_sets: list[AssertionSet] = Field(default_factory=list)
    metrics: dict[str, MetricName] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _without_empty_metrics(self, handler: SerializerFunctionWrapHandler) -> Any:
        written = handler(self)
        if isinstance(written, dict) and not self.metrics:
            written.pop("metrics", None)
        return written

    def scoring_rules(self) -> dict[str, Any] | None:
        """The parts used when comparing verdicts, independently of reporting labels."""
        if self.threshold is None and not self.weights and not self.assertion_sets:
            return None
        return self.model_dump(mode="json", exclude={"metrics": True, "assertion_sets": {"__all__": {"metric"}}})

    def metric_names(self) -> set[str]:
        return set(self.metrics.values()) | {group.metric for group in self.assertion_sets if group.metric is not None}

    def rename_checks(self, names: Mapping[str, str]) -> CaseGate:
        """Use the labels of the case's checks in the suite that holds them."""
        return self.model_copy(
            update={
                "weights": {names.get(label, label): value for label, value in self.weights.items()},
                "metrics": {names.get(label, label): value for label, value in self.metrics.items()},
                "assertion_sets": [
                    group.model_copy(update={"checks": [names.get(label, label) for label in group.checks]})
                    for group in self.assertion_sets
                ],
            }
        )

    def validate_checks(self, labels: Collection[str]) -> None:
        if CASE_GATE in labels:
            raise ValueError(f"the {CASE_GATE} check name is reserved when a case gate is configured")
        members = [label for group in self.assertion_sets for label in group.checks]
        if unknown := sorted((set(self.weights) | set(self.metrics) | set(members)) - set(labels)):
            raise ValueError(f"case_gate names unknown checks: {', '.join(unknown)}")
        if len(set(members)) != len(members):
            raise ValueError("case_gate assigns a check to an assertion set more than once")
        names = [group.name for group in self.assertion_sets]
        if len(set(names)) != len(names):
            raise ValueError("case_gate assertion set names must be unique")

    def apply(
        self,
        record: dict[str, Any],
        labels: Sequence[str],
        required: Collection[str],
        *,
        incomplete: bool = False,
    ) -> None:
        """Record the aggregate alongside the component verdicts and update the case status."""
        checks = record["checks"]
        metadata: dict[str, str] = {}
        if self.threshold is not None:
            metadata["threshold"] = str(self.threshold)
        if incomplete or record.get("error"):
            error = "the case did not produce a complete result"
        else:
            expected = dict.fromkeys([*labels, *required])
            missing = [
                label
                for label in expected
                if checks.get(label, {}).get("passed") is None or checks[label].get("value", 0) is None
            ]
            error = f"checks without a verdict: {', '.join(missing)}" if missing else ""
        passed = None
        value = None
        rationale = None
        if not error:
            try:
                aggregate, set_scores, metrics = self._score(checks, labels)
            except ArithmeticError as exc:
                error = str(exc)
            else:
                value = aggregate.score
                metadata["score"] = str(value)
                metadata.update({f"assertion_sets.{name}.score": str(score) for name, score in set_scores.items()})
                if metrics:
                    record["named_metrics"] = {name: metric.model_dump(mode="json") for name, metric in metrics.items()}
                failed = [label for label in required if checks[label]["passed"] is False]
                passed = aggregate.passed and not failed
                rationale = f"Weighted score {aggregate.score:.8g}"
                if self.threshold is not None:
                    comparator = ">=" if aggregate.passed else "<"
                    rationale += f" {comparator} threshold {self.threshold:.8g}"
                elif not aggregate.passed:
                    rationale += ". A scored check or assertion set failed"
                if failed:
                    rationale += f". Required checks failed: {', '.join(failed)}"
        checks[CASE_GATE] = {
            "passed": passed,
            "value": value,
            "rationale": rationale,
            "error": error or None,
            "metadata": metadata,
        }
        record["passed"] = passed

    def _score(
        self, checks: dict[str, Any], labels: Sequence[str]
    ) -> tuple[_Weighted, dict[str, float], dict[str, CaseMetric]]:
        leaves = {}
        for label in labels:
            check = checks[label]
            passed = check["passed"]
            weight = self.weights.get(label, 1.0)
            leaves[label] = _Weighted(check.get("value", float(passed)), passed or weight == 0, weight)
        grouped = {label for group in self.assertion_sets for label in group.checks}
        direct = [leaves[label] for label in labels if label not in grouped]
        sets = {
            group.name: _aggregate([leaves[label] for label in group.checks], group.threshold, weight=group.weight)
            for group in self.assertion_sets
        }
        overall = _aggregate([*direct, *sets.values()], self.threshold)
        contributions = [
            (self.metrics[label], leaves[label].score, leaves[label].weight or 1.0)
            for label in labels
            if label not in grouped and label in self.metrics
        ]
        for group in self.assertion_sets:
            weight = group.weight or 1.0
            if group.metric is not None:
                contributions.append((group.metric, sets[group.name].score, weight))
            child = measure(
                (self.metrics[label], leaves[label].score, leaves[label].weight or 1.0)
                for label in group.checks
                if label in self.metrics
            )
            for name, metric in child.items():
                if name != group.metric:
                    assert metric.weight is not None
                    contributions.append((name, metric.score, metric.weight * weight))
        return overall, {name: group.score for name, group in sets.items()}, measure(contributions)


class CaseChecks(BaseModel):
    """Checks and scoring rules shared by the named cases in a suite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cases: tuple[str, ...] = Field(min_length=1)
    checks: tuple[str, ...] = Field(min_length=1)
    case_gate: CaseGate | None = None
    # Original check labels for deterministic comparison with the migration source.
    aliases: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self) -> CaseChecks:
        if len(set(self.checks)) != len(self.checks):
            raise ValueError("a check set cannot list a check twice")
        if set(self.aliases) - set(self.checks):
            raise ValueError("check set aliases must reference its own checks")
        names = [self.aliases.get(label, label) for label in self.checks]
        if len(set(names)) != len(names) or any(not name or name == CASE_GATE for name in names):
            raise ValueError("check set aliases must give each check a distinct name")
        if self.case_gate is not None:
            self.case_gate.validate_checks(self.checks)
        return self


def case_check_index(check_sets: Sequence[CaseChecks]) -> dict[str, CaseChecks]:
    found: dict[str, CaseChecks] = {}
    for selected in check_sets:
        for case_id in selected.cases:
            if case_id in found:
                raise ValueError(f"case {case_id!r} appears in more than one check set")
            found[case_id] = selected
    return found


def validate_gates(gate: CaseGate | None, check_sets: Sequence[CaseChecks], labels: Collection[str]) -> set[str]:
    """Validate the suite's checks and return the names of its measured metrics."""
    if not check_sets:
        if gate is None:
            return set()
        gate.validate_checks(labels)
        return gate.metric_names()
    if gate is not None:
        raise ValueError("check_sets and a suite-wide case_gate cannot be used together")
    if CASE_GATE in labels:
        raise ValueError(f"the {CASE_GATE} check name is reserved when check_sets are configured")
    case_check_index(check_sets)
    used = {name for selected in check_sets for name in selected.checks}
    if unknown := sorted(used - set(labels)):
        raise ValueError(f"check_sets name unknown checks: {', '.join(unknown)}")
    if unused := sorted(set(labels) - used):
        raise ValueError(f"checks must appear in a check set: {', '.join(unused)}")
    return {
        name for selected in check_sets if selected.case_gate is not None for name in selected.case_gate.metric_names()
    }


def gate_definition(gate: CaseGate | None, check_sets: Sequence[CaseChecks]) -> dict[str, Any] | None:
    """The definition a recorded gate score identifies, including assignments when cases differ."""
    if gate is not None:
        return gate.model_dump(mode="json")
    if any(selected.case_gate is not None for selected in check_sets):
        return {"check_sets": [selected.model_dump(mode="json", exclude_none=True) for selected in check_sets]}
    return None
