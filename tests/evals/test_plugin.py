"""The kit end to end: real pytest, real MLflow 3.16.1 local store, loopback Backend stand-in."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .stub_backend import ITEMS, StubBackend

HERE = Path(__file__).parent
SAMPLE = (HERE / "sample_gate_test.py").read_text()
CHECKS_SAMPLE = (HERE / "sample_checks_test.py").read_text()
ENFORCEMENT_SAMPLE = (HERE / "sample_enforcement_test.py").read_text()


def run_pytest(scenario, backend=None, *, sample=None, workers=None, args=(), **env):
    with tempfile.TemporaryDirectory() as work:
        Path(work, "test_agent.py").write_text(sample or SAMPLE)
        environment = {k: v for k, v in os.environ.items() if not k.startswith(("MARTIAN_", "MLFLOW_", "GITHUB_"))}
        environment.update(
            SCENARIO=scenario,
            MARTIAN_REPORT_INCLUDE_CONTENT="1",
            MARTIAN_EVALS_STORE_DIR=str(Path(work, "store")),
            MARTIAN_AGENT_ID="11111111-1111-4111-8111-111111111111",
            MARTIAN_EVALS_GIT_SHA="0123456789abcdef0123456789abcdef01234567",
        )
        if backend is not None:
            environment.update(MARTIAN_API_URL=backend.url, MARTIAN_API_TOKEN=backend.token)
        environment.update(env)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-p",
                "martian.evals.plugin",
                "--martian-evals-gate",
                "-q",
                "test_agent.py",
                *(["-n", str(workers)] if workers else []),
                *args,
            ],
            cwd=work,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return proc.returncode, proc.stdout + proc.stderr


class Plugin(unittest.TestCase):
    def test_green_run_uploads_outputs_only_and_exits_zero(self):
        with StubBackend() as backend:
            code, out = run_pytest("pass", backend)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(backend.uploads()), 1)
            _, path, headers, body = backend.uploads()[0]
            self.assertEqual(path, "/v1/evals/ci/runs")
            self.assertEqual(headers["Authorization"], "Bearer mce_test-token")
            self.assertEqual({r["item_id"] for r in body["rows"]}, {i["id"] for i in ITEMS})
            self.assertEqual(body["dataset_id"], "ds-1")
            self.assertEqual(body["items_digest"], "a" * 64)
            self.assertNotIn("threshold", body)
            self.assertEqual(body["provenance"]["git_sha"], "0123456789abcdef0123456789abcdef01234567")
            # Backend holds expected text; the upload never carries prompts or expectations.
            flat = json.dumps(body)
            for forbidden in ("expected", "prompt", "Capital of France?", "Greet me"):
                self.assertNotIn(forbidden, flat)
            self.assertIn("EV-1001", out)
            # The session carries the dataset test, linked to the run Backend scored.
            session = backend.sessions()[0][3]
            self.assertEqual([t["kind"] for t in session["tests"]], ["dataset"])
            self.assertTrue(session["tests"][0]["name"].endswith("::test_gate"))
            self.assertIn(session["tests"][0]["run_id"], backend.run_gates)
            self.assertIn("verdict=passed", out)

    def test_agent_calls_are_made_once_per_item(self):
        # SKIP_TRACE_VALIDATION is set by the plugin, so the first row is not predicted twice.
        with StubBackend() as backend:
            code, out = run_pytest("pass", backend)
            self.assertEqual(code, 0, out)
            rows = backend.uploads()[0][3]["rows"]
            self.assertEqual(len(rows), 3)

    def test_failing_row_fails_the_job_and_is_still_recorded(self):
        with StubBackend() as backend:
            code, out = run_pytest("wrong", backend)
            self.assertEqual(code, 1, out)
            self.assertEqual(len(backend.uploads()), 1, "a failing run must still be uploaded")
            self.assertIn("failing row", out)
            self.assertIn("verdict=failed", out)

    def test_skipped_gate_is_a_failed_gate(self):
        with StubBackend() as backend:
            code, out = run_pytest("skip", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("skipped", out)
            self.assertEqual(backend.uploads(), [])

    def test_session_with_no_gate_fails(self):
        with StubBackend() as backend:
            code, out = run_pytest("zero", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("no assert_gate, assert_checks or assert_redteam call ran", out)
            (test,) = backend.sessions()[0][3]["tests"]
            self.assertEqual(test["name"], "CI session execution")
            self.assertIn("no assert_gate", test["rows"][0]["error"])

    def test_several_gates_in_one_session_are_each_recorded(self):
        with StubBackend() as backend:
            code, out = run_pytest("double", backend)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(backend.uploads()), 2)
            names = [t["name"] for t in backend.sessions()[0][3]["tests"]]
            self.assertEqual(len(names), 2)
            self.assertTrue(names[1].endswith("::test_gate#2"))

    def test_untraced_agent_fails_closed(self):
        with StubBackend() as backend:
            code, out = run_pytest("untraced", backend)
            self.assertEqual(code, 1, out)

    def test_agent_exception_is_an_error_row_not_a_pass(self):
        with StubBackend() as backend:
            code, out = run_pytest("raises", backend)
            self.assertEqual(code, 1, out)
            if backend.uploads():
                rows = {r["item_id"]: r for r in backend.uploads()[0][3]["rows"]}
                self.assertNotEqual(rows["i2"]["error"], "")
                self.assertEqual(rows["i2"]["output"], "")

    def test_a_client_scorer_alone_cannot_be_the_gate(self):
        # An always-true custom scorer used to pass locally while Backend failed the row. The gate
        # now refuses to run without exact_match, the only scorer Backend re-checks.
        with StubBackend() as backend:
            code, out = run_pytest("lenient", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("must include 'exact_match'", out)
            self.assertEqual(backend.uploads(), [])

    def test_threshold_is_forwarded_and_applied_by_the_server(self):
        with StubBackend() as backend:
            code, out = run_pytest("threshold", backend, MARTIAN_EVALS_THRESHOLD="0.5", MARTIAN_EVALS_MIN_COVERAGE="1")
            self.assertEqual(code, 0, out)  # 2 of 3 pass, threshold 0.5
            self.assertEqual(backend.uploads()[0][3]["threshold"], 0.5)
            code, out = run_pytest("threshold", backend, MARTIAN_EVALS_THRESHOLD="0.9")
            self.assertEqual(code, 1, out)

    def test_upload_retries_transient_failures_with_the_same_request_id(self):
        with StubBackend() as backend:
            backend.upload_statuses = [502, 503]
            code, out = run_pytest("pass", backend, GITHUB_RUN_ID="42", GITHUB_REPOSITORY="o/r")
            self.assertEqual(code, 0, out)
            ids = {r[3]["request_id"] for r in backend.uploads()}
            self.assertEqual(len(backend.uploads()), 3)
            self.assertEqual(len(ids), 1)

    def test_default_policy_is_strict_without_any_threshold(self):
        # Previously the server verdict was "unconfigured" here and only the client could fail.
        with StubBackend() as backend:
            code, out = run_pytest("wrong", backend)
            self.assertEqual(code, 1, out)
            gate = backend.uploads()[0]
            self.assertNotIn("threshold", gate[3])
            self.assertIn("Backend verdict is failed", out)

    def test_a_swallowed_gate_failure_still_fails_the_session(self):
        with StubBackend() as backend:
            code, out = run_pytest("swallow", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("gate failed:", out)

    def test_gate_must_include_exact_match(self):
        with StubBackend() as backend:
            code, out = run_pytest("emptyscorers", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("must include 'exact_match'", out)
            self.assertEqual(backend.uploads(), [])

    def test_structured_outputs_score_the_same_locally_and_on_the_server(self):
        with StubBackend() as backend:
            backend.items = [{**backend.items[0], "expected": "['Paris']"}, *backend.items[1:]]
            code, out = run_pytest("listout", backend)
            self.assertEqual(code, 1, out)
            row = {r["item_id"]: r for r in backend.uploads()[0][3]["rows"]}["i1"]
            self.assertEqual(row["output"], '["Paris"]')
            self.assertNotIn("disagree", out)  # both sides agree it fails

    def test_truncated_dataset_fails_before_upload(self):
        with StubBackend() as backend:
            backend.truncated = True
            code, out = run_pytest("pass", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("item cap", out)
            self.assertEqual(backend.uploads(), [])

    def test_oversized_output_is_an_error_row_not_a_rejected_upload(self):
        with StubBackend() as backend:
            code, out = run_pytest("bigout", backend)
            self.assertEqual(code, 1, out)
            row = {r["item_id"]: r for r in backend.uploads()[0][3]["rows"]}["i3"]
            self.assertEqual((row["output"], row["error"]), ("", "output too large"))

    def test_concurrent_conflict_is_retried(self):
        with StubBackend() as backend:
            backend.upload_statuses = [409]
            code, out = run_pytest("pass", backend)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(backend.uploads()), 2)

    def test_rate_limited_uploads_are_retried_after_the_advertised_delay(self):
        with StubBackend() as backend:
            backend.upload_statuses = [429, 429]
            code, out = run_pytest("pass", backend)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(backend.uploads()), 3)

    def test_a_null_verdict_body_fails_closed(self):
        with StubBackend() as backend:
            backend.null_response = True
            code, out = run_pytest("pass", backend)
            self.assertEqual(code, 1, out)

    def test_missing_commit_is_refused_before_running(self):
        with StubBackend() as backend:
            code, out = run_pytest("pass", backend, MARTIAN_EVALS_GIT_SHA="")
            self.assertNotEqual(code, 0, out)
            self.assertIn("no commit to attribute", out)
            self.assertEqual(backend.uploads(), [])

    def test_upload_failure_fails_the_job(self):
        with StubBackend() as backend:
            backend.upload_statuses = [500, 500, 500]
            code, out = run_pytest("pass", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("upload to Backend failed", out)

    def test_rejected_token_fails_before_any_agent_call_result_is_trusted(self):
        with StubBackend() as backend:
            code, out = run_pytest("pass", backend, MARTIAN_API_TOKEN="mce_wrong")
            self.assertNotEqual(code, 0, out)
            self.assertEqual(backend.uploads(), [])

    def test_missing_secrets_fail_instead_of_skipping(self):
        code, out = run_pytest("pass", None)
        self.assertNotEqual(code, 0, out)
        self.assertIn("MARTIAN_API_TOKEN", out)

    def test_remote_tracking_uri_is_refused(self):
        with StubBackend() as backend:
            code, out = run_pytest("pass", backend, MLFLOW_TRACKING_URI="https://mlflow.example.com")
            self.assertNotEqual(code, 0, out)
            self.assertIn("MLFLOW_TRACKING_URI", out)

    def test_offline_mode_gates_locally_and_never_uploads(self):
        with tempfile.TemporaryDirectory() as work:
            fixture = Path(work, "dataset.json")
            fixture.write_text(
                json.dumps({"dataset_id": "ds-1", "name": "support", "items_digest": "a" * 64, "items": ITEMS})
            )
            code, out = run_pytest("pass", None, MARTIAN_EVALS_OFFLINE="1", MARTIAN_EVALS_DATASET_FILE=str(fixture))
            self.assertEqual(code, 0, out)
            code, out = run_pytest("wrong", None, MARTIAN_EVALS_OFFLINE="1", MARTIAN_EVALS_DATASET_FILE=str(fixture))
            self.assertEqual(code, 1, out)


if __name__ == "__main__":
    unittest.main()
