"""``redteam.sarif``: the run's findings as SARIF 2.1.0, for code scanning.

One rule per vulnerability and type, and one result per finding: ``error`` for a confirmed finding
(evidence level 2 and above) and ``warning`` for a suspected one. A result's message names the
vulnerability, the attack method, the label and the level; it never holds the target's content.
Given the run file's path, every result points at it, as GitHub code scanning requires a location.
A finding's ``partialFingerprints`` stay the same from run to run, so code scanning follows it.
"""

from __future__ import annotations

import hashlib
from typing import Any

from martian._version import __version__
from martian.redteam.model import RedteamRun, budget_usage_error
from martian.redteam.report.standard import FindingRow, RedteamReport, build_report

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
INFORMATION_URI = "https://github.com/MartianSecurity/martian-redteam"
SECURITY_SEVERITY = {"critical": "9.5", "high": "8.0", "medium": "5.5", "low": "3.0", "info": "1.0"}
"""GitHub code scanning's ``security-severity`` for each run file severity."""


def _rule_id(row: FindingRow) -> str:
    return f"{row.vulnerability}/{row.vulnerability_type}"


def _message(row: FindingRow) -> str:
    return (
        f"{row.vulnerability}/{row.vulnerability_type} via {row.attack_method}: {row.label} "
        f"(evidence level {row.evidence_level}, counted as {row.counted_as}); {row.hits} of {row.attempts} "
        f"attempts hit; verification {row.verification}."
    )


def render_sarif(
    run: RedteamRun, report: RedteamReport | None = None, *, run_file: str | None = None
) -> dict[str, Any]:
    """The SARIF log of ``run``. ``run_file`` is the run file's path, relative to the repository root
    for code scanning."""
    report = report or build_report(run)
    rules: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for row in report.findings.items:
        rule = _rule_id(row)
        if rule in index:
            continue
        index[rule] = len(rules)
        properties: dict[str, Any] = {"tags": ["security", "red-team", row.vulnerability, row.objective_class]}
        if row.severity is not None:
            properties["security-severity"] = SECURITY_SEVERITY[row.severity]
        rules.append(
            {
                "id": rule,
                "name": "".join(part.title() for part in rule.replace("/", "_").split("_")),
                "shortDescription": {"text": f"{row.vulnerability}: {row.vulnerability_type}"},
                "fullDescription": {
                    "text": f"The target was attacked for {row.vulnerability} ({row.vulnerability_type}), "
                    f"a {row.objective_class} objective."
                },
                "helpUri": f"{INFORMATION_URI}/blob/main/docs/redteam-verdicts.md",
                "defaultConfiguration": {"level": "error"},
                "properties": properties,
            }
        )
    results = []
    for row in report.findings.items:
        result: dict[str, Any] = {
            "ruleId": _rule_id(row),
            "ruleIndex": index[_rule_id(row)],
            "kind": "fail",
            "level": "error" if row.counted_as == "confirmed" else "warning",
            "message": {"text": _message(row)},
            "partialFingerprints": {
                "martianRedteamFinding/v1": hashlib.sha256(row.probe_id.encode()).hexdigest(),
            },
            "properties": {
                "finding_id": row.id,
                "probe_id": row.probe_id,
                "attack_method": row.attack_method,
                "objective_class": row.objective_class,
                "evidence_level": row.evidence_level,
                "label": row.label,
                "counted_as": row.counted_as,
                "severity": row.severity,
                "status": row.status,
                "verification": row.verification,
                "reproductions": row.reproductions,
                "negative_control": row.negative_control,
            },
        }
        if run_file:
            result["locations"] = [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": run_file},
                        "region": {"startLine": 1},
                    }
                }
            ]
        results.append(result)
    spent = report.budget.spent
    budget_error = budget_usage_error(run.budget)
    invocation = {
        "executionSuccessful": run.state in ("completed", "stopped") and budget_error is None,
        "properties": {
            "state": run.state,
            "stopped_by": report.budget.stopped_by,
            "target_queries": spent.get("target_queries", 0),
            "attacker": report.attacker.model if report.attacker else None,
            "judge": report.judge.model if report.judge else None,
            **({"budget_error": budget_error} if budget_error is not None else {}),
        },
    }
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Martian Red Team",
                        "version": __version__,
                        "informationUri": INFORMATION_URI,
                        "rules": rules,
                    }
                },
                "automationDetails": {"id": f"martian-redteam/{run.id}"},
                "invocations": [invocation],
                "results": results,
                "properties": {
                    "confirmed": report.findings.confirmed,
                    "suspected": report.findings.suspected,
                    "per_attempt_confirmed": report.success.per_attempt_confirmed.display,
                    "attacker_refusals": report.refusals.rate.display,
                    "undetermined": report.outcomes.undetermined,
                    "errors": report.outcomes.errors,
                },
            }
        ],
    }


__all__ = ["SARIF_SCHEMA", "SECURITY_SEVERITY", "render_sarif"]
