"""Schema tests for the binary evaluator score."""

from typing import Any

import pytest
from pydantic import ValidationError

from martian_redteam.types import EvaluatorOutput, TestCase


@pytest.mark.parametrize("score", [0, 1])
def test_integer_scores_are_accepted(score: int) -> None:
    assert EvaluatorOutput.model_validate({"score": score, "reason": "judged"}).score == score
    case = TestCase.model_validate(
        {"vulnerability": "bias", "vulnerability_type": "race", "attack_method": "roleplay", "score": score}
    )
    assert case.score == score
    assert type(case.score) is int


@pytest.mark.parametrize("score", [True, False, 1.0, 0.0, "1", "0", 0.5, 2])
def test_non_integer_scores_are_rejected(score: Any) -> None:
    # True == 1 and 1.0 == 1 in Python; a lax literal would read them as "resisted".
    with pytest.raises(ValidationError):
        EvaluatorOutput.model_validate({"score": score, "reason": "judged"})
    with pytest.raises(ValidationError):
        TestCase.model_validate(
            {"vulnerability": "bias", "vulnerability_type": "race", "attack_method": "roleplay", "score": score}
        )


@pytest.mark.parametrize("raw", ["true", "1.0", '"1"'])
def test_non_integer_json_scores_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        EvaluatorOutput.model_validate_json(f'{{"score": {raw}, "reason": "judged"}}')


def test_test_case_score_may_be_null_for_errored_probes() -> None:
    case = TestCase(vulnerability="bias", vulnerability_type="race", attack_method="roleplay", error="boom")
    assert case.score is None
