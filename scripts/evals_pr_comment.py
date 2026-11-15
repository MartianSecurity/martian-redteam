"""Report a `martian evals run` result on its pull request, in one comment kept up to date.

The evals composite action (.github/actions/evals) runs this after the suite. It reads the
output folder, writes the report to the job summary, and on a pull request edits the comment
that carries this suite's hidden marker, creating it only when there is none. Standard library
only, so it runs on the action's Python without further installs.

The comment holds counts, pass rates with their intervals, check names, case ids and turn
numbers, never inputs, replies or judge reasons.
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
NOT_RUN = "not run: "
"""How junit.xml's error for a case the run stopped before starts (``martian.evals.runner.write_junit``)."""
PER_PAGE = 100
READ_ONLY = (
    "The workflow token cannot write pull-request comments, so the result is in the job summary only. "
    "Pull requests from forks and from Dependabot get a read-only token; otherwise grant the job "
    "`pull-requests: write`."
)


@dataclass(frozen=True)
class Outcome:
    """What one run left in its output folder. ``summary`` is None when the run wrote no results."""

    exit_code: int
    summary: dict[str, Any] | None
    cases: list[dict[str, Any]] = field(default_factory=list)


def load_outcome(folder: Path, exit_code: int) -> Outcome:
    """Read summary.json, and the latest record of each case in this run from results.jsonl.

    junit.xml lists exactly this run's cases in data order; results.jsonl can also hold records
    of cases that an earlier run in the same folder graded. A case a stopped run did not reach is
    in junit.xml as an error that starts ``not run:``, and its record in results.jsonl, if any, is
    an earlier run's, so it is left out: the summary counts it under ``not_run``.
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
    order = [str(case.get("name")) for case in ET.parse(folder / "junit.xml").getroot().iter("testcase") if _ran(case)]
    return Outcome(exit_code, summary, [latest[name] for name in order if name in latest])


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


def render(outcome: Outcome, *, suite: str, key: str, sha: str | None = None, run_url: str | None = None) -> str:
    """The comment body: the marker line, a headline, per-check counts and the cases that did not pass.

    Tables stop at MAX_ROWS and cells at 60 characters, which keeps the body far below GitHub's
    65,536-character limit for a comment.
    """
    summary = outcome.summary
    # The exit code is the gate, so it decides the headline.
    title = "did not finish" if summary is None else {0: "passed", 1: "failed"}.get(outcome.exit_code, "did not finish")
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
        # A run stopped at a limit, or cancelled, counts only the cases it finished.
        if not_run := summary.get("not_run"):
            why = "It was cancelled" if summary.get("cancelled") else summary["stopped"]["message"]
            lines += [f"{why}: {not_run} cases not run. Run the suite again to resume after the cases it finished.", ""]
        # A suite whose checks a refusal passes says how many benign cases were refused, next to
        # the pass count, so a model that refuses everything cannot look as if it passed.
        if (refusals := summary.get("false_refusals")) is not None:
            lines += [false_refusals(refusals), ""]
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
        if reliable:
            lines.append(f"\n{_flaky(reliable['flaky'])}")
        lines.append("")
        failed = [case for case in outcome.cases if case["passed"] is False]
        errored = [case for case in outcome.cases if case["passed"] is None]
        if failed:
            lines += _table("Failed cases", "Checks that failed", failed, _failed_checks)
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


def _conversations(talks: Mapping[str, Any]) -> str:
    """How many cases are conversations, and what their verdict means."""
    cases = "1 case is a conversation" if talks["cases"] == 1 else f"{talks['cases']} cases are conversations"
    return (
        f"{cases} of {talks['turns']['total']} turns in all. A conversation counts once: it passes when each "
        "turn check passed every turn it grades and each conversation check passed the whole conversation."
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
    body = render(outcome, suite=suite, key=key, sha=environ.get("MARTIAN_EVALS_SHA"), run_url=run_url)
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
