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
  worse beyond noise (``worse``), or when a case that the baseline has no pair for failed: a new
  case, one whose inputs or expectations changed, or one the baseline has no verdict for. A case
  that failed in both runs, and cases that went from pass to fail within noise, do not fail it;
  the comparison names every case that went from pass to fail. A case without a verdict fails
  the run as it does without a baseline.
- **Reasons.** A failed check's reason is kept only when the SDK wrote it without quoting the
  reply: the deterministic checks in :data:`PLAIN_REASONS`, which report counts, sizes or the
  names the suite gives, and with repeats every check, whose reason is then how its attempts
  went under its rule. An LLM judge's reason, and a numeric, table or schema check's, which
  quote the reply's figures or keys, are left out. A case's error is the SDK's own wording of
  how the target failed, as in the pull-request comment.
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
    attempts: int,
) -> dict[str, Any]:
    """The verdicts file of a run: ``records`` are its results.jsonl records in case order,
    ``digests`` each case's :func:`case_digest`, ``checks`` each check's label with a digest of its
    definition, ``plain`` the labels whose reasons are kept, and ``attempts`` the attempts of each
    case."""
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
    return {"format": FORMAT, "attempts": attempts, "checks": dict(checks), "cases": cases}


def load(folder: str | Path) -> dict[str, Any]:
    """The verdicts file in ``folder``, an earlier run's output folder. Raises :class:`BaselineError`."""
    path = Path(folder) / VERDICTS
    if not path.is_file():
        raise BaselineError(
            f"the baseline folder has no {VERDICTS}: give the output folder of an earlier run of this suite"
        )
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise BaselineError(f"the baseline's {VERDICTS} could not be read") from None
    if not isinstance(found, dict) or found.get("format") != FORMAT:
        raise BaselineError(f"the baseline's {VERDICTS} is not a {FORMAT} file")
    cases = found.get("cases")
    if (
        not isinstance(cases, list)
        or not isinstance(found.get("checks"), dict)
        or not all(
            isinstance(case, dict)
            and isinstance(case.get("id"), str)
            and isinstance(case.get("digest"), str)
            and case.get("passed") in (True, False, None)
            and isinstance(case.get("checks"), dict)
            for case in cases
        )
    ):
        raise BaselineError(f"the baseline's {VERDICTS} is not a {FORMAT} file")
    return found


def compare(baseline: Mapping[str, Any], candidate: Mapping[str, Any], *, source: str = "") -> dict[str, Any]:
    """``candidate``'s verdicts compared with ``baseline``'s, case by case (see the module).

    ``significance`` is the exact McNemar comparison of the pairs' verdicts, None without a pair;
    ``label`` its label. ``regressed`` lists each case that went from pass to fail with the reason
    each of its failed checks gave (None when it is not kept), and ``improved`` the cases that went
    from fail to pass. ``new``, ``changed`` and ``unverified`` are the cases with no pair: not in the
    baseline, with other inputs or expectations, or without a verdict in either run; ``missing``
    the baseline's cases this run did not have; ``unpaired_failures`` the cases with no pair that
    failed, which fail the gate. ``checks`` and ``attempts`` say what changed in the suite itself.
    """
    before = {case["id"]: case for case in baseline["cases"]}
    ran = {case["id"] for case in candidate["cases"]}
    old: list[bool] = []
    now: list[bool] = []
    regressed: list[dict[str, Any]] = []
    improved: list[str] = []
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
        **unpaired,
        "missing": [case_id for case_id in before if case_id not in ran],
        "unpaired_failures": failures,
        "checks": {
            "added": [name for name in checks if name not in earlier_checks],
            "removed": [name for name in earlier_checks if name not in checks],
            "changed": [name for name in checks if name in earlier_checks and earlier_checks[name] != checks[name]],
        },
        "attempts": {"baseline": baseline.get("attempts"), "candidate": candidate.get("attempts")},
        "gate": "failed" if label == "worse" or failures else "passed",
    }


__all__ = [
    "COMPARISON",
    "FORMAT",
    "PLAIN_REASONS",
    "VERDICTS",
    "BaselineError",
    "case_digest",
    "compare",
    "load",
    "verdicts",
]
