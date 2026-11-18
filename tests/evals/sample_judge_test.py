"""Generated into a temp project by test_real_judge.py: MLflow's built-in Guidelines judge."""

import os

import mlflow
from mlflow.genai.scorers import Guidelines

from martian.evals import assert_checks


@mlflow.trace(name="faq_agent", span_type="AGENT")
def faq(question):
    return os.environ["ANSWER"]


@mlflow.test
def test_answers_in_english():
    result = mlflow.genai.evaluate(
        data=[{"inputs": {"question": "What are your hours?"}}],
        predict_fn=faq,
        scorers=[
            Guidelines(
                name="is_english",
                guidelines="The answer must be written in English.",
                model="openai:/gpt-4o-mini",
            )
        ],
    )
    assert_checks(result)
