"""``redteam-report.html``: the report standard as one self-contained page.

The page has its styles inline and no script, font, image or link to anything outside it, so it
opens with no network, and its content security policy forbids the browser to load anything else.
Every value is escaped. It shows what the run's copy holds: digests of the target's content unless
the run was asked for the content.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape

from martian.redteam.model import RedteamRun
from martian.redteam.report.standard import Rate, RedteamReport, build_report

CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; form-action 'none'; base-uri 'none'"
"""The page's content security policy: the browser loads nothing beyond the page itself."""

STYLE = """
:root { color-scheme: light; --ink: #1d2433; --muted: #5b6475; --line: #d9dde5; --panel: #f6f7f9;
  --bad: #b42318; --warn: #b54708; --good: #067647; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px; background: #ffffff; color: var(--ink);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 32px 0 8px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
p, li { color: var(--ink); }
.muted { color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 16px 0; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; }
.card .value { font-size: 20px; font-weight: 600; }
.card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.bad { color: var(--bad); } .warn { color: var(--warn); } .good { color: var(--good); }
table { border-collapse: collapse; width: 100%; margin: 8px 0; }
th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: var(--panel); font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
  overflow-wrap: anywhere; }
details { border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; margin: 6px 0; }
summary { cursor: pointer; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: var(--panel); padding: 8px; border-radius: 4px; }
"""


def _e(value: object) -> str:
    return escape("-" if value is None or value == "" else str(value), quote=True)


def _table(headers: Sequence[str], rows: Iterable[Sequence[object]], numeric: Sequence[int] = ()) -> str:
    head = "".join(f"<th>{_e(header)}</th>" for header in headers)
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="num">{_e(item)}</td>' if index in numeric else f"<td>{_e(item)}</td>"
            for index, item in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _card(label: str, value: object, tone: str = "") -> str:
    kind = f' class="value {tone}"' if tone else ' class="value"'
    return f'<div class="card"><div class="label">{_e(label)}</div><div{kind}>{_e(value)}</div></div>'


def _rate(rate: Rate) -> str:
    return rate.display


