"""The evals action's pull-request comment: what it says, and that each push edits one comment."""

import importlib.util
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import mlflow
import pytest
from typer.testing import CliRunner

from martian.cli import app
from martian.evals.state import STATE

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples/github-action"
SHA = "0123456789abcdef0123456789abcdef01234567"
RUN_URL = "https://github.com/example/agent/actions/runs/1"
TOKEN = "test-token"
BOT = "github-actions[bot]"
SUITE = "examples/github-action/failing/evals.yaml"

_spec = importlib.util.spec_from_file_location("evals_pr_comment", ROOT / "scripts/evals_pr_comment.py")
assert _spec is not None and _spec.loader is not None
comment = importlib.util.module_from_spec(_spec)
sys.modules["evals_pr_comment"] = comment
_spec.loader.exec_module(comment)


@pytest.fixture(scope="module")
def sample_runs(tmp_path_factory):
    """Run both sample suites once through the CLI, with no Martian setting, as the action does."""
    runs = {}
    with pytest.MonkeyPatch.context() as patch:
        for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID"):
            patch.delenv(name, raising=False)
        patch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
        patch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
        old_uri = mlflow.get_tracking_uri()
        for name in ("passing", "failing"):
            STATE.reset()
            folder = tmp_path_factory.mktemp(name) / "results"
            result = CliRunner().invoke(
                app, ["evals", "run", str(SAMPLE / name / "evals.yaml"), "--output", str(folder)]
            )
            runs[name] = (folder, result.exit_code)
        STATE.reset()
        mlflow.set_tracking_uri(old_uri)
    return runs


@pytest.mark.parametrize(("name", "exit_code", "expected"), [("passing", 0, "passed.md"), ("failing", 1, "failed.md")])
def test_sample_suites_render_the_checked_in_comments(sample_runs, name, exit_code, expected):
    folder, code = sample_runs[name]
    assert code == exit_code
    suite = f"examples/github-action/{name}/evals.yaml"
    body = comment.render(comment.load_outcome(folder, code), suite=suite, key=suite, sha=SHA, run_url=RUN_URL)
    assert body == (SAMPLE / "comments" / expected).read_text(encoding="utf-8")


def test_a_run_that_wrote_no_results_says_so_and_ignores_an_older_folder(tmp_path):
    (tmp_path / "results.jsonl").write_text('{"id": "old", "passed": true, "checks": {}}\n', encoding="utf-8")
    body = comment.render(comment.load_outcome(tmp_path, 2), suite=SUITE, key=SUITE)
    assert body.splitlines()[:2] == [comment.marker(SUITE), "### Martian evals did not finish"]
    assert "wrote no results (exit code 2)" in body and "old" not in body


def test_a_repeated_run_says_how_often_each_case_ran_each_checks_rule_and_its_reliability(tmp_path):
    counts = {"passed": 0, "failed": 2, "errors": 0, "pass_rate": 0.0, "confidence_95": [0.0, 0.66]}
    summary = {
        "cases": 2,
        **counts,
        "groups": None,
        "repeats": {
            "attempts": 5,
            "combine": "any",
            "checks": {
                "no_leak": {"combine": "all", "from": "security"},
                "polite": {"combine": "any", "from": "suite"},
            },
        },
        "checks": {"no_leak": counts, "polite": {**counts, "passed": 2, "failed": 0, "pass_rate": 1.0}},
        "reliability": {**counts, "pass_rate": 0.0, "confidence_95": [0.0, 0.66], "flaky": ["key", "hint"]},
    }
    body = comment.render(comment.Outcome(1, summary), suite=SUITE, key=SUITE)
    assert (
        "Each case ran 5 times, and the counts are of cases: each check's attempts combine by its rule, "
        "`all` for `no_leak`; `any` for `polite`."
    ) in body
    # The share of cases that passed every attempt, under the pass rate, and the flaky cases.
    rows = body.splitlines()
    assert rows[rows.index("| **All checks** | 0 | 2 | 0 | 0% (0–66%) |") + 1] == (
        "| **Reliability** (every attempt) | 0 | 2 | 0 | 0% (0–66%) |"
    )
    assert (
        "Reliability counts the cases that passed every attempt. Flaky cases, whose verdict changed "
        "between attempts: `key`, `hint`."
    ) in body
    calm = {**summary, "reliability": {**summary["reliability"], "flaky": []}}
    assert "No flaky cases: no case's verdict changed between attempts." in comment.render(
        comment.Outcome(1, calm), suite=SUITE, key=SUITE
    )
    plain = comment.render(
        comment.Outcome(1, {**summary, "repeats": None, "reliability": None}), suite=SUITE, key=SUITE
    )
    assert "Each case ran" not in plain and "Reliability" not in plain and "Flaky" not in plain


