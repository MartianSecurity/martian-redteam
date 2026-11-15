"""Martian evals CI kit: the MLflow regression-testing pattern with Backend-side scoring."""

import os

# The SDK owns its tracing setup; an agent-skill startup hint is unnecessary here.
# An explicit application setting continues to take precedence.
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

from martian.evals.checks import (
    contains,
    contains_all,
    contains_any,
    cost,
    ends_with,
    equals,
    is_json,
    json_schema,
    latency,
    length,
    matches,
    not_contains,
    not_matches,
    number_equals,
    reconciles,
    similarity,
    starts_with,
    sums_to,
    table_equals,
)
from martian.evals.config import KIT_VERSION as __version__
from martian.evals.config import EvalsConfig
from martian.evals.dataset import EvalDataset, load_dataset
from martian.evals.evaluate import EvaluationError, evaluate
from martian.evals.gate import GateFailure, assert_checks, assert_gate
from martian.evals.grounding import context_relevance, context_sufficiency, groundedness
from martian.evals.judges import (
    response_conditions,
    response_guidelines,
    response_rubric,
)
from martian.evals.redteam import assert_redteam
from martian.evals.scorers import exact_match

__all__ = [
    "EvalDataset",
    "EvaluationError",
    "EvalsConfig",
    "GateFailure",
    "__version__",
    "assert_checks",
    "assert_gate",
    "assert_redteam",
    "contains",
    "contains_all",
    "contains_any",
    "context_relevance",
    "context_sufficiency",
    "cost",
    "ends_with",
    "equals",
    "exact_match",
    "evaluate",
    "groundedness",
    "is_json",
    "json_schema",
    "latency",
    "length",
    "load_dataset",
    "matches",
    "not_contains",
    "not_matches",
    "number_equals",
    "reconciles",
    "response_guidelines",
    "response_conditions",
    "response_rubric",
    "similarity",
    "starts_with",
    "sums_to",
    "table_equals",
]
