"""Write a Martian run report to the GitHub Actions job summary.

The evals composite action (.github/actions/evals) runs this after the suite. It reads the
output folder and appends Markdown to GITHUB_STEP_SUMMARY. It needs only the standard library.

The summary holds counts, pass rates with their intervals, check names, case ids and turn
numbers, never inputs, replies or judge reasons. When the action compares the run with the last
result of the baseline branch, it also says how the run compares (comparison.json, written by
`martian evals run --baseline`), names first each case that newly failed a security check, which
fails the gate whatever the noise label, then each other case that went from pass to fail, with
their failed checks and the reasons the SDK keeps only when they cannot quote the reply; or it
says that there is no baseline yet. A suite with a matrix gets one row per combination
(matrix.json), each compared with the same combination of the baseline, then the cases of each
combination that went from pass to fail, failed or have no verdict. A run with the cache of
model calls on says so under the headline, with how many replies came from the cache.

When the action also ran a red team run file, the summary has a red team section
(scripts/redteam_comment.py, read from redteam-report.json): the gate's result, the attacker, the
budget, the confirmed findings, the suspected count and the success rates. A job may run either or
both; the summary covers both.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKER = "<!-- martian-evals-comment: {key} -->"
MAX_ROWS = 25
NOT_RUN = "not run: "
"""How junit.xml's error for a case the run stopped before starts (``martian.evals.runner.write_junit``)."""
# Combinations of a matrix whose cases the comment names, each with up to five per list.
MAX_DETAILED = 10


@dataclass(frozen=True)
class Combination:
    """One combination of a matrix run: its entry in matrix.json and the latest record of each of
    its cases. ``comparison`` is None when it was not compared with a baseline."""

    id: str
    gate: str
    summary: dict[str, Any]
    comparison: dict[str, Any] | None
    cases: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Outcome:
    """What one run left in its output folder. ``summary`` is None when the run wrote no results
    or ran a matrix, whose ``axes`` and ``combinations`` are then filled; ``comparison`` is None
    when the run was not compared with a baseline."""

    exit_code: int
    summary: dict[str, Any] | None
    cases: list[dict[str, Any]] = field(default_factory=list)
    comparison: dict[str, Any] | None = None
    axes: list[dict[str, Any]] = field(default_factory=list)
    combinations: list[Combination] = field(default_factory=list)


@dataclass(frozen=True)
class Baseline:
    """The branch the action compares a pull request with, and the commit its result came from.
    ``commit`` is None when there was no result to compare with."""

    branch: str
    commit: str | None = None


def load_outcome(folder: Path, exit_code: int) -> Outcome:
    """Read summary.json, the latest record of each case in this run from results.jsonl, and
    comparison.json when the run had a baseline; for a matrix run, matrix.json and the same of
    each combination's folder.

    junit.xml lists exactly this run's cases in data order; results.jsonl can also hold records
    of cases that an earlier run in the same folder graded. A case a stopped run did not reach is
    in junit.xml as an error that starts ``not run:``, and its record in results.jsonl, if any, is
    an earlier run's, so it is left out: the summary counts it under ``not_run``. A run without a
    baseline removes an earlier run's comparison.json, and a run without a matrix an earlier run's
    matrix.json.
    """
    matrix_path = folder / "matrix.json"
    if matrix_path.is_file():
        found = json.loads(matrix_path.read_text(encoding="utf-8"))
        combinations = [
            Combination(
                str(entry["id"]),
                str(entry["gate"]),
                entry["summary"],
                entry["comparison"],
                _cases_of(folder / str(entry["folder"])),
            )
            for entry in found["combinations"]
        ]
        return Outcome(exit_code, None, axes=found["axes"], combinations=combinations)
    summary_path = folder / "summary.json"
    if not summary_path.is_file():
        return Outcome(exit_code, None)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    compared = folder / "comparison.json"
    comparison = json.loads(compared.read_text(encoding="utf-8")) if compared.is_file() else None
    return Outcome(exit_code, summary, _cases_of(folder), comparison)