def test_a_policy_suite_says_how_many_benign_cases_were_refused_under_the_headline():
    from martian.evals.runner import false_refusal_line

    checks = {
        "no_secrets": {"passed": 4, "failed": 0, "errors": 0, "pass_rate": 1.0, "confidence_95": [0.51, 1.0]},
        "no_refusal": {"passed": 3, "failed": 1, "errors": 0, "pass_rate": 0.75, "confidence_95": [0.3, 0.95]},
    }
    measured = {"checks": ["no_refusal"], "benign": 3, "refused": 1, "errors": 0, "rate": 0.3333}
    summary = {
        "cases": 4,
        **checks["no_refusal"],
        "groups": None,
        "checks": checks,
        "false_refusals": measured,
    }
    lines = comment.render(comment.Outcome(1, summary), suite=SUITE, key=SUITE).splitlines()
    assert lines[3:7] == [
        f"Suite {comment.code(SUITE, 120, cell=False)}: 4 cases, 3 passed, 1 failed, 0 errors.",
        "",
        "False refusals: 1 of 3 benign cases refused (33.3%).",
        "",
    ]
    # The comment and `martian evals run` say the same, measured or not.
    unmeasured = {"checks": [], "benign": 0, "refused": 0, "errors": 0, "rate": None}
    for found in (measured, unmeasured, {**measured, "errors": 2}, {**measured, "benign": 0, "rate": None}):
        assert comment.false_refusals(found) == false_refusal_line(found)
    # A suite with no policy check has no false_refusals, and its comment no line for them.
    without = {key: item for key, item in summary.items() if key != "false_refusals"}
    assert "False refusals" not in comment.render(comment.Outcome(1, without), suite=SUITE, key=SUITE)


def test_a_repeated_policy_suite_shows_its_false_refusals_and_its_reliability():
    counts = {"passed": 0, "failed": 2, "errors": 0, "pass_rate": 0.0, "confidence_95": [0.0, 0.66]}
    summary = {
        "cases": 2,
        **counts,
        "groups": None,
        "repeats": {
            "attempts": 5,
            "combine": "any",
            "checks": {
                "no_secrets": {"combine": "all", "from": "security"},
                "no_refusal": {"combine": "any", "from": "suite"},
            },
        },
        "checks": {"no_secrets": counts, "no_refusal": {**counts, "passed": 2, "failed": 0, "pass_rate": 1.0}},
        "false_refusals": {"checks": ["no_refusal"], "benign": 2, "refused": 0, "errors": 0, "rate": 0.0},
        "reliability": {**counts, "flaky": []},
    }
    lines = comment.render(comment.Outcome(1, summary), suite=SUITE, key=SUITE).splitlines()
    # The false-refusal line under the headline, and the reliability row under the pass rate.
    assert lines[5:7] == ["False refusals: 0 of 2 benign cases refused (0.0%).", ""]
    assert lines[lines.index("| **All checks** | 0 | 2 | 0 | 0% (0–66%) |") + 1] == (
        "| **Reliability** (every attempt) | 0 | 2 | 0 | 0% (0–66%) |"
    )
    assert "`all` for `no_secrets`; `any` for `no_refusal`." in "\n".join(lines)


