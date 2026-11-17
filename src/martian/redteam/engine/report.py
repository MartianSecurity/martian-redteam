"""The reports of a version 2 run: ``redteam-run.json`` (the run, as :class:`~martian.redteam.model.RedteamRun`)
and ``redteam-report.md``.

As in version 0.2, a report holds digests of the target's content (the attacks, replies, judge
reasons and tool arguments), not the content, unless ``MARTIAN_REPORT_INCLUDE_CONTENT=1``; credentials
are always redacted (:func:`for_report`). The run kept in the local store is the same copy.

The Markdown states the service and its models, the budget and what was spent, outcomes with the
rate over decided attempts beside the undetermined, stopped and error counts, findings by evidence
level, and each probe's attempts.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from martian._privacy import RedactionPolicy, redact_data, report_text
from martian.redteam.model import RedteamRun

JSON_NAME = "redteam-run.json"
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
        attempt["planted"] = _text(attempt["planted"], _INPUTS, include, policy)
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


def _cell(value: object) -> str:
    text = "-" if value is None or value == "" else " ".join(str(value).split())
    return (text if len(text) <= 160 else text[:157] + "...").replace("|", "\\|")


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    return lines + ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]


def _percent(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "-"


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


def _yes(value: bool | None) -> str:
    return "-" if value is None else "yes" if value else "no"


def _verification(run: RedteamRun) -> list[str]:
    """Each probe's negative control, reproductions and benign task, when the run checked them."""
    if not run.verifications:
        return []
    control = {True: "passed", False: "showed the signal: discarded", None: "not decided"}
    rows: list[list[object]] = []
    for item in run.verifications:
        task = item.task_completion
        rows.append(
            [
                item.probe_id,
                control[item.negative_control_passed] if item.control_attempt_id else "not run",
                f"{item.successful_reproductions} of {item.repetitions}" if item.repetitions else "-",
                item.outcome,
                f"{task.with_attack} of {task.with_attack_decided}" if task else "-",
                _yes(task.without_attack) if task else "-",
            ]
        )
    return [
        "",
        "## Controls and task completion",
        "",
        "The negative control sends the probe without the attack; a hit it shows too is no finding. "
        "The benign task is sent with the attack, and alone in the control.",
        "",
        *_table(
            ["Probe", "Control", "Reproduced", "Outcome", "Task done with the attack", "Task done without it"], rows
        ),
    ]


def _share(value: float | None, interval: list[float] | tuple[float, float] | None) -> str:
    if value is None:
        return "- (nothing to measure it on)"
    low, high = interval or (value, value)
    return f"{value:.0%} (95% interval {low:.0%} to {high:.0%})"


def _calibration(found: dict[str, Any] | None) -> list[str]:
    """The judge's precision and recall on the run's labelled sample, when it was given one."""
    if not found:
        return []
    lines = ["", "## Judge calibration", ""]
    if "error" in found:
        return [*lines, f"The judge `{found['judge']}` could not be measured: {found['error']}"]
    lines.append(
        f"The judge `{found['judge']}` on {found['sample']} labelled items: precision "
        f"{_share(found['precision'], found['precision_interval'])}, recall "
        f"{_share(found['recall'], found['recall_interval'])}. Its hits stay suspected whatever these are."
    )
    lines.append("")
    lines += _table(
        ["True positive", "False positive", "False negative", "True negative", "Undetermined"],
        [
            [
                found["true_positive"],
                found["false_positive"],
                found["false_negative"],
                found["true_negative"],
                found["undetermined"],
            ]
        ],
    )
    return lines


