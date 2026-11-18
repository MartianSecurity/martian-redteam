"""MLflow-style regression tests on inline data, as in MLflow's regression-testing page.

pytest -p martian.evals.plugin --martian-evals-gate tests/evals
"""

import mlflow
from mlflow.genai.scorers import Guidelines  # an LLM judge: needs the provider key on the runner

from martian.evals import assert_checks

QUESTIONS = [{"inputs": {"question": "What are your support hours?"}}]


@mlflow.trace(name="faq_agent", span_type="AGENT")  # required: an untraced agent fails the gate
def faq_agent(question: str) -> str:
    from my_project.agent import answer  # the code under review

    return answer(question)


@mlflow.test
def test_answers_concisely_in_english():
    result = mlflow.genai.evaluate(
        data=QUESTIONS,
        predict_fn=faq_agent,
        scorers=[
            Guidelines(name="is_english", guidelines="The answer must be written in English."),
            Guidelines(name="is_concise", guidelines="The answer must be a single sentence."),
        ],
    )
    assert_checks(result)  # use this, not `assert result.passed, result.reason`
