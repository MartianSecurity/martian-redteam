"""Scorecards, judge calibration and run comparison: summaries over stored results, no model calls.

A port of the server's ``app/evals_quality.py`` and ``compare_runs`` (``app/evals_workbench.py``
in MorningHQ/MartianBackend), over the shared model's case results, scores and reviews in their
server shapes (``martian.evals.server.projection``). Two differences, because the model holds
less than the server's rows: a scorer's kind comes from its values (text values are categorical,
numbers other than booleans numeric, anything else boolean; ``response_length`` is numeric), and a
scoring job's definition digest is a digest of its pinned scorer versions. A scorecard also carries
``mean_confidence_95``, the t interval of a scorer's mean (``martian.evals.stats``), which the
server's scorecard does not have; its ``confidence_95`` is the same Wilson interval as the server's.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from statistics import mean
from typing import Any

from martian.evals.model import Run, Value, digest
from martian.evals.server.projection import Assessment, ReviewRow, dataset_identity
from martian.evals.server.schemas import EvalItemOut
from martian.evals.stats import Interval, mean_interval, wilson


def percentile(values: Iterable[Any], fraction: float) -> float | None:
    ordered = sorted(v for v in values if type(v) in (int, float) and math.isfinite(v))
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo))


def interval(found: Interval | None) -> list[float] | None:
    """An interval as the server's responses carry it: ``[low, high]``."""
    return None if found is None else [found[0], found[1]]


def definition_digest(config: dict[str, Any]) -> str:
    found = config.get("definition_digest")
    if isinstance(found, str) and found:
        return found
    return digest(
        {
            "scorers": sorted(config["scorers"]),
            "guidelines": config.get("guidelines", ""),
            "revision": "martian-eval-criteria-v1",
            "policies": config.get("policies", []),
        }
    )


def _kind(name: str, values: Sequence[Value]) -> str:
    if name == "response_length":
        return "numeric"
    if any(isinstance(value, str) for value in values):
        return "categorical"
    if any(type(value) in (int, float) for value in values):
        return "numeric"
    return "boolean"


def scorecard(
    items: Sequence[EvalItemOut], assessments: Iterable[Assessment], config: dict[str, Any] | None = None
) -> dict[str, Any]:
    config = config or {"scorers": ["exact_match"], "policies": []}
    by_name: dict[str, list[Assessment]] = {name: [] for name in config["scorers"]}
    for assessment in assessments:
        if assessment.name in by_name:
            by_name[assessment.name].append(assessment)
    policies = {p["name"]: p for p in config.get("policies", [])}
    total = len(items)
    cards = []
    for name, rows in by_name.items():
        # Never count missing or errored rows in the pass-rate denominator.
        values = [r.value for r in rows if r.value is not None and not r.error]
        kind = _kind(name, values)
        boolean = kind == "boolean"
        passed = sum(v is True for v in values)
        nums = [float(v) for v in values if type(v) in (int, float)]
        rate = passed / len(values) if boolean and values else None
        average = mean(nums) if nums else None
        coverage = len(values) / total if total else 0
        policy = policies.get(name, {"minimum_coverage": 1, "threshold": None, "direction": "higher"})
        metric = rate if boolean else average
        gate = "unconfigured"
        if policy.get("threshold") is not None:
            if coverage < policy["minimum_coverage"] or metric is None:
                gate = "insufficient_coverage"
            elif metric >= policy["threshold"] if policy["direction"] == "higher" else metric <= policy["threshold"]:
                gate = "passed"
            else:
                gate = "failed"
        cards.append(
            {
                "name": name,
                "kind": kind,
                "categories": {str(value): values.count(value) for value in values}
                if kind in ("categorical", "text")
                else None,
                "total": total,
                "evaluated": len(values),
                "missing": max(0, total - len(rows)),
                "errors": sum(bool(r.error) or r.value is None for r in rows),
                "coverage": coverage,
                "pass_rate": rate,
                "passed": passed if boolean else None,
                "confidence_95": interval(wilson(passed, len(values))) if boolean else None,
                "mean": average,
                "mean_confidence_95": interval(mean_interval(nums)),
                "p50": percentile(nums, 0.5),
                "p95": percentile(nums, 0.95),
                "minimum": min(nums) if nums else None,
                "maximum": max(nums) if nums else None,
                "gate": gate,
                "policy": policy,
            }
        )
    latencies = [i.latency_ms for i in items if i.latency_ms is not None]
    gates = {card["gate"] for card in cards}
    return {
        "criteria": cards,
        "total": total,
        "latency": {
            "observed": len(latencies),
            "missing": total - len(latencies),
            "p50_ms": percentile(latencies, 0.5),
            "p95_ms": percentile(latencies, 0.95),
        },
        "gate": next((gate for gate in ("failed", "insufficient_coverage", "passed") if gate in gates), "unconfigured"),
    }


