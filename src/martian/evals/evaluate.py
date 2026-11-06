"""MLflow's evaluation contract with reliable Martian scorer identity."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import mlflow.genai
from mlflow.genai.evaluation.entities import EvaluationResult
from mlflow.genai.scorers import Scorer

from martian.evals.state import STATE

if TYPE_CHECKING:
    from mlflow.genai.evaluation.utils import EvaluationDatasetTypes


def evaluate(
    *,
    data: EvaluationDatasetTypes,
    scorers: Sequence[Scorer],
    predict_fn: Callable[..., Any] | None = None,
    model_id: str | None = None,
) -> EvaluationResult:
    """Run the local prediction callable and scorers once; return MLflow's typed result.

    Use inside a regression test and pass the result to assert_gate/assert_checks.
    The plugin configures private local storage and prevents validation from
    invoking the first prediction a second time.
    """
    result = mlflow.genai.evaluate(data=data, scorers=list(scorers), predict_fn=predict_fn, model_id=model_id)
    result.__dict__["_martian_scorers"] = [s.name for s in scorers]
    result.__dict__["_martian_checked"] = False
    if STATE.enforce and not any(r is result for r in STATE.evaluated):
        STATE.evaluated.append(result)
    return result
