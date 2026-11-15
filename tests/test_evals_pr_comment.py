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
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"
RUN_URL = "https://github.com/example/agent/actions/runs/1"
TOKEN = "test-token"
BOT = "github-actions[bot]"
SUITE = "examples/github-action/failing/evals.yaml"
MATRIX_SUITE = "examples/evals/matrix/evals.yaml"

_spec = importlib.util.spec_from_file_location("evals_pr_comment", ROOT / "scripts/evals_pr_comment.py")
assert _spec is not None and _spec.loader is not None
comment = importlib.util.module_from_spec(_spec)
sys.modules["evals_pr_comment"] = comment
_spec.loader.exec_module(comment)


@pytest.fixture(scope="module")
def sample_runs(tmp_path_factory):
    """Run both sample suites once through the CLI, with no Martian setting, as the action does,
    and the failing one again with the passing one's output folder as its baseline, as a pull
    request that broke two of the passing suite's cases would be compared with main; then the
    matrix example, once and again with that run as its baseline."""
    runs = {}
    with pytest.MonkeyPatch.context() as patch:
        for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID"):
            patch.delenv(name, raising=False)
        patch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
        # Runs are recorded in the local edition's data folder; never the real one in a test.
        patch.setenv("MARTIAN_EVALS_HOME", str(tmp_path_factory.mktemp("evals-home")))
        patch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
        old_uri = mlflow.get_tracking_uri()
        for name, suite, baseline in (("passing", "passing", None), ("failing", "failing", None)) + (
            ("compared", "failing", "passing"),
            ("matrix", MATRIX_SUITE, None),
            ("matrix-compared", MATRIX_SUITE, "matrix"),
        ):
            STATE.reset()
            folder = tmp_path_factory.mktemp(name) / "results"
            extra = ["--baseline", str(runs[baseline][0])] if baseline else []
            path = ROOT / suite if suite == MATRIX_SUITE else SAMPLE / suite / "evals.yaml"
            result = CliRunner().invoke(app, ["evals", "run", str(path), "--output", str(folder), *extra])
            runs[name] = (folder, result.exit_code)
        STATE.reset()
        mlflow.set_tracking_uri(old_uri)
    return runs


@pytest.mark.parametrize(
    ("name", "suite", "exit_code", "expected", "baseline"),
    [
        ("passing", "passing", 0, "passed.md", None),
        ("failing", "failing", 1, "failed.md", None),
        ("compared", "failing", 1, "compared.md", comment.Baseline("main", BASE_SHA)),
    ],
)
def test_sample_suites_render_the_checked_in_comments(sample_runs, name, suite, exit_code, expected, baseline):
    folder, code = sample_runs[name]
    assert code == exit_code
    suite = f"examples/github-action/{suite}/evals.yaml"
    outcome = comment.load_outcome(folder, code)
    body = comment.render(outcome, suite=suite, key=suite, sha=SHA, run_url=RUN_URL, baseline=baseline)
    assert body == (SAMPLE / "comments" / expected).read_text(encoding="utf-8")


def test_a_pull_request_that_breaks_cases_is_reported_with_each_case_named(sample_runs):
    """The failing sample is the passing one with two replies broken so that they leak: against
    the passing run as its baseline two of three changed cases are within noise, but each newly
    fails a security check, which fails the gate on its own, and both are named with their check
    and reason."""
    folder, code = sample_runs["compared"]
    outcome = comment.load_outcome(folder, code)
    assert outcome.comparison is not None
    assert (outcome.comparison["label"], outcome.comparison["gate"], code) == ("within_noise", "failed", 1)
    leaks = [
        {"id": "declines-prompt-dump", "checks": {"no_secret_leak": "1 of 1 forbidden values found"}},
        {"id": "answers-safely", "checks": {"no_api_key": "pattern found"}},
    ]
    assert outcome.comparison["regressed"] == leaks
    assert outcome.comparison["security_regressed"] == leaks
    body = comment.render(outcome, suite=SUITE, key=SUITE, baseline=comment.Baseline("main"))
    rows = body.splitlines()
    assert rows[1] == "### Martian evals failed against `main`"
    table = rows.index("#### Cases that newly failed a security check, which fails the gate")
    assert rows[table + 4 : table + 6] == [
        "| `declines-prompt-dump` | `no_secret_leak` | `1 of 1 forbidden values found` |",
        "| `answers-safely` | `no_api_key` | `pattern found` |",
    ]
    # The cases named there are not listed again, and nothing else failed.
    assert "Cases that went from pass to fail" not in body
    assert "Failed cases" not in body and "Other failed cases" not in body
    # The same folder without the action's baseline still shows the comparison it holds.
    assert "### Martian evals failed against the baseline" in comment.render(outcome, suite=SUITE, key=SUITE)


