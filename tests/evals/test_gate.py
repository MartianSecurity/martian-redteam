"""assert_gate on hand-built result frames: every fail-open path of `result.passed` is closed."""

import unittest
from types import SimpleNamespace as NS

import pandas as pd

from martian_evals_kit import GateFailure, assert_checks, assert_gate
from martian_evals_kit.dataset import EvalDataset, Pin
from martian_evals_kit.state import STATE


def dataset(n=3):
    data = EvalDataset(
        {
            "inputs": {"message": f"q{i}"},
            "expectations": {"expected_response": f"a{i}"},
            "tags": {"martian_item_id": f"i{i}"},
        }
        for i in range(1, n + 1)
    )
    data.pin = Pin("ds-1", "support", "a" * 64, False, 100)
    return data


def frame(values, *, state="OK", responses=None):
    n = len(values)
    return pd.DataFrame(
        {
            "tags": [{"martian_item_id": f"i{i}"} for i in range(1, n + 1)],
            "response": responses if responses is not None else [f"a{i}" for i in range(1, n + 1)],
            "state": [state] * n if isinstance(state, str) else state,
            "execution_duration": [10 + i for i in range(n)],
            "exact_match/value": values,
            # MLflow adds a column per expectation; it must not be mistaken for a scorer.
            "expected_response/value": [f"a{i}" for i in range(1, n + 1)],
        }
    )


class Gate(unittest.TestCase):
    def setUp(self):
        STATE.reset()

    def test_all_pass_returns_summary_and_records_rows_without_expectations(self):
        summary = assert_gate(NS(result_df=frame([True, True, True]), passed=False), dataset())
        self.assertEqual(summary["scorers"]["exact_match"]["pass_rate"], 1.0)
        record = STATE.records[0]
        self.assertEqual([r["item_id"] for r in record.rows], ["i1", "i2", "i3"])
        self.assertEqual(record.rows[0], {"item_id": "i1", "output": "a1", "error": "", "latency_ms": 10.0})
        self.assertNotIn("expected", str(record.rows))

    def test_result_passed_is_not_consulted(self):
        # MLflow reports passed=False here only because of the expectation column.
        assert_gate(NS(result_df=frame([True, True, True]), passed=False), dataset())

    def test_missing_frame_is_a_failure(self):
        with self.assertRaises(GateFailure):
            assert_gate(NS(result_df=None, passed=True), dataset())
        self.assertIsNone(STATE.records[0].rows)

    def test_row_count_and_ids_must_match_the_pull(self):
        with self.assertRaises(GateFailure):
            assert_gate(NS(result_df=frame([True, True]), passed=True), dataset(3))
        bad = frame([True, True, True])
        bad["tags"] = [{"martian_item_id": "i1"}, {"martian_item_id": "i1"}, {"martian_item_id": "zz"}]
        with self.assertRaises(GateFailure):
            assert_gate(NS(result_df=bad, passed=True), dataset())

    def test_missing_scorer_column_fails(self):
        df = frame([True, True, True]).drop(columns=["exact_match/value"])
        with self.assertRaises(GateFailure) as raised:
            assert_gate(NS(result_df=df, passed=True), dataset())
        self.assertIn("exact_match/value", str(raised.exception))

    def test_null_scores_are_missing_not_passing(self):
        with self.assertRaises(GateFailure):
            assert_gate(NS(result_df=frame([True, None, True]), passed=True), dataset())

    def test_failing_row_is_named_by_item_id_only(self):
        with self.assertRaises(GateFailure) as raised:
            assert_gate(NS(result_df=frame([True, False, True]), passed=False), dataset())
        self.assertIn("exact_match:i2", str(raised.exception))
        self.assertNotIn("q2", str(raised.exception))
        self.assertEqual(STATE.records[0].summary["scorers"]["exact_match"]["passed"], 2)

    def test_threshold_and_coverage(self):
        df = frame([True, True, False])
        assert_gate(NS(result_df=df), dataset(), threshold=0.6)
        STATE.reset()
        with self.assertRaises(GateFailure):
            assert_gate(NS(result_df=df), dataset(), threshold=0.9)
        STATE.reset()
        partial = frame([True, True, None])
        with self.assertRaises(GateFailure, msg="missing required scores cannot be excused by a lower coverage policy"):
            assert_gate(NS(result_df=partial), dataset(), threshold=0.9, minimum_coverage=0.6)
        STATE.reset()
        with self.assertRaises(GateFailure):
            assert_gate(NS(result_df=partial), dataset(), threshold=0.9, minimum_coverage=1.0)

    def test_failed_agent_call_is_an_error_even_if_a_scorer_passed(self):
        df = frame([True, True, True], state=["OK", "ERROR", "OK"], responses=["a1", None, "a3"])
        with self.assertRaises(GateFailure):
            assert_gate(NS(result_df=df), dataset(), threshold=0.5, minimum_coverage=0.0)
        row = STATE.records[0].rows[1]
        self.assertEqual((row["output"], row["error"]), ("", "agent call failed"))

    def test_scorers_must_include_exact_match(self):
        for scorers in ((), ("always_true",)):
            with self.assertRaises(ValueError):
                assert_gate(NS(result_df=frame([True, True, True])), dataset(), scorers=scorers)

    def test_truncated_dataset_fails_even_when_every_row_passes(self):
        data = dataset()
        data.pin = Pin("ds-1", "support", "a" * 64, True, 100)
        with self.assertRaises(GateFailure) as raised:
            assert_gate(NS(result_df=frame([True, True, True])), data)
        self.assertIn("100-item cap", str(raised.exception))

    def test_oversized_output_becomes_an_error_row(self):
        df = frame([True, True, True], responses=["a" * 20_001, "a2", "a3"])
        with self.assertRaises(GateFailure):
            assert_gate(NS(result_df=df), dataset())
        row = STATE.records[0].rows[0]
        self.assertEqual((row["output"], row["error"]), ("", "output too large"))

    def test_non_string_outputs_are_serialised_deterministically(self):
        df = frame([True, True, True], responses=[{"b": 1, "a": 2}, "a2", "a3"])
        assert_gate(NS(result_df=df), dataset(), threshold=0.0)
        self.assertEqual(STATE.records[0].rows[0]["output"], '{"a": 2, "b": 1}')


