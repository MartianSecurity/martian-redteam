"""The red team section of the evals action's pull-request comment.

``scripts/evals_pr_comment.py`` adds it when the action ran a red team run file. It reads
``redteam-report.json`` (the report standard, ``docs/redteam-reports.md``) from the run's output
folder and states the gate's result, what attacked, the budget and what was spent, the confirmed
findings by name, the suspected count, success per attempt with its interval, the attacker's refusals,
and the undetermined, stopped and errored attempts. The report holds no target content, so neither
does the comment. Standard library only, like the script that imports it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REPORT_NAME = "redteam-report.json"
MAX_FINDINGS = 25
MAX_LINES = 10


def load(folder: Path) -> dict[str, Any] | None:
    """The report standard the run wrote, or None when it wrote none (it could not start)."""
    path = folder / REPORT_NAME
    if not path.is_file():
        return None
    found: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return found


def _title(report: Mapping[str, Any] | None, exit_code: int) -> str:
    if report is None or exit_code not in (0, 1):
        return "did not finish"
    if exit_code == 1:
        return "failed"
    gate = report.get("gate") or {}
    return "passed with warnings" if gate.get("warnings") else "passed"


def _spent(report: Mapping[str, Any]) -> str:
    budget = report["budget"]
    spent, limits = budget["spent"], budget["limits"]
    queries = spent.get("target_queries", 0)
    parts = [
        f"{queries} of {limits['target_queries']} target queries"
        if limits.get("target_queries")
        else f"{queries} target queries",
        f"{spent.get('attacker_calls', 0) + spent.get('judge_calls', 0)} model calls",
        f"{spent.get('input_tokens', 0) + spent.get('output_tokens', 0)} tokens",
    ]
    if spent.get("cost_usd"):
        parts.append(f"{spent['cost_usd']:.4f} USD")
    text = "Budget: " + ", ".join(parts)
    if budget.get("attempts_per_probe"):
        text += f"; {budget['attempts_per_probe']} attempt(s) per probe"
    return text + "."


def render(
    report: Mapping[str, Any] | None,
    *,
    exit_code: int,
    run_file: str,
    code: Callable[..., str],
) -> list[str]:
    """The section's lines. ``code`` is the comment script's code span, which keeps text inert."""
    lines = [f"### Martian red team {_title(report, exit_code)}", ""]
    if report is None:
        return lines + [
            f"Run file {code(run_file, 120, cell=False)} wrote no report (exit code {exit_code}). "
            "The run could not start; the job log says why.",
            "",
        ]
    outcomes, findings = report["outcomes"], report["findings"]
    lines += [
        f"Run file {code(run_file, 120, cell=False)}: {len(report['probes'])} probes, {outcomes['attempts']} attack "
        f"attempts: {outcomes['hits']} vulnerable, {outcomes['resisted']} held, {outcomes['undetermined']} "
        f"undetermined, {outcomes['stopped']} stopped, {outcomes['errors']} errors.",
        "",
    ]
    attacker, judge = report.get("attacker"), report.get("judge")
    who = [f"attacker {code(attacker['model'], cell=False)}" if attacker else "attacker not recorded"]
    if judge:
        who.append(f"judge {code(judge['model'], cell=False)}")
    methods = ", ".join(code(method, cell=False) for method in report["methods"][:MAX_LINES])
    lines.append(f"Service {code(report['service'], cell=False)}: {', '.join(who)}; methods {methods}.")
    lines.append(_spent(report))
    if report["budget"].get("stopped_by"):
        lines.append(f"**Stopped by the budget: {code(report['budget']['stopped_by'], cell=False)} ran out.**")
    refusals = report["refusals"]
    lines += [
        f"Attacker refusals: {refusals['rate']['display']}. A refused generation is never counted as held.",
        "",
        "| | Confirmed (level 2 and above) | Suspected (judge only) |",
        "| --- | ---: | ---: |",
        f"| Findings | {findings['confirmed']} | {findings['suspected']} |",
        f"| Success per attempt (95% CI) | {report['success']['per_attempt_confirmed']['display']} | "
        f"{report['success']['per_attempt_suspected']['display']} |",
        "",
    ]
    confirmed = [item for item in findings["items"] if item["counted_as"] == "confirmed"]
    if confirmed:
        lines += [
            "#### Confirmed findings",
            "",
            "| Vulnerability | Type | Attack | Label | Level | Severity | Verification |",
            "| --- | --- | --- | --- | ---: | --- | --- |",
        ]
        for item in confirmed[:MAX_FINDINGS]:
            lines.append(
                f"| {code(item['vulnerability'])} | {code(item['vulnerability_type'])} | "
                f"{code(item['attack_method'])} | {item['label']} | {item['evidence_level']} | "
                f"{item['severity'] or '—'} | {item['verification']} |"
            )
        if len(confirmed) > MAX_FINDINGS:
            lines.append(f"\n{len(confirmed) - MAX_FINDINGS} more confirmed findings are in {REPORT_NAME}.")
        lines.append("")
    if findings["suspected"]:
        lines += [
            f"{findings['suspected']} suspected finding(s): a model judged the reply only, so they are not counted "
            "as confirmed.",
            "",
        ]
    kinds = outcomes["errors_by_kind"]
    if outcomes["errors"]:
        lines += [
            f"Errors: {kinds['target']} target, {kinds['attacker']} attacker, {kinds['judge']} judge, "
            f"{kinds['infrastructure']} infrastructure.",
            "",
        ]
    gate = report.get("gate")
    if gate:
        for heading, items in (("Gate failures", gate["failures"]), ("Gate warnings", gate["warnings"])):
            if items:
                lines += [f"{heading}:", ""]
                lines += [f"- {code(item, 160, cell=False)}" for item in items[:MAX_LINES]]
                if len(items) > MAX_LINES:
                    lines.append(f"- {len(items) - MAX_LINES} more in {REPORT_NAME}")
                lines.append("")
    return lines


__all__ = ["MAX_FINDINGS", "REPORT_NAME", "load", "render"]