def compare_runs(
    baseline: Run, baseline_items: Sequence[EvalItemOut], candidate: Run, candidate_items: Sequence[EvalItemOut]
) -> dict[str, Any]:
    a = {i.dataset_item_id: i for i in baseline_items}
    b = {i.dataset_item_id: i for i in candidate_items}
    left_dataset, right_dataset = dataset_identity(baseline), dataset_identity(candidate)
    reason = ""
    if baseline.state != "completed" or candidate.state != "completed":
        reason = "Both runs must be completed."
    elif left_dataset is None or left_dataset != right_dataset:
        reason = "Runs use different dataset identities; a paired comparison requires the same immutable dataset."
    elif any((a[k].prompt, a[k].expected) != (b[k].prompt, b[k].expected) for k in a.keys() & b.keys()):
        reason = "Record inputs or expectations changed between runs."
    rows = []
    for key in sorted(a.keys() | b.keys(), key=lambda k: (a.get(k) or b[k]).position):
        left, right = a.get(key), b.get(key)
        change = "not_comparable"
        if (
            not reason
            and left
            and right
            and left.status in ("passed", "failed")
            and right.status in ("passed", "failed")
        ):
            if left.status == right.status:
                change = "unchanged"
            else:
                change = "improved" if right.status == "passed" else "regressed"
        either = left or right
        assert either is not None
        rows.append(
            {
                "dataset_item_id": key,
                "prompt": either.prompt,
                "expected": either.expected,
                "baseline": _side(left),
                "candidate": _side(right),
                "change": change,
            }
        )
    return {
        "compatible": not reason,
        "reason": reason,
        "baseline_id": baseline.id,
        "candidate_id": candidate.id,
        "coverage": {
            "baseline": len(a),
            "candidate": len(b),
            "paired": len(a.keys() & b.keys()),
            "truncated": baseline.truncated or candidate.truncated,
        },
        "changes": {
            k: sum(i["change"] == k for i in rows) for k in ("improved", "regressed", "unchanged", "not_comparable")
        },
        "items": rows,
    }


def _side(item: EvalItemOut | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "trace_id": item.evals_trace_id,
        "status": item.status,
        "output": item.output,
        "latency_ms": item.latency_ms,
    }


