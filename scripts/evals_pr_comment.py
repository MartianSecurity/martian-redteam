"""Report a `martian evals run` result on its pull request, in one comment kept up to date.

The evals composite action (.github/actions/evals) runs this after the suite. It reads the
output folder, writes the report to the job summary, and on a pull request edits the comment
that carries this suite's hidden marker, creating it only when there is none. Standard library
only, so it runs on the action's Python without further installs.

The comment holds counts, pass rates with their intervals, check names and case ids, never
inputs, replies or judge reasons. When the action compares the run with the last result of the
baseline branch, it also says how the run compares (comparison.json, written by
`martian evals run --baseline`) and names each case that went from pass to fail with its failed
checks and their reasons, which the SDK keeps only when they cannot quote the reply; or it says
that there is no baseline yet.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKER = "<!-- martian-evals-comment: {key} -->"
DEFAULT_AUTHOR = "github-actions[bot]"
MAX_ROWS = 25
PER_PAGE = 100
READ_ONLY = (
    "The workflow token cannot write pull-request comments, so the result is in the job summary only. "
    "Pull requests from forks and from Dependabot get a read-only token; otherwise grant the job "
    "`pull-requests: write`."
)


@dataclass(frozen=True)
class Outcome:
    """What one run left in its output folder. ``summary`` is None when the run wrote no results;
    ``comparison`` is None when the run was not compared with a baseline."""

    exit_code: int
    summary: dict[str, Any] | None
    cases: list[dict[str, Any]] = field(default_factory=list)
    comparison: dict[str, Any] | None = None


@dataclass(frozen=True)
class Baseline:
    """The branch the action compares a pull request with, and the commit its result came from.
    ``commit`` is None when there was no result to compare with."""

    branch: str
    commit: str | None = None


def load_outcome(folder: Path, exit_code: int) -> Outcome:
    """Read summary.json, the latest record of each case in this run from results.jsonl, and
    comparison.json when the run had a baseline.

    junit.xml lists exactly this run's cases in data order; results.jsonl can also hold records
    of cases that an earlier run in the same folder graded. A run without a baseline removes an
    earlier run's comparison.json.
    """
    summary_path = folder / "summary.json"
    if not summary_path.is_file():
        return Outcome(exit_code, None)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    latest: dict[str, dict[str, Any]] = {}
    for line in (folder / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            latest[str(record["id"])] = record
    order = [str(case.get("name")) for case in ET.parse(folder / "junit.xml").getroot().iter("testcase")]
    compared = folder / "comparison.json"
    comparison = json.loads(compared.read_text(encoding="utf-8")) if compared.is_file() else None
    return Outcome(exit_code, summary, [latest[name] for name in order if name in latest], comparison)


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
    65,536-character limit for a comment.
    """
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
        lines += [
            f"Suite {code(suite, 120, cell=False)}: {summary['cases']} cases, {summary['passed']} passed, "
            f"{summary['failed']} failed, {summary['errors']} errors.",
            "",
        ]
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
        if len(checks) > MAX_ROWS:
            lines.append(f"\n{len(checks) - MAX_ROWS} more checks are in summary.json.")
        if summary["groups"]:
            plural = "group" if summary["groups"] == 1 else "groups"
            lines.append(f"\nIntervals allow for {summary['groups']} case {plural}.")
        if repeats := summary.get("repeats"):
            lines.append(f"\n{_repeats(repeats)}")
        if reliable:
            lines.append(f"\n{_flaky(reliable['flaky'])}")
        lines.append("")
        regressed = comparison["regressed"] if comparison is not None else []
        if regressed:
            lines += _regressed(regressed)
        # A case named under "went from pass to fail" is not listed again.
        shown = {case["id"] for case in regressed}
        failed = [case for case in outcome.cases if case["passed"] is False and str(case["id"]) not in shown]
        errored = [case for case in outcome.cases if case["passed"] is None]
        if failed:
            heading = "Other failed cases" if regressed else "Failed cases"
            lines += _table(heading, "Checks that failed", failed, _failed_checks)
        if errored:
            lines += _table("Cases without a verdict", "Reason", errored, _reason)
    footer = []
    if sha and re.fullmatch(r"[0-9a-f]{7,64}", sha):
        footer.append(f"Commit `{sha[:7]}`")
    if run_url:
        footer.append(f"[Workflow run]({run_url})")
    footer.append("Updated on every push")
    lines.append(" · ".join(footer))
    return "\n".join(lines) + "\n"


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
        " The gate fails only on a change worse than noise, or on a failed case with no result on "
        f"{against} to compare with."
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