def test_a_case_that_newly_fails_a_security_check_is_named_once_as_the_gate_s_reason():
    counts = {"passed": 36, "failed": 4, "errors": 0, "pass_rate": 0.9, "confidence_95": [0.77, 0.96]}
    summary = {"cases": 40, **counts, "groups": None, "checks": {"no_leak": counts, "answers": counts}}
    significance = {
        "label": "within_noise",
        "test": "mcnemar_exact",
        "p_value": 0.25,
        "alpha": 0.05,
        "compared": 40,
        "changed": 2,
        "improved": 0,
        "regressed": 2,
        "baseline": 0.95,
        "candidate": 0.9,
        "effect": -0.05,
        "minimum_detectable_effect": None,
    }
    leak = "1 of 1 forbidden values found"
    comparison = {
        "baseline": "/tmp/baseline",
        "label": "within_noise",
        "significance": significance,
        "regressed": [
            {"id": "s1", "checks": {"no_leak": leak, "answers": "1 of 1 values not found"}},
            {"id": "q1", "checks": {"answers": "1 of 1 values not found"}},
        ],
        "improved": [],
        # s2 failed on main too, on another check, and now also leaks.
        "security_regressed": [{"id": "s1", "checks": {"no_leak": leak}}, {"id": "s2", "checks": {"no_leak": None}}],
        "new": [],
        "changed": [],
        "unverified": [],
        "missing": [],
        "unpaired_failures": [],
        "checks": {"added": [], "removed": [], "changed": []},
        "attempts": {"baseline": 1, "candidate": 1},
        "gate": "failed",
    }
    cases = [
        {"id": case_id, "passed": False, "checks": {"answers": {"passed": False}}}
        for case_id in ("s1", "s2", "q1", "q9")
    ]
    body = comment.render(
        comment.Outcome(1, summary, cases, comparison), suite=SUITE, key=SUITE, baseline=comment.Baseline("main")
    )
    rows = body.splitlines()
    assert rows[1] == "### Martian evals failed against `main`"
    assert (
        "The gate fails on a change worse than noise, on a case that newly fails a security check whatever the "
        "label, or on a failed case with no result on `main` to compare with."
    ) in body
    security = rows.index("#### Cases that newly failed a security check, which fails the gate")
    assert rows[security + 2 : security + 6] == [
        "| Case | Security checks that failed | Reason |",
        "| --- | --- | --- |",
        f"| `s1` | `no_leak` | `{leak}` |",
        "| `s2` | `no_leak` | — |",
    ]
    regressed = rows.index("#### Cases that went from pass to fail")
    assert rows[regressed + 4 :][:2] == ["| `q1` | `answers` | `1 of 1 values not found` |", ""]
    other = rows.index("#### Other failed cases")
    assert rows[other + 4 :][:2] == ["| `q9` | `answers` |", ""]


def test_without_a_baseline_yet_the_comment_says_so_and_the_run_is_gated_on_its_own(sample_runs):
    folder, code = sample_runs["failing"]
    body = comment.render(comment.load_outcome(folder, code), suite=SUITE, key=SUITE, baseline=comment.Baseline("main"))
    assert body.splitlines()[1] == "### Martian evals failed"
    assert (
        "**Compared with `main`: no baseline yet.** No result of `main` is saved for this suite, so this run "
        "is gated on its own and any failed case fails it. The next push to `main` saves one."
    ) in body
    # Without the action's baseline branch nothing is said about a baseline.
    assert "baseline" not in comment.render(comment.load_outcome(folder, code), suite=SUITE, key=SUITE)


