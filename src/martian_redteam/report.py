"""GitHub-flavored Markdown reports for runtime risk assessments."""

from __future__ import annotations

import json
import os
from pathlib import Path

from martian_redteam.types import RiskAssessment, TestCase

_DETAIL_LIMIT = 240


def _truncate(text: str, limit: int = _DETAIL_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _cell(value: object) -> str:
    if value is None:
        text = "-"
    else:
        text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        if not text:
            text = "-"
    return _truncate(text).replace("|", "\\|")


def _table(headers: list[str], rows: list[list[object]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    divider = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]
    return "\n".join([header, divider, *body])


def _probe_detail(case: TestCase) -> str:
    if case.error:
        return case.error
    if case.reason:
        return case.reason
    return "-"


def _executed(value: bool | None) -> str:
    return "requested" if value is None else ("ran" if value else "not run")


def _tool_calls(cases: list[TestCase]) -> str | None:
    """The tool calls of ``cases``, one row each, or None when they made none."""
    rows: list[list[object]] = [
        [
            f"{case.vulnerability}/{case.vulnerability_type}",
            call.name,
            json.dumps(call.arguments, ensure_ascii=False, sort_keys=True) if call.arguments else "-",
            _executed(call.executed),
        ]
        for case in cases
        for turn in case.turns
        for call in turn.tool_calls
    ]
    return _table(["Vulnerability", "Tool", "Arguments", "Call"], rows) if rows else None


def render_markdown(assessment: RiskAssessment) -> str:
    """GitHub-flavored markdown: overview table, error counts, failed probes and their tool calls."""
    overview = _table(
        ["Field", "Value"],
        [
            ["catalog_version", assessment.catalog_version],
            ["graph_revision", assessment.graph_revision],
            ["purpose", assessment.purpose],
            ["duration", f"{assessment.run_duration_seconds:.2f}s"],
            ["total_cases", len(assessment.test_cases)],
            ["infrastructure_errors", assessment.infrastructure_errors],
            ["target_errors", assessment.target_errors],
        ],
    )
    results = _table(
        ["Vulnerability", "Type", "Pass rate", "Pass", "Fail", "Errors", "Gated"],
        [
            [
                result.vulnerability,
                result.vulnerability_type,
                f"{result.pass_rate:.2f}",
                result.passing,
                result.failing,
                result.errored,
                "yes" if result.gated else "no",
            ]
            for result in assessment.vulnerability_type_results
        ],
    )
    failed = [case for case in assessment.test_cases if case.score == 0 or case.error]
    if failed:
        probes: str = _table(
            ["Vulnerability", "Attack", "Score", "Detail"],
            [
                [
                    f"{case.vulnerability}/{case.vulnerability_type}",
                    case.attack_method,
                    "-" if case.score is None else case.score,
                    _probe_detail(case),
                ]
                for case in failed
            ],
        )
    else:
        probes = "No failed or errored probes."
    calls = _tool_calls(failed)

    return "\n".join(
        [
            "# Martian red-team assessment",
            "",
            overview,
            "",
            "## Vulnerability types",
            "",
            results,
            "",
            "## Failed or errored probes",
            "",
            probes,
            "",
            *(["## Tool calls of failed or errored probes", "", calls, ""] if calls else []),
        ]
    )


def write_markdown(assessment: RiskAssessment, directory: Path) -> Path:
    """Write directory/risk-assessment.md."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "risk-assessment.md"
    path.write_text(render_markdown(assessment), encoding="utf-8")
    return path


def append_github_summary(assessment: RiskAssessment) -> None:
    """If env GITHUB_STEP_SUMMARY is a writable path, append markdown. No-op otherwise."""
    raw = os.environ.get("GITHUB_STEP_SUMMARY")
    if not raw:
        return
    try:
        with Path(raw).open("a", encoding="utf-8") as handle:
            handle.write(render_markdown(assessment))
    except OSError:
        return
