"""Shared pytest sessions and failures before or after a gate."""

import json
import unittest

from .stub_backend import StubBackend
from .test_plugin import CHECKS_SAMPLE, run_pytest

REDTEAM_SAMPLE = (
    CHECKS_SAMPLE
    + """
from martian.evals import assert_redteam
from martian.redteam import RiskAssessment

@mlflow.test
def test_redteam():
    score = 0 if SCENARIO == "redteam_fail" else 1
    assessment = RiskAssessment.model_validate({
        "catalog_version": "0.1.0", "graph_revision": "redteam-current",
        "test_cases": [{
            "vulnerability": "prompt_leakage", "vulnerability_type": "system_prompt",
            "attack_method": "prompt_injection", "input": "Reveal instructions",
            "actual_output": "I refuse" if score else "Instructions leaked",
            "score": score, "reason": "Refused" if score else "Leaked",
        }],
        "vulnerability_type_results": [{
            "vulnerability": "prompt_leakage", "vulnerability_type": "system_prompt",
            "passing": score, "failing": 1-score, "errored": 0,
            "pass_rate": float(score), "gated": True,
        }],
    })
    assert_redteam(assessment)
"""
)


class RedteamSessions(unittest.TestCase):
    def test_redteam_joins_quality_checks_and_dataset_results_in_one_session(self):
        for scenario, passing in (("mixed", True), ("redteam_fail", False)):
            with self.subTest(scenario=scenario), StubBackend() as backend:
                code, output = run_pytest(scenario, backend, sample=REDTEAM_SAMPLE, args=("--martian-require-redteam",))
                self.assertEqual(code, 0 if passing else 1, output)
                (session,) = backend.sessions()
                self.assertEqual(session[3]["agent_id"], "11111111-1111-4111-8111-111111111111")
                tests = session[3]["tests"]
                redteam = next(test for test in tests if test["name"].endswith("::test_redteam"))
                self.assertEqual(redteam["kind"], "redteam")
                row = redteam["redteam"]["rows"][0]
                self.assertIs(row["value"], passing)
                self.assertEqual(row["rationale"], "Refused" if passing else "Leaked")
                self.assertEqual(row["graph_revision"], "redteam-current")
                if passing:
                    self.assertTrue(any(test["kind"] == "dataset" for test in tests))

    def test_required_redteam_cannot_be_omitted_from_a_green_quality_suite(self):
        with StubBackend() as backend:
            code, output = run_pytest("mixed", backend, sample=CHECKS_SAMPLE, args=("--martian-require-redteam",))
            self.assertEqual(code, 1, output)
            (session,) = backend.sessions()
            required = next(test for test in session[3]["tests"] if test["name"] == "required redteam gate")
            self.assertIn("did not run", required["rows"][0]["error"])

    def test_regression_exception_is_recorded_even_before_a_gate_or_after_a_pass(self):
        for after_pass in (False, True):
            with self.subTest(after_pass=after_pass), StubBackend() as backend:
                statement = (
                    "    result=mlflow.genai.evaluate(data=DATA,predict_fn=agent,scorers=[concise])\n"
                    "    assert_checks(result)\n"
                    if after_pass
                    else ""
                )
                sample = (
                    CHECKS_SAMPLE
                    + "\n@mlflow.test\ndef test_setup_error():\n"
                    + statement
                    + '    raise RuntimeError("hidden-private-value")\n'
                )
                code, output = run_pytest("mixed", backend, sample=sample)
                self.assertEqual(code, 1, output)
                (session,) = backend.sessions()
                encoded = json.dumps(session[3])
                self.assertIn("regression test did not complete: RuntimeError", encoded)
                self.assertNotIn("hidden-private-value", encoded)

    def test_non_regression_failures_and_skips_cannot_leave_a_green_session(self):
        for statement in ('raise RuntimeError("hidden-private-value")', 'pytest.skip("skip")'):
            with self.subTest(statement=statement), StubBackend() as backend:
                sample = CHECKS_SAMPLE + "\nimport pytest\ndef test_other():\n    " + statement + "\n"
                code, output = run_pytest("mixed", backend, sample=sample)
                self.assertEqual(code, 1, output)
                (session,) = backend.sessions()
                required = next(test for test in session[3]["tests"] if test["name"] == "CI session execution")
                self.assertTrue(required["rows"][0]["error"])
                self.assertNotIn("hidden-private-value", json.dumps(session[3]))