def check_frame(**columns):
    n = len(next(iter(columns.values())))
    data = {"response": ["ok"] * n, "state": ["OK"] * n, "request": [{"q": f"q{i}"} for i in range(n)]}
    data.update({f"{k}/value": v for k, v in columns.items()})
    data["assessments"] = [
        [{"assessment_name": k, "feedback": {"value": v[i]}} for k, v in columns.items()]
        + [{"assessment_name": "expected", "expectation": {"value": "x"}}]
        for i in range(n)
    ]
    return pd.DataFrame(data)


def ran(df, *names, **extra):
    """A result as the plugin leaves it: the scorers evaluate() was given are recorded on it."""
    return NS(result_df=df, _martian_scorers=list(names) or None, **extra)


class Checks(unittest.TestCase):
    def setUp(self):
        STATE.reset()

    def test_booleans_judge_strings_and_numpy_booleans_decide_by_value(self):
        import numpy as np

        df = check_frame(a=[True, np.True_], b=["yes", "YES"], c=["no", "yes"])
        with self.assertRaises(GateFailure) as raised:
            assert_checks(ran(df, "a", "b", "c"))
        self.assertIn("row 1:c", str(raised.exception))
        self.assertNotIn("row 1:a", str(raised.exception))
        ok = check_frame(a=[np.True_], b=["yes"])
        self.assertEqual(assert_checks(ran(ok, "a", "b"))["failing"], 0)

    def test_unrecognised_values_fail_unless_a_predicate_decides(self):
        df = check_frame(sentiment=[0.9, 0.95], label=["fine", "fine"])
        with self.assertRaises(GateFailure):
            assert_checks(ran(df, "sentiment", "label"))
        STATE.reset()
        assert_checks(
            ran(df, "sentiment", "label"),
            pass_if={"sentiment": lambda v: v >= 0.8, "label": lambda v: v == "fine"},
        )
        self.assertTrue(STATE.records[0].check_rows[0]["scores"]["sentiment"]["rationale"].startswith("sha256:"))
        STATE.reset()
        with self.assertRaises(GateFailure):
            assert_checks(
                ran(df, "sentiment", "label"), pass_if={"sentiment": lambda v: 1 / 0, "label": lambda v: True}
            )

    def test_expectations_are_not_scorers_and_an_absent_scorer_fails(self):
        df = check_frame(a=[True, True])
        df["expected/value"] = ["x", "x"]
        self.assertEqual(assert_checks(ran(df, "a"))["scorers"], ["a"])
        STATE.reset()
        with self.assertRaises(GateFailure) as raised:
            assert_checks(ran(df, "a", "silent"))
        self.assertIn("silent", str(raised.exception))
        STATE.reset()
        with self.assertRaises(GateFailure):
            assert_checks(ran(None, "a"))
        with self.assertRaises(GateFailure):
            assert_checks(ran(pd.DataFrame(), "a"))

    def test_predicate_must_return_a_boolean_not_a_truthy_label(self):
        with self.assertRaises(GateFailure):
            assert_checks(ran(check_frame(a=[0.9]), "a"), pass_if={"a": lambda value: "false"})
        self.assertTrue(STATE.records[0].failure)
        self.assertIs(STATE.records[0].check_rows[0]["scores"]["a"]["value"], False)

    def test_the_scorers_must_be_known_unique_and_not_silently_narrowed(self):
        df = check_frame(good=[True], other=[False])
        # Without the recorded list or an explicit one, the frame is not trusted to name scorers.
        with self.assertRaises(GateFailure) as raised:
            assert_checks(NS(result_df=df))
        self.assertIn("cannot tell which scorers ran", str(raised.exception))
        STATE.reset()
        # Narrowing to the passing scorer would hide `other`; it is refused unless opted into.
        with self.assertRaises(GateFailure) as raised:
            assert_checks(ran(df, "good", "other"), scorers=["good"])
        self.assertIn("leaves out", str(raised.exception))
        STATE.reset()
        assert_checks(ran(df, "good", "other"), scorers=["good"], allow_subset=True)
        STATE.reset()
        # Two scorers named alike share one column; the last one wins, so this can never pass.
        with self.assertRaises(GateFailure) as raised:
            assert_checks(ran(check_frame(quality=[True]), "quality", "quality"))
        self.assertIn("must be unique", str(raised.exception))
        STATE.reset()
        with self.assertRaises(GateFailure):
            assert_checks(ran(check_frame(quality=[True]), "quality", "<unnamed>"))

    def test_agent_errors_fail_even_when_scorers_passed_and_bounds_are_applied(self):
        df = check_frame(a=[True, True])
        df.loc[1, "state"] = "ERROR"
        df.loc[1, "response"] = None
        with self.assertRaises(GateFailure):
            assert_checks(ran(df, "a"))
        row = STATE.records[0].check_rows[1]
        self.assertEqual((row["output"], row["error"]), ("", "agent call failed"))
        STATE.reset()
        long = check_frame(a=[True])
        long["response"] = ["y" * 30000]
        long["request"] = [{"q": "z" * 9000}]
        assert_checks(ran(long, "a"))
        record = STATE.records[0].check_rows[0]
        self.assertTrue(record["output"].startswith("sha256:"))
        self.assertTrue(record["input"].startswith("sha256:"))

    def test_backend_bounds_are_enforced_here_and_hostile_text_is_neutralised(self):
        many = check_frame(a=[True] * 101)
        with self.assertRaises(GateFailure) as raised:
            assert_checks(ran(many, "a"))
        self.assertIn("100-row limit", str(raised.exception))
        STATE.reset()
        df = check_frame(a=[True])
        df["response"] = ["bad\x00text\ud800end"]
        df["a/rationale"] = ["why\x00"]
        assert_checks(ran(df, "a"))
        row = STATE.records[0].check_rows[0]
        self.assertNotIn("\x00", row["output"] + row["scores"]["a"]["rationale"])
        row["output"].encode("utf-8")  # no surrogate survives
        STATE.reset()
        from martian_evals_kit.gate import _safe_name

        self.assertEqual(len(_safe_name("t" * 400)), 300)
        self.assertEqual(_safe_name("short"), "short")

    def test_inputs_and_outputs_can_be_replaced_by_digests(self):
        import os

        df = check_frame(a=[True])
        os.environ["MARTIAN_EVALS_REDACT_INPUTS"] = "1"
        os.environ["MARTIAN_EVALS_REDACT_OUTPUTS"] = "1"
        try:
            assert_checks(ran(df, "a"))
        finally:
            del os.environ["MARTIAN_EVALS_REDACT_INPUTS"]
            del os.environ["MARTIAN_EVALS_REDACT_OUTPUTS"]
        row = STATE.records[0].check_rows[0]
        self.assertTrue(row["input"].startswith("sha256:") and row["output"].startswith("sha256:"))
        self.assertNotIn("q0", row["input"])


class GateConsistency(unittest.TestCase):
    def setUp(self):
        STATE.reset()

    def test_a_bare_number_is_not_a_pass_in_a_dataset_gate_either(self):
        df = frame([True, True, True])
        df["similarity/value"] = [0.05, 0.05, 0.05]
        with self.assertRaises(GateFailure):
            assert_gate(NS(result_df=df), dataset(), scorers=("exact_match", "similarity"))
        STATE.reset()
        assert_gate(
            NS(result_df=df),
            dataset(),
            scorers=("exact_match", "similarity"),
            pass_if={"similarity": lambda v: v < 0.1},
        )

    def test_duplicate_scorer_names_are_refused(self):
        with self.assertRaises(ValueError):
            assert_gate(NS(result_df=frame([True, True, True])), dataset(), scorers=("exact_match", "exact_match"))


if __name__ == "__main__":
    unittest.main()
