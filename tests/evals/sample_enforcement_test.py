"""Generated into a temp project; SCENARIO selects the enforcement case under test."""

import os

import mlflow
from mlflow.genai.scorers import scorer

from martian.evals import assert_checks, assert_gate, exact_match, load_dataset

SCENARIO = os.environ["SCENARIO"]
DATA = [{"inputs": {"question": "hours?"}}]


@mlflow.trace(name="agent", span_type="AGENT")
def agent(question):
    return "Hello there"


@scorer
def good(outputs) -> bool:
    return True


@scorer
def bad(outputs) -> bool:
    return False


@scorer
def quality(outputs) -> bool:
    return SCENARIO != "dupnames"


def _gated():
    result = mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[good])
    assert_checks(result)


@mlflow.test
def test_gated():
    _gated()


if SCENARIO == "ungated":

    @mlflow.test
    def test_never_gated():
        try:
            mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[bad])
            raise RuntimeError("boom")
        except RuntimeError:
            pass  # swallowed: nothing reached a gate


if SCENARIO == "unchecked":

    @mlflow.test
    def test_unchecked():
        first = mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[good])
        assert_checks(first)
        second = mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[bad])
        assert second is not None  # a second evaluation that never reaches a gate


if SCENARIO == "dupnames":

    @scorer(name="quality")
    def quality_a(outputs) -> bool:
        return False

    @scorer(name="quality")
    def quality_b(outputs) -> bool:
        return True

    @mlflow.test
    def test_duplicates():
        result = mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[quality_a, quality_b])
        assert_checks(result)


if SCENARIO == "narrow":

    @mlflow.test
    def test_narrowed():
        result = mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[good, bad])
        assert_checks(result, scorers=["good"])


if SCENARIO == "toomany":

    @mlflow.test
    def test_too_many_rows():
        result = mlflow.genai.evaluate(data=DATA * 101, predict_fn=agent, scorers=[good])
        assert_checks(result)


if SCENARIO in ("localfail", "uploadfail"):

    @mlflow.test
    def test_dataset_gate():
        data = load_dataset("support")

        @mlflow.trace(name="support", span_type="AGENT")
        def support(message):
            return {"Capital of France?": "Paris", "2 + 2?": "4", "Greet me": "Hello"}[message]

        scorers = [exact_match] + ([bad] if SCENARIO == "localfail" else [])
        result = mlflow.genai.evaluate(data=data, predict_fn=support, scorers=scorers)
        names = ("exact_match", "bad") if SCENARIO == "localfail" else ("exact_match",)
        assert_gate(result, data, scorers=names)
