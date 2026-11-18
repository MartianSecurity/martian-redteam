"""Generated into a temp project by test_checks_and_sessions.py; SCENARIO selects the behaviour."""

import os

import mlflow
from mlflow.entities import Feedback
from mlflow.genai.scorers import scorer

from martian.evals import assert_checks, assert_gate, exact_match, load_dataset

SCENARIO = os.environ["SCENARIO"]


@mlflow.trace(name="agent", span_type="AGENT")
def agent(question):
    return "Bonjour, nous ouvrons a neuf heures." if SCENARIO == "french" else "Hello there, we open at nine."


@scorer
def is_english(outputs) -> Feedback:
    english = "Hello" in str(outputs)
    return Feedback(value="yes" if english else "no", rationale="English greeting" if english else "Not English")


@scorer
def concise(outputs) -> bool:
    return len(str(outputs)) < 60


@scorer
def sentiment(outputs) -> float:
    return 0.9


@scorer
def sometimes_missing(outputs):
    return None


DATA = [{"inputs": {"question": "What are your hours?"}}, {"inputs": {"question": "Where are you?"}}]


@mlflow.test
def test_answers_in_english():
    result = mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[is_english, concise])
    assert_checks(result)


@mlflow.test
def test_tone():
    if SCENARIO == "numeric":
        result = mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[sentiment])
        assert_checks(result)
    elif SCENARIO == "numeric_pass_if":
        result = mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[sentiment])
        assert_checks(result, pass_if={"sentiment": lambda v: v >= 0.8})
    elif SCENARIO == "missing":
        result = mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[concise, sometimes_missing])
        assert_checks(result)
    else:
        result = mlflow.genai.evaluate(data=DATA, predict_fn=agent, scorers=[concise])
        assert_checks(result, scorers=[concise])


if SCENARIO == "mixed":

    @mlflow.test
    def test_support_regressions():
        data = load_dataset("support")

        @mlflow.trace(name="agent", span_type="AGENT")
        def support(message):
            return {"Capital of France?": "Paris", "2 + 2?": "4", "Greet me": "Hello"}[message]

        result = mlflow.genai.evaluate(data=data, predict_fn=support, scorers=[exact_match])
        assert_gate(result, data)
