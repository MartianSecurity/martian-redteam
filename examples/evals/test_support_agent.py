"""The 20 lines a customer copies. Run with:

pytest -p martian.evals.plugin --martian-evals-gate tests/evals
"""

import mlflow

from martian.evals import assert_gate, exact_match, load_dataset


@mlflow.trace(name="agent", span_type="AGENT")  # required: an untraced agent fails the gate
def agent(message: str) -> str:
    from my_project.agent import answer  # the code under review

    return answer(message)


@mlflow.test
def test_support_regressions():
    data = load_dataset("support-regressions")  # pinned by Backend; full prompts, ACTIVE rows only
    result = mlflow.genai.evaluate(data=data, predict_fn=agent, scorers=[exact_match])
    assert_gate(result, data)  # use this, not `assert result.passed` (see README)
