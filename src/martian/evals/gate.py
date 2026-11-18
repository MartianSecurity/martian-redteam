"""Fail-closed gate over an MLflow EvaluationResult.

`result.passed` cannot be used as the gate: with `expectations` in the data it is False even when
every scorer passed, and it is True when `result_df` is None, a scorer column is missing, or the
store rejected writes. This reads the scorer columns directly and requires full row coverage.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import Any, NoReturn

from martian._privacy import report_text, safe_text
from martian.evals.dataset import ITEM_TAG, EvalDataset
from martian.evals.scorers import MAX_OUTPUT_CHARS, output_text
from martian.evals.state import STATE, GateRecord
from martian.evals.types import EvaluationResultLike

SCORER_FOR_UPLOAD = "exact_match"


class GateFailure(AssertionError):
    """The run did not meet the gate. Subclasses AssertionError so pytest reports it normally."""


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else None


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _clean(text: object) -> str:
    """Backend rejects NUL and unpaired surrogates for the whole session; neutralise them here."""
    return safe_text(text)


def _redact(text: str, env: str) -> str:
    """Optional data minimisation for check rows: keep a short digest instead of the text."""
    return report_text(text, redact_env=env)


def _mark_checked(result: EvaluationResultLike) -> None:
    """The plugin fails a test whose evaluate() result never reached assert_gate/assert_checks."""
    with suppress(AttributeError):
        result.__dict__["_martian_checked"] = True


def _duplicates(names: Sequence[str]) -> list[str]:
    seen, dup = set(), []
    for name in names:
        if name in seen and name not in dup:
            dup.append(name)
        seen.add(name)
    return dup


def _rows(df: Any, ids: list[str]) -> list[dict[str, Any]]:
    """Per-item upload rows. Outputs only: expected text never leaves Backend's side."""
    out = []
    for position, item_id in enumerate(ids):
        response = df["response"].iloc[position] if "response" in df.columns else None
        state = str(df["state"].iloc[position]).upper() if "state" in df.columns else "OK"
        latency = df["execution_duration"].iloc[position] if "execution_duration" in df.columns else None
        text = "" if _is_missing(response) else output_text(response)
        failed = _is_missing(response) or "ERROR" in state
        too_large = len(text) > MAX_OUTPUT_CHARS
        out.append(
            {
                "item_id": item_id,
                "output": "" if failed or too_large else text,
                "error": "agent call failed" if failed else ("output too large" if too_large else ""),
                "latency_ms": None if latency is None or _is_missing(latency) else float(latency),
            }
        )
    return out


