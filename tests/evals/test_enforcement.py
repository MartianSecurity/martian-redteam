"""Fail-closed enforcement found by adversarial review, through real pytest sessions."""

import unittest

from .stub_backend import StubBackend
from .test_plugin import ENFORCEMENT_SAMPLE, run_pytest


def run(scenario, backend, **kw):
    return run_pytest(scenario, backend, sample=ENFORCEMENT_SAMPLE, **kw)


class Enforcement(unittest.TestCase):
    def test_a_test_that_never_reaches_a_gate_fails_the_session(self):
        with StubBackend() as backend:
            code, out = run("ungated", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("test_never_gated: gate failed: the test passed without reaching", out)
            # Both tests are recorded: the gated one passed, the ungated one is a failed placeholder.
            tests = backend.sessions()[0][3]["tests"]
            self.assertEqual(len(tests), 2)
            ungated = next(t for t in tests if t["name"].endswith("test_never_gated"))
            self.assertIn("without reaching", ungated["rows"][0]["error"])

    def test_an_unasserted_evaluate_result_fails(self):
        with StubBackend() as backend:
            code, out = run("unchecked", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("never passed to assert_gate or assert_checks", out)

    def test_deselected_regression_tests_fail_unless_partial_runs_are_allowed(self):
        with StubBackend() as backend:
            code, out = run("ungated", backend, args=("-k", "test_gated"))
            self.assertEqual(code, 1, out)
            self.assertIn("was deselected", out)
        with StubBackend() as backend:
            code, out = run("ungated", backend, args=("-k", "test_gated"), MARTIAN_EVALS_ALLOW_PARTIAL="1")
            self.assertEqual(code, 0, out)

    def test_two_scorers_with_one_name_cannot_hide_a_failure(self):
        with StubBackend() as backend:
            code, out = run("dupnames", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("must be unique", out)

    def test_narrowing_the_asserted_scorers_is_refused(self):
        with StubBackend() as backend:
            code, out = run("narrow", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("leaves out", out)

    def test_a_test_over_backends_row_limit_fails_before_upload(self):
        with StubBackend() as backend:
            code, out = run("toomany", backend)
            self.assertEqual(code, 1, out)
            self.assertIn("100-row limit", out)

    def test_a_dataset_gate_that_failed_locally_is_recorded_as_a_failed_test(self):
        with StubBackend() as backend:
            code, out = run("localfail", backend)
            self.assertEqual(code, 1, out)
            self.assertEqual(len(backend.uploads()), 1, "the run upload still happened")
            tests = backend.sessions()[0][3]["tests"]
            dataset_like = next(t for t in tests if t["name"].endswith("test_dataset_gate"))
            self.assertEqual(
                dataset_like["kind"], "dataset", "retain the Backend-scored run even if a local scorer failed"
            )
            failure = next(t for t in tests if t["name"].endswith("test_dataset_gate#runner"))
            self.assertEqual(failure["kind"], "checks")
            self.assertIn("gate", failure["rows"][0]["error"])

    def test_an_omitted_dataset_run_is_a_failed_placeholder_not_a_missing_test(self):
        with StubBackend() as backend:
            backend.upload_statuses = [500, 500, 500]  # the run upload keeps failing
            code, out = run("uploadfail", backend)
            self.assertEqual(code, 1, out)
            tests = backend.sessions()[0][3]["tests"]
            self.assertEqual(len(tests), 2)  # the gated test and the dataset test that could not upload
            lost = next(t for t in tests if t["name"].endswith("test_dataset_gate"))
            self.assertEqual(lost["kind"], "checks")
            self.assertIn("could not be uploaded", lost["rows"][0]["error"])
            self.assertIn("upload to Backend failed", out)
