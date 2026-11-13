"""MLflow's evaluation contract with reliable Martian scorer identity."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

import mlflow
import mlflow.genai
from mlflow.genai.evaluation.entities import EvaluationResult
from mlflow.genai.scorers import Scorer

from martian.evals.state import STATE

if TYPE_CHECKING:
    from mlflow.genai.evaluation.utils import EvaluationDatasetTypes


class EvaluationError(RuntimeError):
    """The evaluation could not produce a result for every row, so nothing can be gated on it."""


def _rows(data: Any) -> Iterable[Any]:
    """Row dicts of an in-memory dataset. Other dataset types are left to MLflow."""
    if isinstance(data, list):
        return data
    to_dict = getattr(data, "to_dict", None)  # pandas DataFrame, without importing pandas here
    if callable(to_dict):
        try:
            return list(to_dict(orient="records"))
        except TypeError:
            return ()
    return ()


def check_scorer_names(data: Any, scorers: Sequence[Any]) -> None:
    """Reject a scorer that shares its name with an expectation key.

    MLflow writes both to a "<name>/value" result column, and the expectation's own value
    replaces the scorer's verdict without any warning.
    """
    names = {str(name) for scorer in scorers if (name := getattr(scorer, "name", None))}
    clashes: set[str] = set()
    for row in _rows(data):
        expectations = row.get("expectations") if isinstance(row, dict) else None
        if isinstance(expectations, dict):
            clashes |= names & {str(key) for key in expectations}
    if clashes:
        listed = ", ".join(sorted(clashes))
        raise ValueError(
            f"scorer name(s) {listed} are also expectation keys; MLflow would overwrite the scorer's "
            "verdict with the expectation's value. Give the scorer a different name."
        )


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

    Mistakes that would otherwise produce a silent wrong result raise instead: a scorer named
    like an expectation key (ValueError), and an evaluation that recorded no per-row results
    (EvaluationError). A prediction function that is not already traced is traced here,
    because MLflow records per-row results only for traced calls.
    """
    check_scorer_names(data, scorers)
    if predict_fn is not None and not getattr(predict_fn, "__mlflow_traced__", False):
        predict_fn = mlflow.trace(predict_fn)
    result = mlflow.genai.evaluate(data=data, scorers=list(scorers), predict_fn=predict_fn, model_id=model_id)
    frame = getattr(result, "result_df", None)
    expected = len(data) if isinstance(data, list) else None
    if frame is None or (expected is not None and len(frame) != expected):
        found = "no" if frame is None else str(len(frame))
        raise EvaluationError(
            f"the evaluation recorded {found} per-row results"
            + (f" for {expected} rows" if expected is not None else "")
            + ". Check that the local MLflow store is writable and that the prediction function ran."
        )
    result.__dict__["_martian_scorers"] = [s.name for s in scorers]
    result.__dict__["_martian_checked"] = False
    if STATE.enforce and not any(r is result for r in STATE.evaluated):
        STATE.evaluated.append(result)
    return result