def assert_gate(
    result: EvaluationResultLike,
    dataset: EvalDataset,
    *,
    scorers: Sequence[str] = ("exact_match",),
    threshold: float | None = None,
    minimum_coverage: float | None = None,
    pass_if: Mapping[str, Callable[[Any], bool]] | None = None,
) -> dict[str, Any]:
    """Raise GateFailure unless the run covered the whole dataset and met the gate.

    With no threshold every row must pass. With a threshold the exact_match pass rate over the
    evaluated rows must reach it and coverage must reach `minimum_coverage` (default 1.0).
    """
    if "exact_match" not in scorers:
        # Backend re-scores exact match and nothing else; a gate that does not include it could
        # pass here while the server fails it, or pass with no scorer at all.
        raise ValueError("assert_gate scorers must include 'exact_match'")
    if _duplicates(scorers):
        raise ValueError("assert_gate scorer names must be unique")
    pass_if = pass_if or {}
    threshold = threshold if threshold is not None else _env_float("MARTIAN_EVALS_THRESHOLD")
    if minimum_coverage is None:
        minimum_coverage = _env_float("MARTIAN_EVALS_MIN_COVERAGE")
    minimum_coverage = 1.0 if minimum_coverage is None else minimum_coverage
    pin = dataset.pin
    expected_ids = dataset.item_ids

    test = STATE.test_name()
    _mark_checked(result)

    def record(rows: list[dict[str, Any]] | None, summary: dict[str, Any], failure: str) -> None:
        STATE.record(
            GateRecord(
                test=test,
                kind="dataset",
                dataset_name=pin.name,
                dataset_id=pin.dataset_id,
                items_digest=pin.items_digest,
                truncated=pin.truncated,
                threshold=threshold,
                minimum_coverage=minimum_coverage,
                rows=rows,
                summary=summary,
                failure=failure,
            )
        )

    def fail(message: str, rows: list[dict[str, Any]] | None = None, summary: dict[str, Any] | None = None) -> NoReturn:
        record(rows, summary or {}, message)
        raise GateFailure(message)

    if threshold is not None and not 0 <= threshold <= 1:
        fail("threshold must be finite and between 0 and 1")
    if not 0 <= minimum_coverage <= 1:
        fail("minimum_coverage must be finite and between 0 and 1")
    if pin.truncated:
        fail(
            f"the dataset has more usable items than the {pin.cap}-item cap, so part of it cannot be "
            "gated; reduce it to the cap"
        )
    df = getattr(result, "result_df", None)
    if df is None:
        fail(
            "result_df is None: no per-row results were recorded. Check that predict_fn is traced "
            "and the local store is writable."
        )
    if len(df) != len(expected_ids):
        fail(f"result has {len(df)} rows for a dataset of {len(expected_ids)}")
    if "tags" not in df.columns:
        fail("result frame has no tags column; item ids cannot be mapped")
    ids = [(tags or {}).get(ITEM_TAG) for tags in df["tags"]]
    if sorted(map(str, ids)) != sorted(expected_ids):
        fail("result rows do not match the pulled dataset items")
    for name in scorers:
        if f"{name}/value" not in df.columns:
            fail(f"scorer column {name}/value is missing: every score write may have been rejected")

    rows = _rows(df, [str(i) for i in ids])
    total = len(ids)
    failing: list[str] = []
    missing: list[str] = []
    summary: dict[str, Any] = {"total": total, "scorers": {}, "truncated": pin.truncated}
    for name in scorers:
        values = df[f"{name}/value"].tolist()
        evaluated = passed = 0
        for item_id, value in zip(ids, values, strict=True):
            if _is_missing(value):
                missing.append(f"{name}:{item_id}")
            else:
                evaluated += 1
                # The same interpretation as assert_checks: a bare number is not a pass.
                if _interpret(value, pass_if.get(name)) is True:
                    passed += 1
                else:
                    failing.append(f"{name}:{item_id}")
        coverage = evaluated / total if total else 0.0
        rate = passed / evaluated if evaluated else None
        summary["scorers"][name] = {
            "evaluated": evaluated,
            "passed": passed,
            "pass_rate": rate,
            "coverage": coverage,
        }
    # Rows whose agent call failed are errors for the gate even if a scorer produced a value.
    errored = [r["item_id"] for r in rows if r["error"]]
    summary["errored"] = len(errored)

    problems = []
    if threshold is None:
        if failing:
            problems.append(f"{len(failing)} failing row(s): " + ", ".join(failing[:10]))
        if missing:
            problems.append(f"{len(missing)} row(s) without a score: " + ", ".join(missing[:10]))
    else:
        for name, s in summary["scorers"].items():
            if s["pass_rate"] is None or s["pass_rate"] < threshold:
                problems.append(f"{name} pass rate {s['pass_rate']} is below threshold {threshold}")
            if s["coverage"] < minimum_coverage:
                problems.append(f"{name} coverage {s['coverage']:.2f} is below {minimum_coverage:.2f}")
    if missing and threshold is not None:
        problems.append(f"{len(missing)} required score(s) are missing")
    if errored:
        problems.append(f"{len(errored)} agent call(s) failed: " + ", ".join(errored[:10]))
    summary["problems"] = problems
    if problems:
        record(rows, summary, "; ".join(problems))
        raise GateFailure("; ".join(problems))
    record(rows, summary, "")
    return summary


# ---------- scorer-defined checks (the MLflow `Guidelines` style) ----------

