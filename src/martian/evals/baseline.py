"""A suite-file run compared with an earlier run of the same suite, case by case: a baseline in CI.

Every suite-file run writes ``verdicts.json`` beside its results: each case's verdict and its
checks' verdicts, with no input, reply or judge reason, so the file can be kept as a baseline
where ``results.jsonl`` cannot, such as in a CI cache that pull requests read.
``martian evals run evals.yaml --baseline DIR`` (``run_suite(path, baseline=DIR)``) reads
``DIR/verdicts.json`` before any case runs, runs the suite, pairs its cases with the baseline's,
labels the change with :func:`martian.evals.stats.compare_outcomes`, and writes
``comparison.json`` beside the results.

- **A pair** is a case in both runs, by id, with the same inputs and expectations (a digest of
  both) and a verdict in both. A case whose inputs or expectations changed is not paired: the
  comparison measures a change of the system, not of an edited case.
- **The gate.** With a baseline the gate is the comparison. The run fails when the change is
  worse beyond noise (``worse``); when a paired case newly fails a security check, whatever the
  label; or when a case that the baseline has no pair for failed: a new case, one whose inputs or
  expectations changed, or one the baseline has no verdict for. A case that failed in both runs,
  and cases that went from pass to fail within noise on other checks, do not fail it; the
  comparison names every case that went from pass to fail. A case without a verdict fails the run
  as it does without a baseline.
- **Security checks** are the checks the candidate run names as such in its verdicts file: the
  checks whose :attr:`martian.evals.runner.CheckSpec.security_check` is true: the types in
  :data:`martian.evals.runner.SECURITY_CHECKS`, and a python check marked ``security: true``. A
  paired case newly fails one when it failed it in this run and the baseline's case passed it or
  had no such check. One leaked secret or followed
  injection is never noise, so each such case fails the gate on its own, also when the case
  already failed on another check in the baseline.
- **Reasons.** A failed check's reason is kept only when the SDK wrote it without quoting the
  reply: the deterministic checks in :data:`PLAIN_REASONS`, which report counts, sizes or the
  names the suite gives, and with repeats every check, whose reason is then how its attempts
  went under its rule. An LLM judge's reason, and a numeric, table or schema check's, which
  quote the reply's figures or keys, are left out. A case's error is the SDK's own wording of
  how the target failed, as in the pull-request comment.
- **A matrix.** A suite with a matrix (:mod:`martian.evals.matrix`) writes each combination's
  verdicts.json in its own folder, and at the top of the output folder a verdicts.json of format
  :data:`MATRIX_FORMAT` that holds them all by combination id (:func:`matrix_verdicts`). Each
  combination is compared with the baseline's result of the same id (:func:`result`). A
  combination the baseline has no result of, such as a new one, or every combination when the
  baseline is a run without a matrix, is not compared: it is gated on its own, as a run without a
  baseline is. So is a suite without a matrix whose baseline is a matrix's.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from martian.evals.stats import compare_outcomes

VERDICTS = "verdicts.json"
"""Each case's verdicts, written by every suite-file run; a later run's baseline."""

COMPARISON = "comparison.json"
"""A run compared with its baseline, written by a run given one."""

FORMAT = "martian-evals-verdicts/1"

MATRIX_FORMAT = "martian-evals-matrix-verdicts/1"
"""A matrix run's verdicts.json: each combination's verdicts file under ``combinations``, by id."""

PLAIN_REASONS = frozenset(
    {
        "contains",
        "not_contains",
        "contains_any",
        "contains_all",
        "equals",
        "starts_with",
        "ends_with",
        "similarity",
        "matches",
        "not_matches",
        "is_json",
        "length",
        "latency",
        "cost",
        "no_prohibited_phrases",
        "no_personal_data",
        "no_secrets",
        "resists_injection",
        "no_refusal",
    }
)
"""Check types whose reason the SDK writes from counts, sizes and the names the suite gives,
never from the reply's text. A check type that is not here keeps no reason, so a new one is left
out until it is added."""


class BaselineError(ValueError):
    """The baseline cannot be read: no verdicts.json in the folder, or not one this SDK wrote."""


def matrix_verdicts(combinations: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The verdicts file of a matrix run: each combination's :func:`verdicts`, by its id."""
    return {"format": MATRIX_FORMAT, "combinations": {key: dict(found) for key, found in combinations.items()}}


def result(found: Mapping[str, Any], combination: str | None) -> dict[str, Any] | None:
    """The verdicts in a loaded baseline (:func:`load`) of the combination ``combination``, or of a
    suite without a matrix when None; None when the baseline has no result of it."""
    if found["format"] == FORMAT:
        return dict(found) if combination is None else None
    held = found["combinations"].get(combination) if combination is not None else None
    return None if held is None else dict(held)


def _readable(found: Any) -> bool:
    cases = found.get("cases") if isinstance(found, dict) else None
    return (
        isinstance(cases, list)
        and found.get("format") == FORMAT
        and isinstance(found.get("checks"), dict)
        and all(
            isinstance(case, dict)
            and isinstance(case.get("id"), str)
            and isinstance(case.get("digest"), str)
            and case.get("passed") in (True, False, None)
            and isinstance(case.get("checks"), dict)
            for case in cases
        )
    )


def digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def case_digest(inputs: Mapping[str, Any], expectations: Mapping[str, Any]) -> str:
    """What makes two runs' results of a case a pair: its inputs and expectations, not its reply."""
    return digest({"inputs": inputs, "expectations": expectations})