def _regressed(cases: list[dict[str, Any]]) -> list[str]:
    """The cases that went from pass to fail, each with its failed checks and their reasons."""
    lines = [
        "#### Cases that went from pass to fail",
        "",
        "| Case | Checks that failed | Reason |",
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
    lines += [f"| {code(case['id'])} | {detail(case)} |" for case in cases[:MAX_ROWS]]
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


def _failed_checks(case: dict[str, Any]) -> str:
    return _names([name for name, check in case["checks"].items() if check.get("passed") is False])


def _reason(case: dict[str, Any]) -> str:
    # The case error is the SDK's own wording (the target failed, and how). A check's error can
    # hold judge or scorer text, so only the check's name is shown.
    if case.get("error"):
        return code(case["error"], 120)
    return "No verdict from " + _names([name for name, check in case["checks"].items() if check.get("passed") is None])


class GitHubError(Exception):
    def __init__(self, status: int, method: str, path: str) -> None:
        self.status = status
        what = f"HTTP {status}" if status else "no response"
        super().__init__(f"GitHub {method} {path.split('?')[0]} failed: {what}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the token to another address, so it is an error instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class GitHub:
    """The three issue-comment calls the report needs, with the workflow token."""

    def __init__(self, api_url: str, repository: str, token: str, *, timeout: float = 30.0) -> None:
        self.api_url = api_url.rstrip("/")
        self.repository = repository
        self._token = token
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            self.api_url + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "martian-evals-action",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise GitHubError(error.code, method, path) from None
        except (urllib.error.URLError, TimeoutError):
            raise GitHubError(0, method, path) from None

    def comments(self, number: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        page = 1
        while True:
            path = f"/repos/{self.repository}/issues/{number}/comments?per_page={PER_PAGE}&page={page}"
            batch = self._call("GET", path)
            found.extend(batch)
            if len(batch) < PER_PAGE:
                return found
            page += 1

    def create(self, number: int, body: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": body}
        )
        return result

    def update(self, comment_id: int, body: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "PATCH", f"/repos/{self.repository}/issues/comments/{comment_id}", {"body": body}
        )
        return result


def upsert(github: GitHub, number: int, body: str, *, key: str, author: str) -> tuple[str, dict[str, Any]]:
    """Edit this suite's comment by ``author``, found by its first line, or create it."""
    wanted = marker(key)
    for comment in github.comments(number):
        first = str(comment.get("body") or "").split("\n", 1)[0].strip()
        if first == wanted and (comment.get("user") or {}).get("login") == author:
            return "updated", github.update(int(comment["id"]), body)
    return "created", github.create(number, body)


def main(environ: Mapping[str, str] = os.environ) -> int:
    suite = environ["MARTIAN_EVALS_SUITE"]
    exit_code = int(environ["MARTIAN_EVALS_EXIT_CODE"])
    key = environ.get("MARTIAN_EVALS_COMMENT_KEY") or suite
    run = [environ.get(name, "") for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    run_url = "{}/{}/actions/runs/{}".format(*run) if all(run) else None
    outcome = load_outcome(Path(environ["MARTIAN_EVALS_OUTPUT"]), exit_code)
    # Set by the action when it compares a pull request with the baseline branch's last result.
    branch = environ.get("MARTIAN_EVALS_BASELINE_BRANCH", "")
    baseline = Baseline(branch, environ.get("MARTIAN_EVALS_BASELINE_SHA") or None) if branch else None
    body = render(
        outcome, suite=suite, key=key, sha=environ.get("MARTIAN_EVALS_SHA"), run_url=run_url, baseline=baseline
    )
    if environ.get("GITHUB_STEP_SUMMARY"):
        with open(environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(body + "\n")
    number = environ.get("MARTIAN_EVALS_PR_NUMBER", "")
    if not number:
        print("Not a pull-request event: the result is in the job summary and no comment was written.")
        return 0
    github = GitHub(
        environ.get("GITHUB_API_URL") or "https://api.github.com",
        environ["GITHUB_REPOSITORY"],
        environ.get("GITHUB_TOKEN", ""),
    )
    try:
        action, comment = upsert(
            github, int(number), body, key=key, author=environ.get("MARTIAN_EVALS_COMMENT_AUTHOR") or DEFAULT_AUTHOR
        )
    except GitHubError as error:
        if error.status == 403:
            print(f"::warning title=Martian evals comment::{READ_ONLY}")
            return 0
        print(f"::error title=Martian evals comment::{error}")
        return 1
    print(f"The pull-request comment was {action}: {comment.get('html_url', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