_YES = {"yes", "true", "pass", "passed"}
_NO = {"no", "false", "fail", "failed"}
MAX_INPUT_CHARS = 4000
MAX_RATIONALE_CHARS = 1000
# Backend bounds: exceeding one rejects the whole session, so the kit enforces them itself.
MAX_ROWS_PER_TEST = 100
MAX_SCORERS = 20
MAX_TEST_NAME = 300


def _safe_name(name: str) -> str:
    clean = _clean(name)
    if len(clean) <= MAX_TEST_NAME:
        return clean
    return clean[: MAX_TEST_NAME - 9] + "…" + hashlib.sha1(clean.encode()).hexdigest()[:8]


def _is_bool(value: object) -> bool:
    """A Python or numpy boolean. MLflow hands boolean scorer values back as numpy booleans."""
    try:
        import numpy as np

        return isinstance(value, (bool, np.bool_))
    except ImportError:  # numpy ships with pandas, which the kit requires
        return isinstance(value, bool)


def _interpret(value: Any, pass_if: Callable[[Any], bool] | None) -> bool | None:
    """True / False / None (no usable value). Judge-style 'yes'/'no' and booleans decide by value;
    anything else (a number, a free-form label) needs an explicit `pass_if`, like MLflow's own
    `result.passed`, and otherwise counts as not passing."""
    if _is_missing(value):
        return None
    if pass_if is not None:
        try:
            decision = pass_if(value)
            return bool(decision) if _is_bool(decision) else False
        except Exception:  # noqa: BLE001 - a raising predicate is a failed check, not a crash
            return False
    if isinstance(value, str):
        low = value.strip().lower()
        return True if low in _YES else False if low in _NO else False
    if _is_bool(value):
        return bool(value)
    return False


def _feedback_names(df: Any) -> list[str]:
    """Scorer (feedback) names in the frame; expectations also add `<name>/value` columns."""
    names: list[str] = []
    if "assessments" in df.columns:
        for assessments in df["assessments"]:
            for item in assessments or []:
                if isinstance(item, dict) and "feedback" in item:
                    name = item.get("assessment_name") or item.get("name")
                    if name and name not in names:
                        names.append(name)
    return names


