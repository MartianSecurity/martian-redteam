"""Offline remediation reports derived from one saved scan."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from martian.redteam.catalog import find_entry
from martian.redteam.model import Finding, Probe, RedteamRun, RunState
from martian.redteam.prompt_hardening import PromptControl, prompt_controls
from martian.redteam.report.guidance import EVIDENCE_RANK, GUIDANCE, RISK_RANK, finding_title
from martian_redteam.catalog.entries import Severity

REPORT_VERSION: Literal["1"] = "1"
JSON_NAME = "redteam-remediation.json"
HTML_NAME = "redteam-remediation.html"


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RemediationFinding(_Out):
    finding_id: str
    title: str
    vulnerability: str
    vulnerability_type: str
    category: str
    severity: Severity | None
    counted_as: Literal["confirmed", "attempted", "suspected"]
    evidence_level: int
    evidence_label: str
    hit_attempts: int
    example_attempt_id: str | None
    method: str


class RemediationAction(_Out):
    priority: int
    category: str
    title: str
    confirmed_severity: Severity | None
    effort: Literal["low", "medium", "high"]
    steps: tuple[str, ...]
    example: str
    finding_ids: list[str]


class RemediationReport(_Out):
    version: Literal["1"] = REPORT_VERSION
    project_id: str
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    state: RunState
    incomplete: bool
    overall_risk: Severity | None
    confirmed: int
    attempted: int
    suspected: int
    findings: list[RemediationFinding]
    actions: list[RemediationAction]
    prompt_controls: list[PromptControl] = Field(default_factory=list)


class ScanFindingSummary(_Out):
    overall_risk: Severity | None
    confirmed: int
    attempted: int
    suspected: int


def summarize_findings(findings: list[Finding]) -> ScanFindingSummary:
    counts = Counter(row.counted_as for row in findings)
    severity = max(
        (row.severity for row in findings if row.counted_as == "confirmed" and row.severity is not None),
        default=None,
        key=RISK_RANK.__getitem__,
    )
    return ScanFindingSummary(
        overall_risk=severity,
        confirmed=counts["confirmed"],
        attempted=counts["attempted"],
        suspected=counts["suspected"],
    )


def finding_priority(finding: Finding) -> tuple[int, int, int, str]:
    return (
        -EVIDENCE_RANK[finding.counted_as],
        -RISK_RANK.get(finding.severity or "", 0),
        -int(finding.evidence_level),
        finding.id,
    )


def describe_finding(finding: Finding, probe: Probe) -> RemediationFinding:
    catalog = find_entry(finding.vulnerability)
    source = probe.metadata.get("promptfoo")
    policy_name = source.get("policy_name") if isinstance(source, dict) else None
    return RemediationFinding(
        finding_id=finding.id,
        title=finding_title(finding.vulnerability, catalog.label if catalog else None, policy_name),
        vulnerability=catalog.id if catalog else finding.vulnerability,
        vulnerability_type=finding.vulnerability_type,
        category=catalog.group if catalog else "custom",
        severity=finding.severity,
        counted_as=finding.counted_as,
        evidence_level=int(finding.evidence_level),
        evidence_label=finding.label,
        hit_attempts=len(finding.attempt_ids),
        example_attempt_id=next(iter(finding.attempt_ids), None),
        method=probe.attack_method,
    )


def build_remediation(run: RedteamRun) -> RemediationReport:
    """Map recorded findings to ranked defensive actions. No scan content is sent to a model."""
    findings: list[RemediationFinding] = []
    by_category: dict[str, list[RemediationFinding]] = defaultdict(list)
    probes = {probe.id: probe for probe in run.probes}
    for finding in sorted(run.findings, key=finding_priority):
        row = describe_finding(finding, probes[finding.probe_id])
        findings.append(row)
        by_category[row.category].append(row)
    actions = []
    for priority, (action_category, rows) in enumerate(by_category.items(), 1):
        guidance = GUIDANCE[action_category]
        actions.append(
            RemediationAction(
                priority=priority,
                category=action_category,
                title=guidance.title,
                confirmed_severity=next(
                    (row.severity for row in rows if row.counted_as == "confirmed" and row.severity), None
                ),
                effort=guidance.effort,
                steps=guidance.steps,
                example=guidance.example,
                finding_ids=[row.finding_id for row in rows],
            )
        )
    summary = summarize_findings(run.findings)
    return RemediationReport(
        project_id=run.project_id,
        run_id=run.id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        state=run.state,
        incomplete=run.state != "completed"
        or (run.expected_attempts is not None and len(run.attempts) < run.expected_attempts)
        or any(attempt.outcome in ("undetermined", "error", "stopped") for attempt in run.attempts),
        overall_risk=summary.overall_risk,
        confirmed=summary.confirmed,
        attempted=summary.attempted,
        suspected=summary.suspected,
        findings=findings,
        actions=actions,
        prompt_controls=prompt_controls(run.findings),
    )


def write_remediation_reports(run: RedteamRun, folder: Path) -> tuple[Path, ...]:
    """Write the finished run's plan from its report copy and return the artifact paths."""
    if run.state in ("queued", "running"):
        return ()
    report = build_remediation(run)
    outputs = {JSON_NAME: report.model_dump_json(indent=2) + "\n", HTML_NAME: render_remediation_print(report)}
    paths = tuple(folder / name for name in outputs)
    with TemporaryDirectory(dir=folder, prefix=".remediation-") as staging:
        root = Path(staging)
        for name, content in outputs.items():
            (root / name).write_text(content, encoding="utf-8")
        for destination in paths:
            (root / destination.name).replace(destination)
    return paths