def verdicts(
    records: Sequence[Mapping[str, Any]],
    digests: Mapping[str, str],
    *,
    checks: Mapping[str, str],
    plain: Collection[str],
    security: Collection[str],
    attempts: int,
) -> dict[str, Any]:
    """The verdicts file of a run: ``records`` are its results.jsonl records in case order,
    ``digests`` each case's :func:`case_digest`, ``checks`` each check's label with a digest of its
    definition, ``plain`` the labels whose reasons are kept, ``security`` the labels of its
    security checks, and ``attempts`` the attempts of each case."""
    cases = []
    for record in records:
        found = {}
        for label, check in record["checks"].items():
            failed = check.get("passed") is False
            reason = check.get("rationale") if failed and label in plain else None
            found[label] = {"passed": check.get("passed"), "reason": None if reason is None else str(reason)[:300]}
        cases.append(
            {
                "id": str(record["id"]),
                "digest": digests[str(record["id"])],
                "passed": record["passed"],
                "error": record.get("error"),
                "checks": found,
            }
        )
    return {
        "format": FORMAT,
        "attempts": attempts,
        "checks": dict(checks),
        "security": [label for label in checks if label in security],
        "cases": cases,
    }


def load(folder: str | Path) -> dict[str, Any]:
    """The verdicts file in ``folder``, an earlier run's output folder: a run's, or a matrix run's
    (:data:`MATRIX_FORMAT`). Raises :class:`BaselineError`."""
    path = Path(folder) / VERDICTS
    if not path.is_file():
        raise BaselineError(
            f"the baseline folder has no {VERDICTS}: give the output folder of an earlier run of this suite"
        )
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise BaselineError(f"the baseline's {VERDICTS} could not be read") from None
    if isinstance(found, dict) and found.get("format") == MATRIX_FORMAT:
        held = found.get("combinations")
        if not isinstance(held, dict) or not all(_readable(one) for one in held.values()):
            raise BaselineError(f"the baseline's {VERDICTS} is not a {MATRIX_FORMAT} file")
        return found
    if not _readable(found):
        raise BaselineError(f"the baseline's {VERDICTS} is not a {FORMAT} file")
    return dict(found)


def compare(baseline: Mapping[str, Any], candidate: Mapping[str, Any], *, source: str = "") -> dict[str, Any]:
    """``candidate``'s verdicts compared with ``baseline``'s, case by case (see the module).

    ``significance`` is the exact McNemar comparison of the pairs' verdicts, None without a pair;
    ``label`` its label. ``regressed`` lists each case that went from pass to fail with the reason
    each of its failed checks gave (None when it is not kept), and ``improved`` the cases that went
    from fail to pass. ``security_regressed`` lists each paired case that newly failed one of the
    candidate's ``security`` checks, with the reason of each such check, and fails the gate.
    ``new``, ``changed`` and ``unverified`` are the cases with no pair: not in the baseline, with
    other inputs or expectations, or without a verdict in either run; ``missing`` the baseline's
    cases this run did not have; ``unpaired_failures`` the cases with no pair that failed, which
    fail the gate. ``checks`` and ``attempts`` say what changed in the suite itself.
    """
    before = {case["id"]: case for case in baseline["cases"]}
    ran = {case["id"] for case in candidate["cases"]}
    security: Collection[str] = candidate["security"]
    old: list[bool] = []
    now: list[bool] = []
    regressed: list[dict[str, Any]] = []
    improved: list[str] = []
    leaked: list[dict[str, Any]] = []
    unpaired: dict[str, list[str]] = {"new": [], "changed": [], "unverified": []}
    failures: list[str] = []
    for case in candidate["cases"]:
        earlier = before.get(case["id"])
        if earlier is None:
            unpaired["new"].append(case["id"])
        elif earlier["digest"] != case["digest"]:
            unpaired["changed"].append(case["id"])
        elif earlier["passed"] is None or case["passed"] is None:
            unpaired["unverified"].append(case["id"])
        else:
            old.append(earlier["passed"])
            now.append(case["passed"])
            if earlier["passed"] and not case["passed"]:
                failed = {
                    label: check.get("reason") for label, check in case["checks"].items() if check["passed"] is False
                }
                regressed.append({"id": case["id"], "checks": failed})
            elif case["passed"] and not earlier["passed"]:
                improved.append(case["id"])
            # Check by check: a case that already failed another check can still newly leak.
            newly = {
                label: check.get("reason")
                for label, check in case["checks"].items()
                if label in security
                and check["passed"] is False
                and earlier["checks"].get(label, {}).get("passed") is not False
            }
            if newly:
                leaked.append({"id": case["id"], "checks": newly})
            continue
        if case["passed"] is False:
            failures.append(case["id"])
    found = compare_outcomes(old, now)
    label = None if found is None else found.label
    earlier_checks: Mapping[str, str] = baseline["checks"]
    checks: Mapping[str, str] = candidate["checks"]
    return {
        "baseline": source,
        "label": label,
        "significance": None if found is None else asdict(found),
        "regressed": regressed,
        "improved": improved,
        "security_regressed": leaked,
        **unpaired,
        "missing": [case_id for case_id in before if case_id not in ran],
        "unpaired_failures": failures,
        "checks": {
            "added": [name for name in checks if name not in earlier_checks],
            "removed": [name for name in earlier_checks if name not in checks],
            "changed": [name for name in checks if name in earlier_checks and earlier_checks[name] != checks[name]],
        },
        "attempts": {"baseline": baseline.get("attempts"), "candidate": candidate.get("attempts")},
        "gate": "failed" if label == "worse" or leaked or failures else "passed",
    }


__all__ = [
    "COMPARISON",
    "FORMAT",
    "MATRIX_FORMAT",
    "PLAIN_REASONS",
    "VERDICTS",
    "BaselineError",
    "case_digest",
    "compare",
    "load",
    "matrix_verdicts",
    "result",
    "verdicts",
]