def assert_checks(
    result: EvaluationResultLike,
    *,
    scorers: Sequence[Any] | None = None,
    pass_if: Mapping[str, Callable[[Any], bool]] | None = None,
    name: str | None = None,
    allow_subset: bool = False,
) -> dict[str, Any]:
    """The MLflow regression-test assertion for inline data and any scorer, fail-closed.

    Replaces `assert result.passed, result.reason`: every row must have a passing value from every
    scorer, with no agent error and no missing value. The scorers (including LLM judges) run on the
    runner, so the uploaded record is client-scored and labelled so; Backend decides only the
    aggregate.

    The scorers asserted are the ones the `evaluate()` call was given (recorded by the plugin).
    `scorers=` may restate them but cannot silently narrow them (`allow_subset=True` opts in), and
    names must be unique because two scorers with one name share one result column. `pass_if` maps a
    scorer name to a predicate for numeric or categorical values.
    """
    pass_if = pass_if or {}
    test = _safe_name(name or STATE.test_name())
    _mark_checked(result)

    def fail(message: str, rows: list[dict[str, Any]] | None = None, summary: dict[str, Any] | None = None) -> NoReturn:
        STATE.record(GateRecord(test=test, kind="checks", check_rows=rows, summary=summary or {}, failure=message))
        raise GateFailure(message)

    df = getattr(result, "result_df", None)
    if df is None or len(df) == 0:
        fail("result_df is empty or missing: no per-row results were recorded")
    if len(df) > MAX_ROWS_PER_TEST:
        fail(f"{len(df)} rows exceed the {MAX_ROWS_PER_TEST}-row limit of one test; split the data")
    recorded = getattr(result, "_martian_scorers", None)
    explicit = [getattr(s, "name", s) for s in scorers] if scorers is not None else None
    if recorded is None and explicit is None:
        # The plugin could not see which scorers evaluate() was given (for example `evaluate` was
        # imported before the plugin patched it), and a scorer that returned nothing leaves no
        # column to find. Do not guess from the frame.
        fail("cannot tell which scorers ran: pass scorers=[...] explicitly or call mlflow.genai.evaluate")
    wanted = list(explicit if explicit is not None else recorded or [])
    if _duplicates(recorded or []) or _duplicates(wanted):
        fail("scorer names must be unique: two scorers with one name share one result column")
    if "<unnamed>" in wanted or (recorded and "<unnamed>" in recorded):
        fail("a scorer has no name; give every scorer a unique name")
    if recorded and explicit is not None and not allow_subset:
        left_out = [n for n in recorded if n not in explicit]
        if left_out:
            fail("scorers= leaves out scorer(s) that evaluate() ran: " + ", ".join(left_out))
    if not wanted:
        fail("no scorers to assert")
    if len(wanted) > MAX_SCORERS or any(not 1 <= len(n) <= 80 for n in wanted):
        fail(f"at most {MAX_SCORERS} scorers per test, with names of 1 to 80 characters")
    absent = [n for n in wanted if f"{n}/value" not in df.columns]
    if absent:
        fail("scorer column(s) missing from the result: " + ", ".join(absent))

    rows = []
    failing: list[str] = []
    missing: list[str] = []
    errored: list[int] = []
    for position in range(len(df)):
        response = df["response"].iloc[position] if "response" in df.columns else None
        state = str(df["state"].iloc[position]).upper() if "state" in df.columns else "OK"
        broke = _is_missing(response) or "ERROR" in state
        request = df["request"].iloc[position] if "request" in df.columns else ""
        text = "" if broke else output_text(response)
        scores = {}
        for scorer_name in wanted:
            raw = df[f"{scorer_name}/value"].iloc[position]
            outcome = _interpret(raw, pass_if.get(scorer_name))
            rationale = ""
            if f"{scorer_name}/rationale" in df.columns:
                got = df[f"{scorer_name}/rationale"].iloc[position]
                rationale = "" if _is_missing(got) else str(got)
            if outcome is not None and not isinstance(raw, str) and not _is_bool(raw):
                rationale = (rationale + f" value={raw}").strip()
            scores[scorer_name] = {
                "value": outcome,
                "rationale": report_text(_clean(rationale), redact_env="MARTIAN_EVALS_REDACT_OUTPUTS")[
                    :MAX_RATIONALE_CHARS
                ],
            }
            if outcome is False:
                failing.append(f"row {position + 1}:{scorer_name}")
            elif outcome is None:
                missing.append(f"row {position + 1}:{scorer_name}")
        if broke:
            errored.append(position + 1)
        rows.append(
            {
                "input": _redact(_clean(output_text(request))[:MAX_INPUT_CHARS], "MARTIAN_EVALS_REDACT_INPUTS"),
                "output": _redact(_clean(text)[:MAX_OUTPUT_CHARS], "MARTIAN_EVALS_REDACT_OUTPUTS"),
                "error": "agent call failed" if broke else "",
                "scores": scores,
            }
        )
    summary = {
        "rows": len(rows),
        "scorers": wanted,
        "failing": len(failing),
        "missing": len(missing),
        "errored": len(errored),
    }
    problems = []
    if failing:
        problems.append(f"{len(failing)} failing check(s): " + ", ".join(failing[:10]))
    if missing:
        problems.append(f"{len(missing)} check(s) without a value: " + ", ".join(missing[:10]))
    if errored:
        problems.append(f"{len(errored)} agent call(s) failed: rows " + ", ".join(map(str, errored[:10])))
    return _record_checks(test, rows, summary, problems)


def _record_checks(
    test: str, rows: list[dict[str, Any]] | None, summary: dict[str, Any], problems: list[str]
) -> dict[str, Any]:
    """Retain a checks gate before raising so a swallowed error still fails the session."""
    STATE.record(
        GateRecord(
            test=test,
            kind="checks",
            check_rows=rows,
            summary=summary,
            failure="; ".join(problems),
        )
    )
    if problems:
        raise GateFailure("; ".join(problems))
    return summary