def render_remediation_print(report: RemediationReport, *, link_attempts: bool = False) -> str:
    """A self-contained document for browser printing or saving as PDF."""
    risk = report.overall_risk.title() if report.overall_risk else "Unrated"
    scope = urlencode({"project": report.project_id, "run": report.run_id})
    actions = {action.category: action.priority for action in report.actions}
    rows = []
    for row in report.findings:
        detail = escape(row.evidence_label)
        if row.example_attempt_id and link_attempts:
            query = escape(f"{scope}&{urlencode({'attempt': row.example_attempt_id})}", quote=True)
            detail = f'<a href="/#/redteam/attempt?{query}">{detail}</a>'
        elif row.example_attempt_id:
            detail += f"<br>Attempt {escape(row.example_attempt_id)}"
        rows.append(
            f"<tr><td>{escape(row.title)}<br>{escape(row.vulnerability_type)}</td>"
            f"<td>{escape(row.severity or 'Unrated')}</td>"
            f"<td>{detail}, level {row.evidence_level} ({row.hit_attempts} hits)</td>"
            f"<td>{actions[row.category]}</td></tr>"
        )
    sections = []
    for action in report.actions:
        steps = "".join(f"<li>{escape(step)}</li>" for step in action.steps)
        example = f"<pre>{escape(action.example)}</pre>" if action.example else ""
        sections.append(
            f"<section><h3>{action.priority}. {escape(action.title)}</h3>"
            f"<p>Confirmed severity {escape(action.confirmed_severity or 'unrated')} · "
            f"Typical effort {escape(action.effort)}</p><ol>{steps}</ol>{example}</section>"
        )
    if report.prompt_controls:
        titles = {row.finding_id: f"{row.title} ({row.vulnerability_type})" for row in report.findings}
        sections.append("<h2>System prompt recommendations</h2>")
        for control in report.prompt_controls:
            findings = ", ".join(titles[finding_id] for finding_id in control.finding_ids)
            sections.append(
                f"<section><h3>{escape(control.title)}</h3><p>Related findings · {escape(findings)}</p>"
                f"<pre>{escape(control.instruction)}</pre></section>"
            )
    scan = escape(report.run_id)
    warning = (
        "<p>The scan is incomplete. The evidence below covers recorded findings only.</p>" if report.incomplete else ""
    )
    mapping = (
        "<table><thead><tr><th>Finding</th><th>Severity</th><th>Evidence</th><th>Action</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        if rows
        else "<p>No findings were recorded in this scan.</p>"
    )
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'">
<title>Martian remediation report · {scan}</title>
<style>
body {{ max-width: 52rem; margin: 3rem auto; padding: 0 1rem; font: 16px/1.55 system-ui, sans-serif; }}
table {{ width: 100%; border-collapse: collapse; overflow-wrap: anywhere; }}
th, td {{ padding: .6rem; border-bottom: 1px solid #bbb; text-align: left; vertical-align: top; }}
pre {{ border: 1px solid #bbb; padding: .6rem; white-space: pre-wrap; overflow-wrap: anywhere; }}
@media print {{ body {{ margin: 0; padding: 0; }} .screen {{ display: none; }} tr, h2, h3 {{ break-after: avoid; }} }}
</style>
<p class="screen">Use your browser’s Print command to save this report as a PDF.</p>
<h1>Remediation report</h1><p>Martian Red Team · {scan} · {report.started_at.date().isoformat()}</p>
<h2>Executive summary</h2>
<p>Highest confirmed severity: <strong>{risk}</strong>.
{report.confirmed} confirmed · {report.attempted} attempted · {report.suspected} suspected.</p>{warning}
<h2>Findings and actions</h2>{mapping}
{"".join(sections)}
<p>Verify changes with the same scan configuration and compare the saved results.</p>
</html>"""
