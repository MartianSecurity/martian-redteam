"""Martian evals CI kit: the MLflow regression-testing pattern with Backend-side scoring."""

import os

# The SDK owns its tracing setup; an agent-skill startup hint is unnecessary here.
# An explicit application setting continues to take precedence.
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

from martian.evals.config import KIT_VERSION as __version__
from martian.evals.config import EvalsConfig
from martian.evals.dataset import EvalDataset, load_dataset
from martian.evals.evaluate import evaluate
from martian.evals.gate import GateFailure, assert_checks, assert_gate
from martian.evals.judges import response_conditions, response_guidelines
from martian.evals.redteam import assert_redteam
from martian.evals.scorers import exact_match

__all__ = [
    "EvalDataset",
    "EvalsConfig",
    "GateFailure",
    "__version__",
    "assert_checks",
    "assert_gate",
    "assert_redteam",
    "exact_match",
    "evaluate",
    "load_dataset",
    "response_guidelines",
    "response_conditions",
]