def render_markdown(run: RedteamRun) -> str:
    counts, provenance, budget = run.counts, run.provenance, run.budget
    lines = [f"# Red team run {run.id}", ""]
    service = provenance.get("service", "unknown")
    models = [f"{role} `{provenance[role]['model']}`" for role in ("attacker", "judge") if role in provenance]
    lines.append(f"- Service: {service}" + (f" ({', '.join(models)})" if models else ""))
    if run.target is not None:
        lines.append(f"- Target: {run.target.kind} `{run.target.name}`")
    lines.append(f"- State: {run.state}; catalogue {run.catalog_version}; rules {run.policy_revision}")
    spent = budget.get("spent", {})
    limits = budget.get("limits", {})
    lines.append(
        "- Spent: {} target queries, {} attacker calls, {} judge calls, {} tokens{}".format(
            spent.get("target_queries", 0),
            spent.get("attacker_calls", 0),
            spent.get("judge_calls", 0),
            spent.get("input_tokens", 0) + spent.get("output_tokens", 0),
            f", {spent['cost_usd']:.4f} USD" if spent.get("cost_usd") else "",
        )
    )
    if limits:
        lines.append("- Budget: " + ", ".join(f"{name} {value}" for name, value in limits.items()))
    if budget.get("stopped_by"):
        lines.append(f"- **Stopped by the budget: {budget['stopped_by']} ran out.**")
    refusal = counts.refusal_rate
    lines.append(
        f"- Attacker refusals: {counts.refusals} of {counts.generations} generations refused or empty"
        + ("" if refusal is None else f" ({refusal:.0%})")
        + ". A refused generation is never counted as held."
    )
    lines += ["", "## Outcomes", ""]
    rate = counts.hit_rate
    lines += _table(
        ["Attempts", "Vulnerable", "Held", "Rate over decided", "Undetermined", "Stopped", "Errors"],
        [
            [
                counts.attempts,
                counts.hits,
                counts.resisted,
                "-" if rate is None else f"{rate:.0%} of {counts.decided}",
                counts.undetermined,
                counts.stopped,
                counts.errors,
            ]
        ],
    )
    if counts.errors:
        lines += [
            "",
            f"Errors: {counts.target_errors} target, {counts.attacker_errors} attacker, "
            f"{counts.judge_errors} judge, {counts.infrastructure_errors} infrastructure.",
        ]
    lines += ["", "## Findings", ""]
    lines.append(
        f"{counts.confirmed} confirmed (level 2 and above), {counts.attempted} attempted actions (a matching "
        f"tool call that is not known to have run), {counts.suspected} suspected (judge only)."
    )
    if run.findings:
        lines.append("")
        lines += _table(
            ["Vulnerability", "Type", "Label", "Level", "Severity", "Counted as"],
            [
                [
                    finding.vulnerability,
                    finding.vulnerability_type,
                    finding.label,
                    int(finding.evidence_level),
                    finding.severity,
                    finding.counted_as,
                ]
                for finding in run.findings
            ],
        )
    lines += _verification(run)
    lines += _calibration(run.provenance.get("judge_calibration"))
    by_method = refusals_by_method(run)
    if any(generations for _, generations in by_method.values()):
        lines += ["", "## Attacker refusals by method", ""]
        lines += _table(
            ["Attack method", "Generations", "Refused or empty", "Rate"],
            [[method, total, refused, _percent(refused, total)] for method, (refused, total) in by_method.items()],
        )
    lines += ["", "## Probes", ""]
    rows: list[list[object]] = []
    for probe in run.probes:
        outcomes = Counter(attempt.outcome for attempt in run.attempts_of(probe.id))
        rows.append(
            [
                probe.vulnerability,
                probe.vulnerability_type,
                probe.attack_method,
                ", ".join(check.id for check in probe.checks),
                outcomes["hit"],
                outcomes["resisted"],
                outcomes["undetermined"],
                outcomes["stopped"],
                outcomes["error"],
            ]
        )
    lines += _table(
        ["Vulnerability", "Type", "Attack", "Checks", "Vulnerable", "Held", "Undetermined", "Stopped", "Errors"], rows
    )
    if run.limitations:
        lines += ["", "## Limitations", ""] + [f"- {item}" for item in run.limitations]
    return "\n".join(lines) + "\n"


def write_reports(
    run: RedteamRun, folder: str | Path, *, include_content: bool = False, policy: RedactionPolicy | None = None
) -> tuple[Path, Path, RedteamRun]:
    """Write both reports to ``folder``; return their paths and the copy they hold."""
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    report = for_report(run, include_content=include_content, policy=policy)
    data = directory / JSON_NAME
    data.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    text = render_markdown(report)
    markdown = directory / MARKDOWN_NAME
    markdown.write_text(text, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        try:
            with Path(summary).open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass
    return data, markdown, report


__all__ = ["JSON_NAME", "MARKDOWN_NAME", "for_report", "refusals_by_method", "render_markdown", "write_reports"]
