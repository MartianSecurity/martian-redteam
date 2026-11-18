"""MLflow-style regression suites through the kit: many tests, scorer checks, xdist."""

import unittest

from .stub_backend import StubBackend
from .test_plugin import CHECKS_SAMPLE, run_pytest


def run(scenario, backend=None, **kw):
    return run_pytest(scenario, backend, sample=CHECKS_SAMPLE, **kw)


class ChecksSessions(unittest.TestCase):
    def test_two_regression_tests_become_one_recorded_session(self):
        with StubBackend() as backend:
            code, out = run("pass", backend)
            self.assertEqual(code, 0, out)
            self.assertEqual(backend.uploads(), [])  # checks tests upload no dataset run
            (session,) = backend.sessions()
            tests = session[3]["tests"]
            self.assertEqual(len(tests), 2)
            self.assertTrue(all(t["kind"] == "checks" for t in tests))
            first = tests[0]
            self.assertTrue(first["name"].endswith("::test_answers_in_english"))
            self.assertEqual(len(first["rows"]), 2)
            scores = first["rows"][0]["scores"]
            # Judge-style "yes" and plain booleans both become booleans; rationales survive.
            self.assertEqual(scores["is_english"], {"value": True, "rationale": "English greeting"})
            self.assertEqual(scores["concise"]["value"], True)
            self.assertEqual(first["rows"][0]["input"], '{"question": "What are your hours?"}')
            self.assertIn("tests=2/2", out)

    def test_a_failing_judge_fails_the_job_but_the_session_is_still_recorded(self):
        with StubBackend() as backend:
            code, out = run("french", backend)
            self.assertEqual(code, 1, out)
            (session,) = backend.sessions()
            english = session[3]["tests"][0]
            self.assertEqual(english["rows"][0]["scores"]["is_english"]["value"], False)
            self.assertEqual(english["rows"][0]["scores"]["is_english"]["rationale"], "Not English")
            self.assertIn("verdict=failed", out)
            self.assertIn("failing check", out)

    def test_numbers_need_a_pass_if_and_are_never_assumed_passing(self):
        with StubBackend() as backend:
            code, out = run("numeric", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("failing check", out)
        with StubBackend() as backend:
            code, out = run("numeric_pass_if", backend)
            self.assertEqual(code, 0, out)
            row = backend.sessions()[0][3]["tests"][1]["rows"][0]
            self.assertEqual(row["scores"]["sentiment"]["value"], True)
            self.assertIn("value=0.9", row["scores"]["sentiment"]["rationale"])

    def test_a_scorer_with_no_value_is_a_failure(self):
        with StubBackend() as backend:
            code, out = run("missing", backend)
            self.assertEqual(code, 1, out)

    def test_dataset_and_checks_tests_share_one_session(self):
        with StubBackend() as backend:
            code, out = run("mixed", backend)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(backend.uploads()), 1)
            kinds = sorted(t["kind"] for t in backend.sessions()[0][3]["tests"])
            self.assertEqual(kinds, ["checks", "checks", "dataset"])
            session_tests = backend.sessions()[0][3]["tests"]
            dataset = next(t for t in session_tests if t["kind"] == "dataset")
            self.assertIn(dataset["run_id"], backend.run_gates)

    def test_session_upload_failure_fails_the_job(self):
        with StubBackend() as backend:
            backend.session_statuses = [500, 500, 500]
            code, out = run("pass", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("session upload to Backend failed", out)

    def test_offline_mode_runs_checks_without_a_backend(self):
        code, out = run("pass", None, MARTIAN_EVALS_OFFLINE="1")
        self.assertEqual(code, 0, out)
        code, out = run("french", None, MARTIAN_EVALS_OFFLINE="1")
        self.assertEqual(code, 1, out)


class Parallel(unittest.TestCase):
    def test_xdist_workers_hand_their_gates_to_one_session(self):
        with StubBackend() as backend:
            code, out = run("pass", backend, workers=2)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(backend.sessions()), 1, "the controller uploads once")
            names = sorted(t["name"].split("::")[-1] for t in backend.sessions()[0][3]["tests"])
            self.assertEqual(names, ["test_answers_in_english", "test_tone"])

    def test_a_failure_on_a_worker_fails_the_controller(self):
        with StubBackend() as backend:
            code, out = run("french", backend, workers=2)
            self.assertEqual(code, 1, out)
            self.assertEqual(len(backend.sessions()), 1)
            self.assertIn("verdict=failed", out)

    def test_skips_are_still_counted_under_xdist(self):
        # SCENARIO=skip is handled by the dataset sample, which has a skipping test.
        from .test_plugin import SAMPLE

        with StubBackend() as backend:
            code, out = run_pytest("skip", backend, sample=SAMPLE, workers=2)
            self.assertEqual(code, 1, out)
            self.assertIn("skipped", out)


if __name__ == "__main__":
    unittest.main()
