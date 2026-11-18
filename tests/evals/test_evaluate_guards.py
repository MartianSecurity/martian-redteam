"""evaluate() raises on mistakes that would otherwise give a silent wrong result."""

import threading

import mlflow
import pandas as pd
import pytest
from mlflow.genai.scorers import scorer

from martian.evals import EvaluationError, evaluate
from martian.evals.state import STATE


@pytest.fixture
def store(monkeypatch, tmp_path):
    old_uri = mlflow.get_tracking_uri()
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "true")
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'tracking.db'}")
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


def _named(name):
    @scorer(name=name)
    def check(outputs) -> bool:
        return bool(outputs)

    return check


def test_scorer_named_like_an_expectation_key_is_rejected_before_anything_runs():
    calls = []
    data = [{"inputs": {"message": "hi"}, "expectations": {"rubric": "Greet the user."}}]
    with pytest.raises(ValueError, match="rubric"):
        evaluate(data=data, scorers=[_named("rubric")], predict_fn=lambda message: calls.append(message) or "hello")
    assert calls == []


def test_untraced_prediction_function_still_yields_a_row_per_case(store):
    def target(message):  # deliberately not decorated with mlflow.trace
        return "hello " + message

    data = [{"inputs": {"message": "one"}}, {"inputs": {"message": "two"}}]
    result = evaluate(data=data, scorers=[_named("answered")], predict_fn=target)
    assert len(result.result_df) == 2
    assert sorted(result.result_df["response"]) == ["hello one", "hello two"]


def test_missing_per_row_results_raise(monkeypatch):
    class Empty:
        result_df = None

    monkeypatch.setattr("mlflow.genai.evaluate", lambda **kwargs: Empty())
    with pytest.raises(EvaluationError, match="no per-row results"):
        evaluate(data=[{"inputs": {"message": "hi"}, "outputs": "hello"}], scorers=[_named("answered")])


def test_a_prediction_function_that_hangs_on_one_case_is_that_cases_error(store):
    never = threading.Event()  # never set: the hung call would wait for ever

    def target(message):
        if message == "hang":
            never.wait()
        return "hello " + message

    data = [{"inputs": {"message": "one"}}, {"inputs": {"message": "hang"}}, {"inputs": {"message": "two"}}]
    result = evaluate(data=data, scorers=[_named("answered")], predict_fn=target, target_timeout_seconds=0.3)
    frame = result.result_df
    assert len(frame) == 3
    by_message = {row["request"]["message"]: row for _, row in frame.iterrows()}
    assert by_message["one"]["response"] == "hello one"
    assert by_message["two"]["response"] == "hello two"
    assert pd.isna(by_message["hang"]["response"])
    assert "ERROR" in str(by_message["hang"]["state"]).upper()