def test_a_stopped_run_says_how_many_cases_it_did_not_run_and_shows_no_earlier_result_for_them(tmp_path):
    from martian.evals.runner import write_junit

    stopped = "Stopped at its time limit (max_seconds 60) after 61 seconds"
    counts = {"passed": 1, "failed": 0, "errors": 0, "pass_rate": 1.0, "confidence_95": [0.21, 1.0]}
    summary = {
        "cases": 1,
        **counts,
        "groups": None,
        "checks": {"no_leak": counts},
        "not_run": 1,
        "stopped": {"limit": "max_seconds", "value": 60, "used": 61, "message": stopped},
    }
    done = {"id": "done", "passed": True, "checks": {"no_leak": {"passed": True}}, "error": None}
    # An earlier run's record of the case this run stopped before.
    earlier = {"id": "later", "passed": None, "checks": {}, "error": "the target call failed"}
    lines = "".join(json.dumps(record) + "\n" for record in (done, earlier))
    (tmp_path / "results.jsonl").write_text(lines, encoding="utf-8")
    (tmp_path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    write_junit([done], tmp_path / "junit.xml", suite_name="evals", summary=summary, not_run=["later"], stopped=stopped)
    outcome = comment.load_outcome(tmp_path, 2)
    assert [case["id"] for case in outcome.cases] == ["done"]
    body = comment.render(outcome, suite=SUITE, key=SUITE)
    assert body.splitlines()[1] == "### Martian evals did not finish"
    assert f"{stopped}: 1 cases not run. Run the suite again to resume after the cases it finished." in body
    assert "later" not in body and "Cases without a verdict" not in body
    cancelled = comment.render(
        comment.Outcome(2, {**summary, "stopped": None, "cancelled": True}), suite=SUITE, key=SUITE
    )
    assert "It was cancelled: 1 cases not run." in cancelled


def test_names_cannot_break_the_table_the_marker_or_mention_anyone():
    cell = comment.code("a|b`c @team <b>x</b>")
    assert cell == "``a\\|b`c @team <b>x</b>``"  # one code span; the escaped pipe stays in its cell
    assert comment.marker("x --> <script>").count("-->") == 1


class FakeGitHub:
    """One pull request's issue comments, served on loopback the way api.github.com serves them."""

    def __init__(self):
        self.comments = []
        self.calls = []
        self.refuse_writes = None
        self.next_id = 1

    def add(self, body, login):
        self.comments.append({"id": self.next_id, "body": body, "user": {"login": login}})
        self.next_id += 1
        return self.comments[-1]

    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                url = urlsplit(self.path)
                fake.calls.append((method, url.path))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._reply(401, {"message": "Bad credentials"})
                if method != "GET" and fake.refuse_writes:
                    return self._reply(fake.refuse_writes, {"message": "Resource not accessible by integration"})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if method == "GET" and url.path == "/repos/example/agent/issues/7/comments":
                    query = parse_qs(url.query)
                    size, page = int(query["per_page"][0]), int(query["page"][0])
                    return self._reply(200, fake.comments[(page - 1) * size : page * size])
                if method == "POST" and url.path == "/repos/example/agent/issues/7/comments":
                    return self._reply(201, fake.add(body["body"], BOT))
                found = re.fullmatch(r"/repos/example/agent/issues/comments/(\d+)", url.path)
                if method == "PATCH" and found:
                    for existing in fake.comments:
                        if existing["id"] == int(found.group(1)):
                            existing["body"] = body["body"]
                            return self._reply(200, existing)
                return self._reply(404, {"message": "Not Found"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

        return Handler


@pytest.fixture
def github(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    fake = FakeGitHub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield fake
    server.shutdown()
    server.server_close()


def action_env(github, folder, exit_code, summary_file, **extra):
    """What the action's report step passes, plus what the runner sets for every step."""
    return {
        "MARTIAN_EVALS_SUITE": SUITE,
        "MARTIAN_EVALS_OUTPUT": str(folder),
        "MARTIAN_EVALS_EXIT_CODE": str(exit_code),
        "MARTIAN_EVALS_PR_NUMBER": "7",
        "MARTIAN_EVALS_SHA": SHA,
        "GITHUB_TOKEN": TOKEN,
        "GITHUB_API_URL": github.url,
        "GITHUB_REPOSITORY": "example/agent",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "1",
        "GITHUB_STEP_SUMMARY": str(summary_file),
        **extra,
    }


def test_each_push_edits_the_one_comment_with_this_suites_marker(github, sample_runs, tmp_path, capsys):
    for number in range(110):
        github.add(f"Review note {number}", "reviewer")
    copied = github.add(comment.marker(SUITE) + "\nquoted by a reviewer", "reviewer")
    other = github.add(comment.marker("examples/other/evals.yaml") + "\nanother suite", BOT)
    untouched = [dict(copied), dict(other)]

    # The first push fails the suite: no comment of ours exists yet, so one is created.
    failing, code = sample_runs["failing"]
    assert comment.main(action_env(github, failing, code, tmp_path / "summary-1.md")) == 0
    ours = [c for c in github.comments if c["user"]["login"] == BOT and c["body"].startswith(comment.marker(SUITE))]
    assert len(ours) == 1 and "### Martian evals failed" in ours[0]["body"]
    assert ("POST", "/repos/example/agent/issues/7/comments") in github.calls

    # The next push fixes it: the same comment, found on the second page, is edited in place.
    github.calls.clear()
    passing, code = sample_runs["passing"]
    assert comment.main(action_env(github, passing, code, tmp_path / "summary-2.md")) == 0
    methods = [method for method, _ in github.calls]
    assert methods == ["GET", "GET", "PATCH"]
    assert github.calls[-1][1] == f"/repos/example/agent/issues/comments/{ours[0]['id']}"
    mine = [c for c in github.comments if c["user"]["login"] == BOT and c["body"].startswith(comment.marker(SUITE))]
    assert [c["id"] for c in mine] == [ours[0]["id"]]
    assert "### Martian evals passed" in mine[0]["body"]
    assert [copied, other] == untouched and len(github.comments) == 113

    out = capsys.readouterr().out
    assert "was created" in out and "was updated" in out and TOKEN not in out
    assert "### Martian evals passed" in (tmp_path / "summary-2.md").read_text(encoding="utf-8")


def test_a_read_only_token_warns_and_leaves_the_result_in_the_job_summary(github, sample_runs, tmp_path, capsys):
    github.refuse_writes = 403
    folder, code = sample_runs["failing"]
    assert comment.main(action_env(github, folder, code, tmp_path / "summary.md")) == 0
    assert "::warning" in capsys.readouterr().out
    assert github.comments == []
    assert "### Martian evals failed" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_another_api_failure_fails_the_report_step(github, sample_runs, tmp_path, capsys):
    github.refuse_writes = 500
    folder, code = sample_runs["failing"]
    assert comment.main(action_env(github, folder, code, tmp_path / "summary.md")) == 1
    out = capsys.readouterr().out
    assert "::error" in out and "HTTP 500" in out and TOKEN not in out


def test_outside_a_pull_request_nothing_is_sent(github, sample_runs, tmp_path):
    folder, code = sample_runs["passing"]
    assert comment.main(action_env(github, folder, code, tmp_path / "summary.md", MARTIAN_EVALS_PR_NUMBER="")) == 0
    assert github.calls == []
    assert "### Martian evals passed" in (tmp_path / "summary.md").read_text(encoding="utf-8")
