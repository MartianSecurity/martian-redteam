"""``redteam-report.md``: the report standard as Markdown, for a terminal, a job summary or a file.

It states what attacked and with what budget, success per attempt and within k target queries with
their intervals, the attacker's refusals, outcomes with the undetermined, stopped and error counts,
findings by evidence level with what verified them, controls, each probe, and the gate's result when
the run applied one.
"""

from __future__ import annotations

from collections.abc import Sequence

from martian.redteam.model import RedteamRun
from martian.redteam.report.standard import RedteamReport, build_report, rate_of


def cell(value: object) -> str:
    text = "-" if value is None or value == "" else " ".join(str(value).split())
    return (text if len(text) <= 160 else text[:157] + "...").replace("|", "\\|")


def table(headers: list[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    return lines + ["| " + " | ".join(cell(item) for item in row) + " |" for row in rows]


def models_line(report: RedteamReport) -> str:
    models = [
        f"{role} `{info.model}`" for role, info in (("attacker", report.attacker), ("judge", report.judge)) if info
    ]
    return f"- Service: {report.service}" + (f" ({', '.join(models)})" if models else "")


def spent_line(report: RedteamReport) -> str:
    spent = report.budget.spent
    cost = spent.get("cost_usd")
    return "- Spent: {} target queries, {} attacker calls, {} judge calls, {} tokens{}".format(
        spent.get("target_queries", 0),
        spent.get("attacker_calls", 0),
        spent.get("judge_calls", 0),
        spent.get("input_tokens", 0) + spent.get("output_tokens", 0),
        f", {cost:.4f} USD" if cost else "",
    )


def render_markdown(run: RedteamRun, report: RedteamReport | None = None) -> str:
    report = report or build_report(run)
    lines = [f"# Red team run {run.id}", "", models_line(report)]
    if report.target is not None:
        lines.append(f"- Target: {report.target.kind} `{report.target.name}`")
    lines.append(f"- State: {run.state}; catalogue {run.catalog_version}; rules {run.policy_revision}")
    if report.methods:
        lines.append("- Attack methods: " + ", ".join(f"`{method}`" for method in report.methods))
    lines.append(spent_line(report))
    budget = report.budget
    if budget.limits:
        lines.append("- Budget: " + ", ".join(f"{name} {value}" for name, value in budget.limits.items()))
    if budget.attempts_per_probe is not None:
        lines.append(f"- Attempts per probe: {budget.attempts_per_probe}")
    if budget.stopped_by:
        lines.append(f"- **Stopped by the budget: {budget.stopped_by} ran out.**")
    refusals = report.refusals
    lines.append(
        f"- Attacker refusals: {refusals.refusals} of {refusals.generations} generations refused or empty"
        + ("" if refusals.rate.rate is None else f" ({refusals.rate.rate:.0%})")
        + ". A refused generation is never counted as held."
    )
    if report.gate is not None:
        lines += ["", "## Gate", ""]
        lines.append("Passed." if report.gate.passed else "**Failed.**")
        lines += [f"- Fails: {item}" for item in report.gate.failures]
        lines += [f"- Warns: {item}" for item in report.gate.warnings]

    outcomes = report.outcomes
    lines += ["", "## Outcomes", ""]
    lines += table(
        ["Attempts", "Vulnerable", "Held", "Rate over decided", "Undetermined", "Stopped", "Errors"],
        [
            [
                outcomes.attempts,
                outcomes.hits,
                outcomes.resisted,
                rate_of(outcomes.hits, outcomes.decided).display,
                outcomes.undetermined,
                outcomes.stopped,
                outcomes.errors,
            ]
        ],
    )
    if outcomes.errors:
        kinds = outcomes.errors_by_kind
        lines += [
            "",
            f"Errors: {kinds.target} target, {kinds.attacker} attacker, "
            f"{kinds.judge} judge, {kinds.infrastructure} infrastructure.",
        ]

    success = report.success
    lines += ["", "## Success", ""]
    lines += table(
        ["Measure", "Confirmed (level 2 and above)", "Suspected (judge only)"],
        [["Per attempt", success.per_attempt_confirmed.display, success.per_attempt_suspected.display]]
        + [
            [f"Within {point.k} target queries per probe", point.confirmed.display, point.suspected.display]
            for point in success.within_k
        ],
    )
    lines += [
        "",
        "Rates are over decided attempts (or probes) with a 95% Wilson interval. With no success the "
        "report gives the interval's upper bound instead of a rate of zero.",
    ]
    if success.queries_to_first_confirmed_max is not None:
        lines.append(
            f"Target queries to the first confirmed hit: median {success.queries_to_first_confirmed_median:g}, "
            f"most {success.queries_to_first_confirmed_max}."
        )

    findings = report.findings
    lines += ["", "## Findings", ""]
    lines.append(f"{findings.confirmed} confirmed (level 2 and above), {findings.suspected} suspected (judge only).")
    lines.append("")
    lines += table(
        ["Level", "Label", "Counted as", "Findings"],
        [[item.level, item.label, item.counted_as, item.findings] for item in findings.by_level],
    )
    if findings.items:
        lines.append("")
        lines += table(
            [
                "Vulnerability",
                "Type",
                "Attack",
                "Label",
                "Level",
                "Severity",
                "Counted as",
                "Verification",
                "Reproduced",
                "Control",
            ],
            [
                [
                    item.vulnerability,
                    item.vulnerability_type,
                    item.attack_method,
                    item.label,
                    item.evidence_level,
                    item.severity,
                    item.counted_as,
                    item.verification,
                    item.reproductions,
                    item.negative_control,
                ]
                for item in findings.items
            ],
        )
    lines += ["", "## Verification and controls", ""]
    verification, controls = report.verification, report.controls
    if verification.evaluated:
        lines.append(
            f"- Verification: {verification.verified} verified, {verification.consistent} consistent, "
            f"{verification.not_reproducible} not reproducible, {verification.unverifiable} unverifiable."
        )
    else:
        lines.append(f"- Verification: {verification.note}")
    if controls.evaluated:
        lines.append(
            f"- Negative controls: {controls.passed} passed, {controls.failed} failed, "
            f"{controls.undetermined} undetermined of {controls.ran}."
        )
    else:
        lines.append(f"- Negative controls: {controls.note}")

    if any(item.generations for item in refusals.by_method):
        lines += ["", "## Attacker refusals by method", ""]
        lines += table(
            ["Attack method", "Generations", "Refused or empty", "Rate"],
            [[item.method, item.generations, item.refusals, item.rate.display] for item in refusals.by_method],
        )
    lines += ["", "## By attack method", ""]
    lines += table(
        [
            "Attack method",
            "Arm",
            "Attempts",
            "Decided",
            "Confirmed",
            "Suspected",
            "Undetermined",
            "Stopped",
            "Errors",
            "Target queries",
            "Median queries to first hit",
        ],
        [
            [
                group.name,
                group.arm,
                group.attempts,
                group.decided,
                group.confirmed.display,
                group.suspected.display,
                group.undetermined,
                group.stopped,
                group.errors,
                group.target_queries,
                group.queries_to_first_hit_median,
            ]
            for group in report.by_method
        ],
    )
    if any(group.arm == "control" for group in report.by_method) and any(
        group.arm == "adaptive" for group in report.by_method
    ):
        lines += [
            "",
            "The control arm sends the same fixed templates every run; read each adaptive method beside it.",
        ]
    lines += ["", "## Probes", ""]
    lines += table(
        ["Vulnerability", "Type", "Attack", "Checks", "Vulnerable", "Held", "Undetermined", "Stopped", "Errors"],
        [
            [
                row.vulnerability,
                row.vulnerability_type,
                row.attack_method,
                ", ".join(check.id for check in run.probe(row.id).checks),
                row.hits,
                row.resisted,
                row.undetermined,
                row.stopped,
                row.errors,
            ]
            for row in report.probes
        ],
    )
    if report.limitations:
        lines += ["", "## Limitations", ""] + [f"- {item}" for item in report.limitations]
    return "\n".join(lines) + "\n"


__all__ = ["cell", "models_line", "render_markdown", "spent_line", "table"]
