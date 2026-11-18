"""Generated into a temp project by test_plugin.py; behaviour is chosen by SCENARIO."""

import os
from contextlib import suppress

import mlflow
import pytest
from mlflow.genai.scorers import scorer

from martian.evals import assert_gate, exact_match, load_dataset

SCENARIO = os.environ["SCENARIO"]
ANSWERS = {"Capital of France?": "Paris", "2 + 2?": "4", "Greet me": "Hello"}
if SCENARIO in ("wrong", "threshold", "lenient", "swallow", "emptyscorers"):
    ANSWERS["2 + 2?"] = "5"
STRUCTURED = SCENARIO == "listout"
BIG = SCENARIO == "bigout"


@scorer
def always_true(outputs, expectations) -> bool:
    """A client scorer that disagrees with Backend: the server must not take its word."""
    return True


def _agent(message):
    if SCENARIO == "raises" and message == "2 + 2?":
        raise RuntimeError("tool backend unavailable")
    if STRUCTURED and message == "Capital of France?":
        return ["Paris"]  # Backend sees its JSON text, not Python's repr
    if BIG and message == "Greet me":
        return "x" * 20_001
    return ANSWERS[message]


agent = _agent if SCENARIO == "untraced" else mlflow.trace(name="agent", span_type="AGENT")(_agent)


def test_plain_unit():
    assert 1 + 1 == 2


if SCENARIO != "zero":

    @mlflow.test
    def test_gate():
        if SCENARIO == "skip":
            pytest.skip("secret missing")
        data = load_dataset("support")
        if SCENARIO == "swallow":
            result = mlflow.genai.evaluate(data=data, predict_fn=agent, scorers=[exact_match])
            # Test code that hides the failure must not hide it from the session.
            with suppress(AssertionError):
                assert_gate(result, data)
            return
        if SCENARIO == "emptyscorers":
            result = mlflow.genai.evaluate(data=data, predict_fn=agent, scorers=[exact_match])
            assert_gate(result, data, scorers=())
            return
        if SCENARIO == "lenient":
            result = mlflow.genai.evaluate(data=data, predict_fn=agent, scorers=[always_true])
            assert_gate(result, data, scorers=("always_true",))
            return
        result = mlflow.genai.evaluate(data=data, predict_fn=agent, scorers=[exact_match])
        assert_gate(result, data)
        if SCENARIO == "double":
            result = mlflow.genai.evaluate(data=data, predict_fn=agent, scorers=[exact_match])
            assert_gate(result, data)