def test_a_matrix_suite_shows_one_row_per_combination(sample_runs):
    folder, code = sample_runs["matrix"]
    assert code == 1
    outcome = comment.load_outcome(folder, code)
    assert [one.id for one in outcome.combinations] == [
        "model=small-model,prompt=terse",
        "model=small-model,prompt=friendly",
        "model=large-model,prompt=terse",
        "model=large-model,prompt=friendly",
    ]
    body = comment.render(outcome, suite=MATRIX_SUITE, key=MATRIX_SUITE, sha=SHA, run_url=RUN_URL)
    assert body == (SAMPLE / "comments" / "matrix.md").read_text(encoding="utf-8")


def test_each_combination_is_compared_with_the_same_combination_of_main(sample_runs):
    """The matrix example against a result of itself: each combination has the failures it had on
    main, which do not fail the gate, and its failed cases are still named."""
    folder, code = sample_runs["matrix-compared"]
    assert code == 0
    body = comment.render(
        comment.load_outcome(folder, code), suite=MATRIX_SUITE, key=MATRIX_SUITE, baseline=comment.Baseline("main")
    )
    lines = body.splitlines()
    assert lines[1] == "### Martian evals passed against `main`"
    assert lines[3].endswith("4 combinations of `model` and `prompt`, 6 cases each; 4 passed.")
    assert "**Compared with `main`:** each combination is compared with the same combination there" in body
    assert "| Combination | Passed | Failed | Errors | Pass rate (95% CI) | Compared with `main` | Result |" in lines
    assert "| `model=small-model,prompt=friendly` | 3 | 3 | 0 | 50% (18–82%) | within noise | passed |" in lines
    assert "Failed cases: `warranty` (`states_the_fact`), `payment` (`states_the_fact`)." in lines


def test_a_matrix_comment_shows_repeats_false_refusals_regressions_and_a_combination_without_a_baseline():
    counts = {"passed": 7, "failed": 1, "errors": 0, "pass_rate": 0.875, "confidence_95": [0.53, 0.98]}
    repeats = {"attempts": 3, "combine": "all", "checks": {"no_refusal": {"combine": "all", "from": "suite"}}}
    refusals = {"checks": ["no_refusal"], "benign": 4, "refused": 1, "errors": 0, "rate": 0.25}
    summary = {
        "cases": 8,
        **counts,
        "groups": None,
        "checks": {"no_refusal": counts},
        "repeats": repeats,
        "reliability": {**counts, "passed": 6, "failed": 2, "pass_rate": 0.75, "flaky": ["q2"]},
        "false_refusals": refusals,
    }
    regressed = {
        "label": "within_noise",
        "regressed": [{"id": "q1", "checks": {"no_refusal": None}}, {"id": "q3", "checks": {"no_secrets": None}}],
        "security_regressed": [{"id": "q3", "checks": {"no_secrets": None}}],
        "unpaired_failures": [],
        "gate": "passed",
    }
    cases = [{"id": "q1", "passed": False, "checks": {"no_refusal": {"passed": False}}}]
    outcome = comment.Outcome(
        0,
        None,
        axes=[{"name": "model", "values": ["a", "b"]}],
        combinations=[
            comment.Combination("model=a", "passed", summary, regressed, cases),
            comment.Combination("model=b", "failed", summary, None, cases),
        ],
    )
    body = comment.render(outcome, suite=MATRIX_SUITE, key=MATRIX_SUITE, baseline=comment.Baseline("main"))
    lines = body.splitlines()
    assert (
        "| Combination | Passed | Failed | Errors | Pass rate (95% CI) | Reliability (95% CI) | False refusals "
        "| Compared with `main` | Result |"
    ) in lines
    assert "| `model=a` | 7 | 1 | 0 | 88% (53–98%) | 75% (53–98%) | 1 of 4 (25.0%) | within noise | passed |" in lines
    assert (
        "| `model=b` | 7 | 1 | 0 | 88% (53–98%) | 75% (53–98%) | 1 of 4 (25.0%) | no baseline yet | failed |" in lines
    )
    assert "on a case that newly fails a security check whatever the label" in body
    assert "A combination with no result on `main` is gated on its own." in body
    assert "Each case ran 3 times" in body and "False refusals count the benign cases that were refused." in body
    # A case that newly failed a security check comes first; a case named once is not named again.
    section = lines[lines.index("#### `model=a`") + 2]
    assert section.startswith(
        "Newly failed a security check, which fails the gate: `q3` (`no_secrets`). "
        "From pass to fail: `q1` (`no_refusal`). Reliability counts"
    )
    assert "Failed" not in section
    assert lines[lines.index("#### `model=b`") + 2].startswith("Failed cases: `q1` (`no_refusal`).")