def _cases_of(folder: Path) -> list[dict[str, Any]]:
    """The latest record of each case of the run in ``folder``, in junit.xml's order."""
    latest: dict[str, dict[str, Any]] = {}
    for line in (folder / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            latest[str(record["id"])] = record
    order = [str(case.get("name")) for case in ET.parse(folder / "junit.xml").getroot().iter("testcase") if _ran(case)]
    return [latest[name] for name in order if name in latest]


def _ran(case: ET.Element) -> bool:
    """Whether the run reached this junit.xml testcase, which a stopped run's error says it did not."""
    error = case.find("error")
    return error is None or not error.get("message", "").startswith(NOT_RUN)


def marker(key: str) -> str:
    return MARKER.format(key=re.sub(r"[^A-Za-z0-9._/-]", "-", key)[:200])


def code(text: str, limit: int = 60, *, cell: bool = True) -> str:
    """A code span: GitHub renders no Markdown, HTML or mention inside it.

    The fence is longer than any run of backticks in the text, and in a table cell a pipe is
    escaped so it cannot end the cell.
    """
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    if cell:
        text = text.replace("|", "\\|")
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if not text or text[0] == "`" or text[-1] == "`" else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def rate(counts: Mapping[str, Any]) -> str:
    """A pass rate and its interval from summary.json, written as ``martian.evals.stats.rate_text``
    writes them: the rate to the nearest percent, the bounds rounded outward."""
    if counts["pass_rate"] is None:
        return "—"
    low, high = counts["confidence_95"]
    shown = math.floor(counts["pass_rate"] * 100 + 0.5)
    return f"{shown}% ({math.floor(low * 100 + 1e-9)}–{math.ceil(high * 100 - 1e-9)}%)"


def render(
    outcome: Outcome,
    *,
    suite: str,
    key: str,
    sha: str | None = None,
    run_url: str | None = None,
    baseline: Baseline | None = None,
) -> str:
    """The comment body: the marker line, a headline, per-check counts and the cases that did not pass.
    With ``baseline``, or a comparison in the outcome, also how the run compares with the baseline
    and the cases that went from pass to fail, or that there is no baseline yet.

    Tables stop at MAX_ROWS and cells at 60 characters, which keeps the body far below GitHub's
    65,536-character limit for a comment. A matrix run is rendered by :func:`_matrix`.
    """
    if outcome.combinations:
        return "\n".join([*_matrix(outcome, suite=suite, key=key, baseline=baseline), _footer(sha, run_url)]) + "\n"
    summary = outcome.summary
    comparison = outcome.comparison if summary is not None else None
    against = code(baseline.branch, cell=False) if baseline else "the baseline"
    # The exit code is the gate, so it decides the headline; with a baseline the gate is the comparison.
    title = "did not finish" if summary is None else {0: "passed", 1: "failed"}.get(outcome.exit_code, "did not finish")
    if comparison is not None and outcome.exit_code in (0, 1):
        title += f" against {against}"
    lines = [marker(key), f"### Martian evals {title}", ""]
    if summary is None:
        lines += [
            f"Suite {code(suite, 120, cell=False)} wrote no results (exit code {outcome.exit_code}). "
            "The suite file could not be run; the job log says why.",
            "",
        ]
    else:
        # A simulated user's cases are counted in the headline, so their verdicts cannot pass for
        # real users'.
        simulated = summary.get("simulator")
        played = f" ({simulated['cases']} simulated)" if simulated else ""
        lines += [
            f"Suite {code(suite, 120, cell=False)}: {summary['cases']} cases{played}, {summary['passed']} passed, "
            f"{summary['failed']} failed, {summary['errors']} errors.",
            "",
        ]
        # A run that replayed replies from the cache says so first, so its result is never taken
        # for a fresh one.
        if (used := summary.get("cache")) is not None:
            lines += [cache_used(used), ""]
        # A run stopped at a limit, or cancelled, counts only the cases it finished.
        if not_run := summary.get("not_run"):
            why = "It was cancelled" if summary.get("cancelled") else summary["stopped"]["message"]
            lines += [f"{why}: {not_run} cases not run. Run the suite again to resume after the cases it finished.", ""]
        # A suite whose checks a refusal passes says how many benign cases were refused, next to
        # the pass count, so a model that refuses everything cannot look as if it passed.
        if (refusals := summary.get("false_refusals")) is not None:
            lines += [false_refusals(refusals), ""]
        if comparison is not None:
            lines += _compared(comparison, against, baseline.commit if baseline else None)
        elif baseline is not None:
            lines += [
                f"**Compared with {against}: no baseline yet.** No result of {against} is saved for this suite, "
                "so this run is gated on its own and any failed case fails it. "
                f"The next push to {against} saves one.",
                "",
            ]
        lines += [
            "| Check | Passed | Failed | Errors | Pass rate (95% CI) |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        checks = list(summary["checks"].items())
        for name, counts in checks[:MAX_ROWS]:
            lines.append(
                f"| {code(name)} | {counts['passed']} | {counts['failed']} | {counts['errors']} | {rate(counts)} |"
            )
        lines.append(
            f"| **All checks** | {summary['passed']} | {summary['failed']} | {summary['errors']} | {rate(summary)} |"
        )
        # With repeats, the share of cases that passed every attempt, under the pass rate.
        if reliable := summary.get("reliability"):
            lines.append(
                f"| **Reliability** (every attempt) | {reliable['passed']} | {reliable['failed']} | "
                f"{reliable['errors']} | {rate(reliable)} |"
            )
        # With simulated users, the verdicts of the cases a model played the user of.
        if simulated:
            lines.append(
                f"| **Simulated** (cases) | {simulated['passed']} | {simulated['failed']} | "
                f"{simulated['errors']} | {rate(simulated)} |"
            )
        # With conversation cases, their turns' own verdicts, which count turns, not cases.
        if talks := summary.get("conversations"):
            turns = talks["turns"]
            lines.append(
                f"| **Turns** (of conversations) | {turns['passed']} | {turns['failed']} | {turns['errors']} | — |"
            )
        if len(checks) > MAX_ROWS:
            lines.append(f"\n{len(checks) - MAX_ROWS} more checks are in summary.json.")
        if summary["groups"]:
            plural = "group" if summary["groups"] == 1 else "groups"
            lines.append(f"\nIntervals allow for {summary['groups']} case {plural}.")
        if repeats := summary.get("repeats"):
            lines.append(f"\n{_repeats(repeats)}")
        if talks:
            lines.append(f"\n{_conversations(talks)}")
        if simulated:
            lines.append(f"\n{_simulated(simulated)}")
        if reliable:
            lines.append(f"\n{_flaky(reliable['flaky'])}")
        lines.append("")
        # A case that newly failed a security check is the gate's reason, so it comes first, with
        # those checks; a case named in one table is not listed again in the next.
        leaked = comparison["security_regressed"] if comparison is not None else []
        if leaked:
            lines += _regressed(
                leaked,
                "Cases that newly failed a security check, which fails the gate",
                "Security checks that failed",
            )
        shown = {case["id"] for case in leaked}
        regressed = [case for case in comparison["regressed"] if case["id"] not in shown] if comparison else []
        if regressed:
            lines += _regressed(regressed, "Cases that went from pass to fail", "Checks that failed")
        shown |= {case["id"] for case in regressed}
        failed = [case for case in outcome.cases if case["passed"] is False and str(case["id"]) not in shown]
        errored = [case for case in outcome.cases if case["passed"] is None]
        if failed:
            heading = "Other failed cases" if shown else "Failed cases"
            lines += _table(heading, "Checks that failed", failed, _failed_checks)
        if errored:
            lines += _table("Cases without a verdict", "Reason", errored, _reason)
    lines.append(_footer(sha, run_url))
    return "\n".join(lines) + "\n"


def _footer(sha: str | None, run_url: str | None) -> str:
    footer = []
    if sha and re.fullmatch(r"[0-9a-f]{7,64}", sha):
        footer.append(f"Commit `{sha[:7]}`")
    if run_url:
        footer.append(f"[Workflow run]({run_url})")
    return " · ".join(footer)


RESULTS = {"passed": "passed", "failed": "failed", "error": "did not finish"}


def _matrix(outcome: Outcome, *, suite: str, key: str, baseline: Baseline | None) -> list[str]:
    """A matrix run: one row per combination with its counts, pass rate and interval, with repeats
    its reliability, for a suite with a policy check its false refusals, compared with the same
    combination of the baseline when it was, and how it ends; then, for each combination with a
    case that went from pass to fail, failed or has no verdict, those cases by name."""
    combinations = outcome.combinations
    against = code(baseline.branch, cell=False) if baseline else "the baseline"
    compared = [one for one in combinations if one.comparison is not None]
    title = {0: "passed", 1: "failed"}.get(outcome.exit_code, "did not finish")
    if compared and outcome.exit_code in (0, 1):
        title += f" against {against}"
    names = [code(axis["name"], cell=False) for axis in outcome.axes]
    axes = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    first = combinations[0].summary
    ended = {result: sum(one.gate == result for one in combinations) for result in RESULTS}
    counted = ", ".join(f"{count} {RESULTS[result]}" for result, count in ended.items() if count)
    lines = [
        marker(key),
        f"### Martian evals {title}",
        "",
        f"Suite {code(suite, 120, cell=False)}: {len(combinations)} combinations of {axes}, "
        f"{first['cases']} cases each; {counted}.",
        "",
    ]
    if used := [one.summary["cache"] for one in combinations if "cache" in one.summary]:
        lines += [cache_used(_combined(used)), ""]
    shown_against = baseline is not None or bool(compared)
    if baseline is not None and not compared:
        lines += [
            f"**Compared with {against}: no baseline yet.** No result of {against} is saved for this suite, "
            "so each combination is gated on its own and any failed case fails it. "
            f"The next push to {against} saves one.",
            "",
        ]
    elif compared:
        lines += [
            f"**Compared with {against}:** each combination is compared with the same combination there, case by "
            "case (exact McNemar test), and fails on a change worse than noise, on a case that newly fails a "
            "security check whatever the label, or on a failed case with no result to compare with. A "
            f"combination with no result on {against} is gated on its own.",
            "",
        ]
    repeated = first.get("repeats")
    refusals = first.get("false_refusals")
    header = "| Combination | Passed | Failed | Errors | Pass rate (95% CI) |"
    rule = "| --- | ---: | ---: | ---: | ---: |"
    if repeated:
        header, rule = header + " Reliability (95% CI) |", rule + " ---: |"
    if refusals is not None:
        header, rule = header + " False refusals |", rule + " ---: |"
    if shown_against:
        header, rule = header + f" Compared with {against} |", rule + " --- |"
    lines += [header + " Result |", rule + " --- |"]
    for one in combinations[:MAX_ROWS]:
        counts = one.summary
        row = f"| {code(one.id, 120)} | {counts['passed']} | {counts['failed']} | {counts['errors']} | {rate(counts)} |"
        if repeated:
            row += f" {rate(counts['reliability'])} |"
        if refusals is not None:
            row += f" {_refused(counts['false_refusals'])} |"
        if shown_against:
            label = None if one.comparison is None else one.comparison["label"]
            if one.comparison is None:
                row += " no baseline yet |"
            else:
                row += f" {LABELS[label] if label else 'no case compared'} |"
        lines.append(row + f" {RESULTS.get(one.gate, one.gate)} |")
    if len(combinations) > MAX_ROWS:
        lines.append(f"\n{len(combinations) - MAX_ROWS} more combinations are in matrix.json.")
    notes = []
    if first["groups"]:
        notes.append(f"Intervals allow for {first['groups']} case {'group' if first['groups'] == 1 else 'groups'}.")
    if repeated:
        notes.append(_repeats(repeated) + " Reliability counts the cases that passed every attempt.")
    if refusals is not None:
        # A reply that refuses everything passes the policy checks, so the rate stands beside the
        # pass rate; every combination has the same checks, so one says why it is not measured.
        notes.append(
            "False refusals count the benign cases that were refused."
            if refusals["checks"]
            else false_refusals(refusals)
        )
    lines += ["", *([" ".join(notes), ""] if notes else [])]
    detailed = [one for one in combinations if _details(one)]
    for one in detailed[:MAX_DETAILED]:
        lines += [f"#### {code(one.id, 120, cell=False)}", "", *_details(one), ""]
    if len(detailed) > MAX_DETAILED:
        more = len(detailed) - MAX_DETAILED
        lines += [f"{more} more combinations have cases that did not pass; each one's folder has its results.", ""]
    return lines


def _details(one: Combination) -> list[str]:
    """The cases of one combination that newly failed a security check, that otherwise went from
    pass to fail, failed or have no verdict, at most five of each by name, and its flaky cases. A
    case named in one list is not named again in the next."""
    lines = []
    leaked = one.comparison["security_regressed"] if one.comparison is not None else []
    shown = {case["id"] for case in leaked}
    regressed = [case for case in one.comparison["regressed"] if case["id"] not in shown] if one.comparison else []
    shown |= {case["id"] for case in regressed}
    failed = [case for case in one.cases if case["passed"] is False and str(case["id"]) not in shown]
    errored = [case for case in one.cases if case["passed"] is None]
    if leaked:
        security = _with_checks([(case["id"], list(case["checks"])) for case in leaked])
        lines.append(f"Newly failed a security check, which fails the gate: {security}")
    if regressed:
        lines.append("From pass to fail: " + _with_checks([(case["id"], list(case["checks"])) for case in regressed]))
    if failed:
        heading = "Other failed cases" if shown else "Failed cases"
        failing = [
            (str(case["id"]), [n for n, c in case["checks"].items() if c.get("passed") is False]) for case in failed
        ]
        lines.append(f"{heading}: {_with_checks(failing)}")
    if errored:
        lines.append(f"Without a verdict: {_names([str(case['id']) for case in errored], cell=False)}")
    if one.comparison is not None and (failures := one.comparison["unpaired_failures"]):
        lines.append(f"Failed with no result to compare with, which fails the gate: {_names(failures, cell=False)}.")
    if (reliable := one.summary.get("reliability")) and reliable["flaky"]:
        lines.append(_flaky(reliable["flaky"]))
    # One paragraph: the lines end as sentences.
    return [" ".join(line if line.endswith(".") else line + "." for line in lines)] if lines else []


def cache_used(found: Mapping[str, Any]) -> str:
    """The run's use of the cache of model calls, as `martian evals run` prints it, in bold: of
    each kind of call, how many replayed a reply an earlier run stored."""
    kinds = [kind for kind in ("target", "judge") if found[kind] is not None]
    if not kinds:
        return "**Cache: on**, but this suite makes no model call that the cache holds."
    shown = " and ".join(
        f"{found[kind]['hits']} of {found[kind]['hits'] + found[kind]['calls']} {kind} calls" for kind in kinds
    )
    sent = sum(found[kind]["calls"] for kind in kinds)
    return (
        f"**Cache: {shown} replayed replies stored by an earlier run**; "
        f"{sent} model {'call' if sent == 1 else 'calls'} made."
    )


def _combined(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    """The cache's counts of every combination added up."""
    combined = dict(reports[0])
    for kind in ("target", "judge"):
        if combined[kind] is not None:
            combined[kind] = {name: sum(report[kind][name] for report in reports) for name in combined[kind]}
    return combined


def _refused(found: Mapping[str, Any]) -> str:
    """One combination's false refusals in a cell: refused of benign, or not measured."""
    if not found["benign"]:
        return "not measured"
    return f"{found['refused']} of {found['benign']} ({found['rate']:.1%})"


def _with_checks(cases: list[tuple[str, list[str]]]) -> str:
    """Case ids, each with the checks that failed it, at most five."""
    shown = ", ".join(f"{code(case, cell=False)} ({_names(checks, cell=False)})" for case, checks in cases[:5])
    return shown + (f" and {len(cases) - 5} more" if len(cases) > 5 else "")


def false_refusals(found: dict[str, Any]) -> str:
    """The same line `martian evals run` prints under its table."""
    if not found["checks"]:
        return (
            "False refusals: not measured. A reply that refuses everything passes these checks; "
            "add a no_refusal check and mark the benign cases."
        )
    if not found["benign"] and not found["errors"]:
        return "False refusals: not measured. No case is marked benign."
    line = "False refusals: not measured"
    if found["benign"]:
        case = "case" if found["benign"] == 1 else "cases"
        line = f"False refusals: {found['refused']} of {found['benign']} benign {case} refused ({found['rate']:.1%})"
    if found["errors"]:
        line += f"; {found['errors']} benign {'case' if found['errors'] == 1 else 'cases'} without a verdict"
    return line + "."


LABELS = {"better": "better", "worse": "worse, beyond noise", "within_noise": "within noise"}


def _cases(count: int) -> str:
    return f"{count} {'case' if count == 1 else 'cases'}"


def p_value(found: float) -> str:
    """A p-value as `martian evals run --baseline` prints it: two significant figures, or below 0.001."""
    return "p < 0.001" if found < 0.001 else f"p = {found:.2g}"


def _compared(found: Mapping[str, Any], against: str, commit: str | None) -> list[str]:
    """How the run compares with its baseline (comparison.json): the label and the counts it rests
    on, what the gate fails on, and the cases and suite changes the comparison could not pair."""
    where = against + (f" at `{commit[:7]}`" if commit and re.fullmatch(r"[0-9a-f]{7,64}", commit) else "")
    significance = found["significance"]
    if significance is None:
        text = f"**Compared with {where}:** no case could be compared."
    else:
        text = (
            f"**Compared with {where}: {LABELS[found['label']]}.** Of {_cases(significance['compared'])} compared, "
            f"{significance['regressed']} went from pass to fail and {significance['improved']} from fail to pass "
            f"(exact McNemar test, {p_value(significance['p_value'])})."
        )
        effect = significance["minimum_detectable_effect"]
        text += (
            " With fewer than six cases compared, no change can be told from chance."
            if effect is None
            else f" The smallest change of the pass rate it can tell from chance is {round(effect * 100)} points."
        )
    text += (
        " The gate fails on a change worse than noise, on a case that newly fails a security check whatever "
        f"the label, or on a failed case with no result on {against} to compare with."
    )
    notes = []
    unpaired = [
        (found["new"], f"new since {against}"),
        (found["changed"], "with changed inputs or expectations"),
        (found["unverified"], "without a verdict in one of the runs"),
    ]
    if counted := [f"{_cases(len(ids))} {what}" for ids, what in unpaired if ids]:
        notes.append(f"Not compared: {', '.join(counted)}.")
    if failures := found["unpaired_failures"]:
        notes.append(f"Failed with no result to compare with, which fails the gate: {_names(failures, cell=False)}.")
    if found["missing"]:
        notes.append(f"{_cases(len(found['missing']))} of {against} did not run here.")
    checks = found["checks"]
    changes = [
        f"{what} {_names(checks[part], cell=False)}"
        for part, what in (("added", "added"), ("removed", "removed"), ("changed", "changed"))
        if checks[part]
    ]
    attempts = found["attempts"]
    if attempts["baseline"] != attempts["candidate"]:
        changes.append(f"attempts per case {attempts['baseline']} on {against} and {attempts['candidate']} here")
    if changes:
        notes.append(
            f"The suite changed since {against} ({'; '.join(changes)}), so the label measures that change too."
        )
    return [text, "", *([" ".join(notes), ""] if notes else [])]


def _regressed(cases: list[dict[str, Any]], heading: str, column: str) -> list[str]:
    """Cases from comparison.json, each with the checks it names and their reasons."""
    lines = [
        f"#### {heading}",
        "",
        f"| Case | {column} | Reason |",
        "| --- | --- | --- |",
    ]
    hidden = False
    for case in cases[:MAX_ROWS]:
        reasons = []
        for reason in case["checks"].values():
            hidden = hidden or reason is None
            reasons.append("—" if reason is None else code(reason, 120))
        lines.append(f"| {code(case['id'])} | {_names(list(case['checks']))} | {'; '.join(reasons[:5])} |")
    if len(cases) > MAX_ROWS:
        lines.append(f"\n{len(cases) - MAX_ROWS} more; comparison.json in the output folder has every case.")
    if hidden:
        lines.append(
            "\n— is a reason that could quote the reply, such as an LLM judge's; results.jsonl in the output "
            "folder has it."
        )
    return [*lines, ""]


def _table(
    heading: str, column: str, cases: list[dict[str, Any]], detail: Callable[[dict[str, Any]], str]
) -> list[str]:
    lines = [f"#### {heading}", "", f"| Case | {column} |", "| --- | --- |"]
    # A case whose user a model played says so beside its id.
    lines += [
        f"| {code(case['id'])}{' (simulated)' if case.get('simulated') is True else ''} | {detail(case)} |"
        for case in cases[:MAX_ROWS]
    ]
    if len(cases) > MAX_ROWS:
        lines.append(f"\n{len(cases) - MAX_ROWS} more; results.jsonl in the output folder has every case.")
    return [*lines, ""]


def _repeats(repeats: Mapping[str, Any]) -> str:
    """How many times each case ran, and which checks' attempts combined by which rule."""
    by_rule: dict[str, list[str]] = {}
    for name, rule in repeats["checks"].items():
        by_rule.setdefault(rule["combine"], []).append(name)
    rules = "; ".join(f"{code(rule, cell=False)} for {_names(names, cell=False)}" for rule, names in by_rule.items())
    return (
        f"Each case ran {repeats['attempts']} times, and the counts are of cases: each check's attempts "
        f"combine by its rule, {rules}."
    )


def _conversations(talks: Mapping[str, Any]) -> str:
    """How many cases are conversations, and what their verdict means."""
    cases = "1 case is a conversation" if talks["cases"] == 1 else f"{talks['cases']} cases are conversations"
    return (
        f"{cases} of {talks['turns']['total']} turns in all. A conversation counts once: it passes when each "
        "turn check passed every turn it grades and each conversation check passed the whole conversation."
    )


def _simulated(simulated: Mapping[str, Any]) -> str:
    """How many conversations had a simulated user, and which model played them."""
    cases = "1 conversation has" if simulated["cases"] == 1 else f"{simulated['cases']} conversations have"
    return (
        f"{cases} a simulated user, played by {code(simulated['model'], 120, cell=False)}: **Simulated** "
        f"counts their verdicts, and {code('goal_reached', cell=False)} is whether each reached its goal."
    )


def _flaky(names: list[str]) -> str:
    """What reliability counts, and the cases whose verdict changed between attempts."""
    meaning = "Reliability counts the cases that passed every attempt."
    if not names:
        return f"{meaning} No flaky cases: no case's verdict changed between attempts."
    return f"{meaning} Flaky cases, whose verdict changed between attempts: {_names(names, cell=False)}" + (
        "; summary.json lists every one." if len(names) > 5 else "."
    )


def _names(names: list[str], *, cell: bool = True) -> str:
    shown = ", ".join(code(name, cell=cell) for name in names[:5])
    return shown + (f" and {len(names) - 5} more" if len(names) > 5 else "")


def _turns(numbers: list[int]) -> str:
    return ("turn " if len(numbers) == 1 else "turns ") + ", ".join(str(number) for number in numbers[:10])


def _failed_checks(case: dict[str, Any]) -> str:
    shown = _names([name for name, check in case["checks"].items() if check.get("passed") is False])
    # A conversation also says which of its turns failed.
    if failed := [turn["number"] for turn in case.get("turns", []) if turn.get("passed") is False]:
        shown += f" (failed {_turns(failed)})"
    return shown


def _reason(case: dict[str, Any]) -> str:
    # The case error is the SDK's own wording (the target failed, and how). A check's error can
    # hold judge or scorer text, so only the check's name is shown.
    if case.get("error"):
        failed = [turn["number"] for turn in case.get("turns", []) if turn.get("error")]
        return (f"{_turns(failed[:1]).capitalize()}: " if failed else "") + code(case["error"], 120)
    return "No verdict from " + _names([name for name, check in case["checks"].items() if check.get("passed") is None])


def _redteam() -> Any:
    """``scripts/redteam_comment.py``, beside this script, which renders the red team section."""
    spec = importlib.util.spec_from_file_location("redteam_comment", Path(__file__).with_name("redteam_comment.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def with_redteam(
    body: str | None,
    *,
    key: str,
    run_file: str,
    folder: Path,
    exit_code: int,
    sha: str | None = None,
    run_url: str | None = None,
) -> str:
    """``body`` (the evals comment, or None when the action ran no suite) with the red team section of
    the run in ``folder`` added before its footer."""
    redteam = _redteam()
    section = redteam.render(redteam.load(folder), exit_code=exit_code, run_file=run_file, code=code)
    if body is None:
        lines, footer = [marker(key)], _footer(sha, run_url)
    else:
        lines = body.removesuffix("\n").split("\n")
        footer = lines.pop()
        if lines[-1] != "":
            lines.append("")
    return "\n".join([*lines, *section, footer]) + "\n"


def main(environ: Mapping[str, str] = os.environ) -> int:
    suite = environ.get("MARTIAN_EVALS_SUITE", "")
    run_file = environ.get("MARTIAN_REDTEAM_RUN_FILE", "")
    key = environ.get("MARTIAN_EVALS_COMMENT_KEY") or suite or run_file
    run = [environ.get(name, "") for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    run_url = "{}/{}/actions/runs/{}".format(*run) if all(run) else None
    sha = environ.get("MARTIAN_EVALS_SHA")
    body = None
    if suite:
        outcome = load_outcome(Path(environ["MARTIAN_EVALS_OUTPUT"]), int(environ["MARTIAN_EVALS_EXIT_CODE"]))
        # Set by the action when it compares a pull request with the baseline branch's last result.
        branch = environ.get("MARTIAN_EVALS_BASELINE_BRANCH", "")
        baseline = Baseline(branch, environ.get("MARTIAN_EVALS_BASELINE_SHA") or None) if branch else None
        body = render(outcome, suite=suite, key=key, sha=sha, run_url=run_url, baseline=baseline)
    if run_file:
        body = with_redteam(
            body,
            key=key,
            run_file=run_file,
            folder=Path(environ["MARTIAN_REDTEAM_OUTPUT"]),
            exit_code=int(environ.get("MARTIAN_REDTEAM_EXIT_CODE") or 2),
            sha=sha,
            run_url=run_url,
        )
    if body is None:
        print("::error title=Martian Evals::The action ran neither a suite nor a red team run file.")
        return 1
    summary = environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        print("::error title=Martian Evals::GITHUB_STEP_SUMMARY is missing.")
        return 1
    with open(summary, "a", encoding="utf-8") as handle:
        handle.write(body + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
