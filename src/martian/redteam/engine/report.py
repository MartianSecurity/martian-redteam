"""The reports of a version 2 run: ``redteam-run.json`` (the run, as :class:`~martian.redteam.model.RedteamRun`),
``redteam-report.json`` (the report standard, :mod:`martian.redteam.report`) and ``redteam-report.md``.

As in version 0.2, a report holds digests of the target's content (the attacks, replies, judge
reasons and tool arguments), not the content, unless ``MARTIAN_REPORT_INCLUDE_CONTENT=1``; credentials
are always redacted (:func:`for_report`). The run kept in the local store is the same copy.

The report standard and the Markdown are computed from the copy ``redteam-run.json`` holds, so they show no
more than it does.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from martian._privacy import RedactionPolicy, redact_data, report_text
from martian.redteam.model import RedteamRun

if TYPE_CHECKING:
    from martian.redteam.report import GateOutcome

JSON_NAME = "redteam-run.json"
REPORT_NAME = "redteam-report.json"
MARKDOWN_NAME = "redteam-report.md"
_INPUTS, _OUTPUTS = "MARTIAN_EVALS_REDACT_INPUTS", "MARTIAN_EVALS_REDACT_OUTPUTS"


_DIGEST = re.compile(r"^sha256:[0-9a-f]{16}$")


def _text(value: str | None, flag: str, include: bool, policy: RedactionPolicy) -> str | None:
    if value is None or _DIGEST.match(value):
        # A digest already: an attempt kept by an earlier session of a resumed run.
        return value
    return report_text(value, include_content=include, redact_env=flag, policy=policy)


def _arguments(value: Any, include: bool, policy: RedactionPolicy) -> Any:
    if isinstance(value, str):
        return _text(value, _OUTPUTS, include, policy)
    if isinstance(value, list):
        return [_arguments(item, include, policy) for item in value]
    if isinstance(value, dict):
        return {key: _arguments(item, include, policy) for key, item in value.items()}
    return value


def for_report(run: RedteamRun, *, include_content: bool = False, policy: RedactionPolicy | None = None) -> RedteamRun:
    """A copy safe to write and keep: content as digests unless asked for, credentials redacted."""
    policy = policy or RedactionPolicy()
    include = include_content
    data = redact_data(run.model_dump(mode="json"), policy=policy)
    data["purpose"] = _text(run.purpose, _INPUTS, include, policy)
    if data.get("target"):
        data["target"]["purpose"] = _text(run.target.purpose if run.target else "", _INPUTS, include, policy) or ""
    for attempt in data["attempts"]:
        attempt["input"] = _text(attempt["input"], _INPUTS, include, policy)
        attempt["output"] = _text(attempt["output"], _OUTPUTS, include, policy)
        for turn in attempt["turns"]:
            flag = _INPUTS if turn["role"] == "attacker" else _OUTPUTS
            turn["content"] = _text(turn["content"], flag, include, policy)
            for call in turn["tool_calls"]:
                call["arguments"] = _arguments(call["arguments"], include, policy)
                call["result"] = _text(call["result"], _OUTPUTS, include, policy)
        for result in attempt["check_results"]:
            if result["kind"] == "judge":
                result["rationale"] = _text(result["rationale"], _OUTPUTS, include, policy)
    for item in data["evidence"]:
        item["content"] = _text(item["content"], _OUTPUTS, include, policy)
    return RedteamRun.model_validate(data)


def refusals_by_method(run: RedteamRun) -> dict[str, tuple[int, int]]:
    """Each attack method's refused or empty generations and all its generations."""
    methods = {probe.id: probe.attack_method for probe in run.probes}
    found: dict[str, tuple[int, int]] = {}
    for attempt in run.attempts:
        method = methods[attempt.probe_id]
        refused, total = found.get(method, (0, 0))
        generations = attempt.spend.attacker_calls if attempt.spend is not None else 0
        found[method] = (refused + attempt.attacker_refusals, total + generations)
    return found


def render_markdown(run: RedteamRun) -> str:
    """``redteam-report.md`` of ``run`` (:func:`martian.redteam.report.render_markdown`)."""
    from martian.redteam.report import render_markdown as render

    return render(run)


def write_reports(
    run: RedteamRun,
    folder: str | Path,
    *,
    include_content: bool = False,
    policy: RedactionPolicy | None = None,
    gate: GateOutcome | None = None,
) -> tuple[Path, Path, RedteamRun]:
    """Write ``redteam-run.json``, ``redteam-report.json`` (the report standard, with ``gate`` when
    given) and ``redteam-report.md`` to ``folder``; return the paths of the run and the Markdown and
    the copy they hold."""
    from martian.redteam.report import build_report
    from martian.redteam.report import render_markdown as render

    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    report = for_report(run, include_content=include_content, policy=policy)
    data = directory / JSON_NAME
    data.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    standard = build_report(report, gate=gate)
    (directory / REPORT_NAME).write_text(standard.model_dump_json(indent=2), encoding="utf-8")
    text = render(report, standard)
    markdown = directory / MARKDOWN_NAME
    markdown.write_text(text, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        try:
            with Path(summary).open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass
    return data, markdown, report


__all__ = [
    "JSON_NAME",
    "MARKDOWN_NAME",
    "REPORT_NAME",
    "for_report",
    "refusals_by_method",
    "render_markdown",
    "write_reports",
]
