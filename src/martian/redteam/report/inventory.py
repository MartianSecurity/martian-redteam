"""Share a filtered findings inventory without transcripts or reviewer notes."""

from __future__ import annotations

import csv
import io
import json
from typing import TYPE_CHECKING, Any

from martian._version import __version__
from martian.redteam.report.guardrails import findings_colang
from martian.redteam.report.sarif import INFORMATION_URI, SARIF_SCHEMA
from martian.redteam.report.tables import spreadsheet_cell

if TYPE_CHECKING:
    from martian.redteam.findings import FindingExport, InventoryFinding


def render_findings(rows: list[InventoryFinding], format: FindingExport) -> bytes:
    if format == "csv":
        return findings_csv(rows).encode("utf-8")
    if format == "sarif":
        return json.dumps(findings_sarif(rows), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    return findings_colang(rows, "1.0" if format == "colang-v1" else "2.x")


def findings_csv(rows: list[InventoryFinding]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        (
            "finding_id",
            "target",
            "category",
            "vulnerability",
            "vulnerability_type",
            "severity",
            "status",
            "evidence",
            "evidence_level",
            "scans",
            "observations",
            "first_seen",
            "last_seen",
            "seen_after_fix",
            "remediation",
            "title",
        )
    )
    for row in rows:
        writer.writerow(
            spreadsheet_cell(value)
            for value in (
                row.id,
                row.target.name,
                row.category,
                row.vulnerability,
                row.vulnerability_type,
                row.severity,
                row.status,
                row.counted_as,
                int(row.evidence_level),
                row.scan_count,
                row.observation_count,
                row.first_seen.isoformat(),
                row.last_seen.isoformat(),
                row.seen_after_fix,
                row.remediation,
                row.title,
            )
        )
    return buffer.getvalue()


def findings_sarif(rows: list[InventoryFinding]) -> dict[str, Any]:
    rules: dict[tuple[str, str, str], int] = {}
    descriptions: list[dict[str, Any]] = []
    results = []
    for row in rows:
        rule = (row.vulnerability, row.vulnerability_type, row.objective_class)
        rule_id = "/".join(rule)
        if rule not in rules:
            rules[rule] = len(descriptions)
            descriptions.append(
                {
                    "id": rule_id,
                    "shortDescription": {"text": row.title},
                    "help": {"text": row.remediation},
                    "properties": {"tags": ["security", "red-team", row.category]},
                }
            )
        results.append(
            {
                "ruleId": rule_id,
                "ruleIndex": rules[rule],
                "kind": "fail" if row.counted_as == "confirmed" else "review",
                "level": "error" if row.counted_as == "confirmed" else "none",
                "message": {"text": f"{row.title} ({row.vulnerability_type}). {row.evidence_label}."},
                "partialFingerprints": {"martianFinding/v1": row.id},
                "properties": {
                    "target": row.target.name,
                    "severity": row.severity,
                    "status": row.status,
                    "counted_as": row.counted_as,
                    "evidence_level": int(row.evidence_level),
                    "scans": row.scan_count,
                    "seen_after_fix": row.seen_after_fix,
                },
            }
        )
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Martian Red Team Findings",
                        "version": __version__,
                        "informationUri": INFORMATION_URI,
                        "rules": descriptions,
                    }
                },
                "results": results,
            }
        ],
    }
