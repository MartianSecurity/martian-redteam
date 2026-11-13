"""evaluate() raises on mistakes that would otherwise give a silent wrong result."""

import mlflow
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