def render_html(run: RedteamRun, report: RedteamReport | None = None) -> str:
    report = report or build_report(run)
    parts: list[str] = []
    add = parts.append
    add(f"<h1>Red team run {_e(run.id)}</h1>")
    add(
        f'<p class="muted">State {_e(run.state)} · catalogue {_e(run.catalog_version)} · rules '
        f"{_e(run.policy_revision)} · started {_e(run.started_at.isoformat())}</p>"
    )
    findings, outcomes, gate = report.findings, report.outcomes, report.gate
    cards = [
        _card("Confirmed findings", findings.confirmed, "bad" if findings.confirmed else "good"),
        _card("Suspected findings", findings.suspected, "warn" if findings.suspected else ""),
        _card("Confirmed success per attempt", _rate(report.success.per_attempt_confirmed)),
        _card("Attacker refusals", _rate(report.refusals.rate)),
        _card("Undetermined, stopped, errors", f"{outcomes.undetermined}, {outcomes.stopped}, {outcomes.errors}"),
    ]
    if gate is not None:
        cards.insert(0, _card("Gate", "passed" if gate.passed else "failed", "good" if gate.passed else "bad"))
    add(f'<div class="cards">{"".join(cards)}</div>')
    if gate is not None and (gate.failures or gate.warnings):
        items = [f'<li class="bad">Fails: {_e(item)}</li>' for item in gate.failures]
        items += [f'<li class="warn">Warns: {_e(item)}</li>' for item in gate.warnings]
        add(f"<ul>{''.join(items)}</ul>")

    add("<h2>What attacked</h2>")
    budget = report.budget
    spent = budget.spent
    rows: list[list[object]] = [
        ["Service", report.service],
        [
            "Attacker",
            ", ".join(info.model for info in report.attackers)
            if report.attackers
            else (report.attacker.model if report.attacker else "not recorded"),
        ],
        ["Judge", report.judge.model if report.judge else ("not recorded" if report.judge_used else "not used")],
        ["Attack methods", ", ".join(report.methods)],
        ["Target", f"{report.target.kind} {report.target.name}" if report.target else "not recorded"],
        ["Budget", ", ".join(f"{name} {value}" for name, value in budget.limits.items()) or "no limit"],
        ["Attempts per probe", budget.attempts_per_probe],
        [
            "Spent",
            f"{spent.get('target_queries', 0)} target queries, {spent.get('attacker_calls', 0)} attacker calls, "
            f"{spent.get('judge_calls', 0)} judge calls, "
            f"{spent.get('input_tokens', 0) + spent.get('output_tokens', 0)} tokens"
            + (f", {spent['cost_usd']:.4f} USD" if spent.get("cost_usd") else ""),
        ],
        ["Stopped by", budget.stopped_by or "nothing: the run finished"],
    ]
    add(_table(["", ""], rows))

    add("<h2>Success</h2>")
    success = report.success
    attempted = report.outcomes.attempted_hits > 0
    add(
        _table(
            ["Measure", "Confirmed (level 2 and above)", "Suspected (judge only)"]
            + (["Attempted action (level 3, not known to run)"] if attempted else []),
            [
                [
                    "Per attempt",
                    _rate(success.per_attempt_confirmed),
                    _rate(success.per_attempt_suspected),
                    *(
                        [_rate(success.per_attempt_attempted) if success.per_attempt_attempted else ""]
                        if attempted
                        else []
                    ),
                ]
            ]
            + [
                [
                    f"Within {point.k} target queries per probe",
                    _rate(point.confirmed),
                    _rate(point.suspected),
                    *([_rate(point.attempted) if point.attempted else ""] if attempted else []),
                ]
                for point in success.within_k
            ],
        )
    )
    add(
        '<p class="muted">Rates are over decided attempts (or probes) with a 95% Wilson interval. With no '
        "success the report gives the interval's upper bound instead of a rate of zero.</p>"
    )
    add("<h2>Outcomes</h2>")
    kinds = outcomes.errors_by_kind
    add(
        _table(
            ["Attempts", "Decided", "Vulnerable", "Held", "Undetermined", "Stopped", "Errors", "Errors by kind"],
            [
                [
                    outcomes.attempts,
                    outcomes.decided,
                    outcomes.hits,
                    outcomes.resisted,
                    outcomes.undetermined,
                    outcomes.stopped,
                    outcomes.errors,
                    f"{kinds.target} target, {kinds.attacker} attacker, {kinds.judge} judge, "
                    f"{kinds.infrastructure} infrastructure",
                ]
            ],
            numeric=range(7),
        )
    )

    add("<h2>Findings</h2>")
    add(
        _table(
            ["Level", "Label", "Counted as", "Findings"],
            [[item.level, item.label, item.counted_as, item.findings] for item in findings.by_level],
            numeric=(0, 3),
        )
    )
    if findings.items:
        add(
            _table(
                [
                    "Vulnerability",
                    "Type",
                    "Attack",
                    "Label",
                    "Level",
                    "Severity",
                    "Hits",
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
                        f"{item.hits} of {item.attempts}",
                        item.verification,
                        item.reproductions,
                        item.negative_control,
                    ]
                    for item in findings.items
                ],
            )
        )
    else:
        add("<p>No finding.</p>")
    verification, controls = report.verification, report.controls
    add("<h2>Verification and controls</h2><ul>")
    add(
        "<li>Verification: "
        + (
            _e(
                f"{verification.verified} verified, {verification.consistent} consistent, "
                f"{verification.not_reproducible} not reproducible, {verification.unverifiable} unverifiable."
            )
            if verification.evaluated
            else _e(verification.note)
        )
        + "</li>"
    )
    add(
        "<li>Negative controls: "
        + (
            _e(
                f"{controls.passed} passed, {controls.failed} failed, "
                f"{controls.undetermined} undetermined of {controls.ran}."
            )
            if controls.evaluated
            else _e(controls.note)
        )
        + "</li></ul>"
    )

    add("<h2>Attacker refusals</h2>")
    add(
        _table(
            ["Attack method", "Generations", "Refused or empty", "Rate"],
            [[item.method, item.generations, item.refusals, _rate(item.rate)] for item in report.refusals.by_method]
            + [["All", report.refusals.generations, report.refusals.refusals, _rate(report.refusals.rate)]],
            numeric=(1, 2),
        )
    )
    add('<p class="muted">A refused generation is never counted as held.</p>')
    for title, groups in (("By attack method", report.by_method), ("By vulnerability", report.by_vulnerability)):
        add(f"<h2>{_e(title)}</h2>")
        add(
            _table(
                ["Name", "Attempts", "Decided", "Confirmed", "Suspected", "Undetermined", "Stopped", "Errors"],
                [
                    [
                        f"{group.name} ({group.arm})" if group.arm else group.name,
                        group.attempts,
                        group.decided,
                        _rate(group.confirmed),
                        _rate(group.suspected),
                        group.undetermined,
                        group.stopped,
                        group.errors,
                    ]
                    for group in groups
                ],
                numeric=(1, 2, 5, 6, 7),
            )
        )

    if report.by_attacker:
        add("<h2>By attacker</h2>")
        add(
            _table(
                ["Attacker", "Attempts", "Decided", "Confirmed", "Suspected", "Attacker calls", "Refused"],
                [
                    [
                        group.name,
                        group.attempts,
                        group.decided,
                        _rate(group.confirmed),
                        _rate(group.suspected),
                        group.attacker_calls,
                        group.refusals,
                    ]
                    for group in report.by_attacker
                ],
                numeric=(1, 2, 5, 6),
            )
        )

    add("<h2>Attempts</h2>")
    for attempt in run.attempts:
        probe = run.probe(attempt.probe_id)
        heading = (
            f"{probe.vulnerability}/{probe.vulnerability_type} via {probe.attack_method} · {attempt.role} "
            f"{attempt.number} · {attempt.outcome}"
            + (f" at level {int(attempt.level)}" if attempt.level else "")
            + (f" · attacker {attempt.attacker}" if attempt.attacker else "")
        )
        checks = _table(
            ["Check", "Kind", "Result", "Level", "Why"],
            [
                [
                    result.check_id,
                    result.kind,
                    "hit" if result.hit else ("no hit" if result.hit is False else "undetermined"),
                    None if result.level is None else int(result.level),
                    result.rationale or result.error,
                ]
                for result in attempt.check_results
            ],
        )
        detail = [f"<summary>{_e(heading)}</summary>", checks]
        if attempt.error:
            detail.append(f"<p>Error ({_e(attempt.error_kind)}): {_e(attempt.error)}</p>")
        detail.append(f"<p>Attack</p><pre>{_e(attempt.input)}</pre>")
        detail.append(f"<p>Reply</p><pre>{_e(attempt.output)}</pre>")
        add(f"<details>{''.join(detail)}</details>")

    add("<h2>Limitations</h2>")
    add("<ul>" + "".join(f"<li>{_e(item)}</li>" for item in report.limitations) + "</ul>")
    body = "\n".join(parts)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Red team run {_e(run.id)}</title><style>{STYLE}</style></head>"
        f"<body><main>\n{body}\n</main></body></html>\n"
    )


__all__ = ["render_html"]