def test_a_comparison_says_what_fails_the_gate_and_what_it_could_not_pair():
    counts = {"passed": 30, "failed": 10, "errors": 0, "pass_rate": 0.75, "confidence_95": [0.6, 0.86]}
    summary = {"cases": 40, **counts, "groups": None, "checks": {"judge": counts}}
    significance = {
        "label": "worse",
        "test": "mcnemar_exact",
        "p_value": 1058 / 2**32,
        "alpha": 0.05,
        "compared": 40,
        "changed": 32,
        "improved": 2,
        "regressed": 30,
        "baseline": 0.95,
        "candidate": 0.25,
        "effect": -0.7,
        "minimum_detectable_effect": 0.15,
    }
    comparison = {
        "baseline": "/tmp/baseline",
        "label": "worse",
        "significance": significance,
        "regressed": [{"id": f"q{n}", "checks": {"judge": None}} for n in range(30)],
        "improved": ["q40"],
        "security_regressed": [],
        "new": ["n1", "n2"],
        "changed": ["c1"],
        "unverified": [],
        "missing": ["gone"],
        "unpaired_failures": ["n2"],
        "checks": {"added": ["judge"], "removed": ["old"], "changed": []},
        "attempts": {"baseline": 1, "candidate": 3},
        "gate": "failed",
    }
    outcome = comment.Outcome(1, summary, comparison=comparison)
    body = comment.render(outcome, suite=SUITE, key=SUITE, baseline=comment.Baseline("main", SHA))
    assert body.splitlines()[1] == "### Martian evals failed against `main`"
    assert (
        "**Compared with `main` at `0123456`: worse, beyond noise.** Of 40 cases compared, 30 went from pass to "
        "fail and 2 from fail to pass (exact McNemar test, p < 0.001). The smallest change of the pass rate it can "
        "tell from chance is 15 points."
    ) in body
    assert (
        "Not compared: 2 cases new since `main`, 1 case with changed inputs or expectations. Failed with no result "
        "to compare with, which fails the gate: `n2`. 1 case of `main` did not run here. The suite changed since "
        "`main` (added `judge`; removed `old`; attempts per case 1 on `main` and 3 here), so the label measures "
        "that change too."
    ) in body
    # A judge's reason is never shown, and the table stops at 25 cases.
    assert "| `q0` | `judge` | — |" in body and "`q25`" not in body
    assert "5 more; comparison.json in the output folder has every case." in body
    assert "— is a reason that could quote the reply" in body


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


def test_the_action_passes_the_baseline_branch_and_commit_to_the_comment(github, sample_runs, tmp_path):
    folder, code = sample_runs["compared"]
    env = action_env(
        github,
        folder,
        code,
        tmp_path / "summary.md",
        MARTIAN_EVALS_BASELINE_BRANCH="main",
        MARTIAN_EVALS_BASELINE_SHA=BASE_SHA,
    )
    assert comment.main(env) == 0
    [posted] = github.comments
    assert posted["body"] == (SAMPLE / "comments/compared.md").read_text(encoding="utf-8")
