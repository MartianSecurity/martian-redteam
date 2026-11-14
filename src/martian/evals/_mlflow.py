"""Every MLflow internal the SDK depends on, and nothing else.

MLflow's public API (``mlflow.set_tracking_uri``, ``mlflow.genai.evaluate``, ``mlflow.entities``,
``mlflow.genai.scorers``, ``mlflow.types.llm``, ``mlflow.environment_variables``) may be imported
anywhere. Anything else MLflow does not promise to keep is reached only through this module, so
an MLflow upgrade breaks one file and ``tests/evals/test_mlflow_private.py`` names what went
missing. ``PRIVATE_DEPENDENCIES`` lists each internal name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlflow.gateway.constants import MLFLOW_GATEWAY_CALLER_HEADER, GatewayCaller
from mlflow.genai.evaluation.entities import EvaluationResult
from mlflow.genai.judges.adapters.gateway_adapter import GatewayAdapter
from mlflow.genai.judges.adapters.gateway_adapter import _build_request as build_chat_request
from mlflow.genai.judges.adapters.litellm_adapter import LiteLLMAdapter
from mlflow.genai.judges.adapters.litellm_adapter import _invoke_litellm as invoke_litellm
from mlflow.genai.judges.adapters.utils import (
    ChatCompletionError,
    get_adapter,
    is_context_window_error,
    is_response_format_error,
)
from mlflow.genai.judges.adapters.utils import _safe_parse_error as error_text
from mlflow.genai.utils.llm_utils import _lookup_model_cost as lookup_model_cost
from mlflow.metrics.genai.model_utils import _get_provider_instance as provider_instance
from mlflow.metrics.genai.model_utils import _parse_model_uri as parse_model_uri
from mlflow.tracing.constant import AssessmentMetadataKey
from mlflow.tracking import fluent as _fluent
from mlflow.utils.workspace_context import get_request_workspace
from mlflow.utils.workspace_utils import WORKSPACE_HEADER_NAME

if TYPE_CHECKING:
    from mlflow.genai.evaluation.utils import EvaluationDatasetTypes as EvaluationDatasetTypes

PRIVATE_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("mlflow.gateway.constants", "MLFLOW_GATEWAY_CALLER_HEADER"),
    ("mlflow.gateway.constants", "GatewayCaller"),
    ("mlflow.genai.evaluation.entities", "EvaluationResult"),
    ("mlflow.genai.evaluation.utils", "EvaluationDatasetTypes"),
    ("mlflow.genai.judges.adapters.gateway_adapter", "GatewayAdapter"),
    ("mlflow.genai.judges.adapters.gateway_adapter", "_build_request"),
    ("mlflow.genai.judges.adapters.litellm_adapter", "LiteLLMAdapter"),
    ("mlflow.genai.judges.adapters.litellm_adapter", "_invoke_litellm"),
    ("mlflow.genai.judges.adapters.utils", "ChatCompletionError"),
    ("mlflow.genai.judges.adapters.utils", "_safe_parse_error"),
    ("mlflow.genai.judges.adapters.utils", "get_adapter"),
    ("mlflow.genai.judges.adapters.utils", "is_context_window_error"),
    ("mlflow.genai.judges.adapters.utils", "is_response_format_error"),
    ("mlflow.genai.utils.llm_utils", "_lookup_model_cost"),
    ("mlflow.metrics.genai.model_utils", "_get_provider_instance"),
    ("mlflow.metrics.genai.model_utils", "_parse_model_uri"),
    ("mlflow.pytest.decorator", "MLFLOW_TEST_ATTR"),
    ("mlflow.pytest.plugin", ""),  # loaded by name as a pytest plugin
    ("mlflow.tracing.constant", "AssessmentMetadataKey"),
    ("mlflow.tracking.fluent", "_active_experiment_id"),
    ("mlflow.utils.workspace_context", "get_request_workspace"),
    ("mlflow.utils.workspace_utils", "WORKSPACE_HEADER_NAME"),
)

TYPE_ONLY = frozenset({("mlflow.genai.evaluation.utils", "EvaluationDatasetTypes")})
"""Dependencies MLflow defines only for type checkers, so they cannot be looked up at run time."""

MLFLOW_PYTEST_PLUGIN = "mlflow.pytest.plugin"
"""MLflow's pytest plugin module, which the eval gate loads by name."""


def active_experiment_id() -> str | None:
    """The experiment ``mlflow.set_experiment`` last selected in this process."""
    value: str | None = _fluent._active_experiment_id
    return value


def restore_active_experiment_id(value: str | None) -> None:
    """Put back an experiment selection saved with :func:`active_experiment_id`.

    MLflow has no public way to clear the selection, and ``set_experiment`` would create an
    experiment, so this writes MLflow's own module variable.
    """
    vars(_fluent)["_active_experiment_id"] = value


def mlflow_test_attr() -> str:
    """The attribute ``@mlflow.test`` sets on a test function. Imported late: it loads pytest."""
    from mlflow.pytest.decorator import MLFLOW_TEST_ATTR

    name: str = MLFLOW_TEST_ATTR
    return name


__all__ = [
    "MLFLOW_GATEWAY_CALLER_HEADER",
    "MLFLOW_PYTEST_PLUGIN",
    "PRIVATE_DEPENDENCIES",
    "TYPE_ONLY",
    "WORKSPACE_HEADER_NAME",
    "AssessmentMetadataKey",
    "ChatCompletionError",
    "EvaluationResult",
    "GatewayAdapter",
    "GatewayCaller",
    "LiteLLMAdapter",
    "active_experiment_id",
    "build_chat_request",
    "error_text",
    "get_adapter",
    "get_request_workspace",
    "invoke_litellm",
    "is_context_window_error",
    "is_response_format_error",
    "lookup_model_cost",
    "mlflow_test_attr",
    "parse_model_uri",
    "provider_instance",
    "restore_active_experiment_id",
]