def compare_assessments(
    result: dict[str, Any],
    baseline_items: Sequence[EvalItemOut],
    candidate_items: Sequence[EvalItemOut],
    left_job: dict[str, Any],
    right_job: dict[str, Any],
    left: Sequence[Assessment],
    right: Sequence[Assessment],
) -> dict[str, Any]:
    """Add each scorer's change per record to a comparison. ``left_job`` and ``right_job`` are the
    scoring jobs in their server shape."""
    reason = result["reason"]
    if definition_digest(left_job["config"]) != definition_digest(right_job["config"]):
        reason = "Scorer definitions, guidelines or threshold policies differ; select matching suite versions."
    lm = {(s.item_id, s.name): s for s in left}
    rm = {(s.item_id, s.name): s for s in right}
    li = {i.dataset_item_id: i for i in baseline_items}
    ri = {i.dataset_item_id: i for i in candidate_items}
    policies = {p["name"]: p for p in left_job["config"].get("policies", [])}
    names = left_job["config"]["scorers"]
    counts: dict[str, Counter[str]] = {n: Counter() for n in names}
    for row in result["items"]:
        left_item, right_item = li.get(row["dataset_item_id"]), ri.get(row["dataset_item_id"])
        criteria = {}
        for name in names:
            a = lm.get((left_item.id, name)) if left_item else None
            b = rm.get((right_item.id, name)) if right_item else None
            change = "not_comparable"
            delta = None
            if (
                not reason
                and a
                and b
                and not a.error
                and not b.error
                and a.value is not None
                and b.value is not None
                and a.source_id == b.source_id
            ):
                if isinstance(a.value, str) or isinstance(b.value, str):
                    change = "unchanged" if type(a.value) is type(b.value) and a.value == b.value else "changed"
                    counts[name][change] += 1
                    criteria[name] = {
                        "baseline": a.value,
                        "candidate": b.value,
                        "delta": None,
                        "change": change,
                        "baseline_source": a.source_id,
                        "candidate_source": b.source_id,
                    }
                    continue
                delta = float(b.value) - float(a.value)
                direction = policies.get(name, {}).get("direction", "higher")
                if delta == 0:
                    change = "unchanged"
                else:
                    change = "improved" if (delta > 0) == (direction == "higher") else "regressed"
            counts[name][change] += 1
            criteria[name] = {
                "baseline": a.value if a else None,
                "candidate": b.value if b else None,
                "delta": delta,
                "change": change,
                "baseline_source": a.source_id if a else None,
                "candidate_source": b.source_id if b else None,
            }
        row["criteria"] = criteria
    result.update(
        compatible=not reason,
        reason=reason,
        criteria={n: dict(c) for n, c in counts.items()},
        baseline_job_id=left_job["id"],
        candidate_job_id=right_job["id"],
        baseline_scorecard=scorecard(baseline_items, left, left_job["config"]),
        candidate_scorecard=scorecard(candidate_items, right, right_job["config"]),
    )
    return result


def calibration(assessments: Iterable[Assessment], reviews: Iterable[ReviewRow]) -> list[dict[str, Any]]:
    """How often each judge agrees with the latest complete human review of the same criterion."""
    latest: dict[tuple[Any, str], ReviewRow] = {}
    for review in reviews:
        latest.setdefault((review.item_id, review.criterion), review)
    by_name: dict[tuple[str, str], dict[str, Any]] = {}
    for a in assessments:
        if a.source != "LLM_JUDGE":
            continue
        stats = by_name.setdefault(
            (a.name, a.source_id),
            {
                "criterion": a.name,
                "source_id": a.source_id,
                "paired": 0,
                "missing": 0,
                "agree": 0,
                "true_positive": 0,
                "false_positive": 0,
                "true_negative": 0,
                "false_negative": 0,
            },
        )
        r = latest.get((a.item_id, a.name))
        if a.error or type(a.value) is not bool or not r or r.state != "complete" or type(r.value) is not bool:
            stats["missing"] += 1
            continue
        stats["paired"] += 1
        stats["agree"] += a.value == r.value
        if a.value and r.value:
            stats["true_positive"] += 1
        elif a.value:
            stats["false_positive"] += 1
        elif r.value:
            stats["false_negative"] += 1
        else:
            stats["true_negative"] += 1
    return [
        {
            **s,
            "agreement": s["agree"] / s["paired"] if s["paired"] else None,
            "alignment_eligible": s["paired"] >= 10
            and s["true_positive"] + s["false_negative"] > 0
            and s["true_negative"] + s["false_positive"] > 0,
        }
        for s in by_name.values()
    ]
