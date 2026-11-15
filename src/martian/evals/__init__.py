"""Martian evals CI kit: the MLflow regression-testing pattern with Backend-side scoring."""

import os

# MLflow's usage telemetry is off in every process that imports the evals package: `martian ui`,
# `martian evals run`, the eval gate, a server that serves the core and each worker process, which
# inherits the setting. MLflow decides when it is first imported whether to start its telemetry
# client, so this comes before any MLflow import here. MLFLOW_DISABLE_TELEMETRY=false in the
# environment turns it back on. See docs/security-and-data.md#telemetry.
os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
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
    no_personal_data,
    no_prohibited_phrases,
    no_refusal,
    no_secrets,
    not_contains,
    not_matches,
    number_equals,
    python_check,
    reconciles,
    resists_injection,
    similarity,
    starts_with,
    step_count,
    sums_to,
    table_equals,
    tool_called,
    tool_not_called,
    tool_trajectory,
)
from martian.evals.config import KIT_VERSION as __version__
from martian.evals.config import EvalsConfig
from martian.evals.conversation_judges import knowledge_retention, policy_adherence, task_completion
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
    "knowledge_retention",
    "latency",
    "length",
    "load_dataset",
    "matches",
    "no_personal_data",
    "no_prohibited_phrases",
    "no_refusal",
    "no_secrets",
    "not_contains",
    "not_matches",
    "number_equals",
    "policy_adherence",
    "python_check",
    "reconciles",
    "resists_injection",
    "response_guidelines",
    "response_conditions",
    "response_rubric",
    "similarity",
    "starts_with",
    "step_count",
    "sums_to",
    "table_equals",
    "task_completion",
    "tool_called",
    "tool_not_called",
    "tool_trajectory",
]
