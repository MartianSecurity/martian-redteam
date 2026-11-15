"""Every MLflow internal the SDK depends on, and nothing else.

MLflow's public API (``mlflow.set_tracking_uri``, ``mlflow.genai.evaluate``, ``mlflow.entities``,
``mlflow.genai.scorers``, ``mlflow.types.llm``, ``mlflow.environment_variables``) may be imported
anywhere. Anything else MLflow does not promise to keep is reached only through this module, so
an MLflow upgrade breaks one file and ``tests/evals/test_mlflow_private.py`` names what went
missing. ``PRIVATE_DEPENDENCIES`` lists each internal name.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mlflow.entities import Trace as MlflowTrace
from mlflow.entities import TraceData
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
from mlflow.store.db.utils import _all_tables_exist, _get_alembic_config, _get_latest_schema_revision
from mlflow.store.tracking.dbmodels.models import SqlRun, SqlTraceInfo
from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore
from mlflow.store.tracking.sqlalchemy_workspace_store import WorkspaceAwareSqlAlchemyStore
from mlflow.store.workspace.sqlalchemy_store import SqlAlchemyStore as WorkspaceSqlAlchemyStore
from mlflow.tracing.constant import AssessmentMetadataKey, SpansLocation, TraceTagKey
from mlflow.tracing.utils.artifact_utils import TRACE_DATA_FILE_NAME, get_artifact_uri_for_trace
from mlflow.tracking import fluent as _fluent
from mlflow.utils.search_utils import SearchTraceUtils
from mlflow.utils.workspace_context import ServerWorkspaceContext, get_request_workspace
from mlflow.utils.workspace_utils import WORKSPACE_HEADER_NAME

if TYPE_CHECKING:
    from mlflow.genai.evaluation.utils import EvaluationDatasetTypes as EvaluationDatasetTypes

PRIVATE_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("mlflow.entities", "Trace"),  # public, imported here to build a trace from its artifact file
    ("mlflow.entities", "TraceData.from_dict"),
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
    ("mlflow.store.db.utils", "_all_tables_exist"),
    ("mlflow.store.db.utils", "_get_alembic_config"),
    ("mlflow.store.db.utils", "_get_latest_schema_revision"),
    ("mlflow.tracing.constant", "AssessmentMetadataKey"),
    ("mlflow.tracing.constant", "TraceTagKey.SPANS_LOCATION"),
    ("mlflow.tracing.constant", "SpansLocation.TRACKING_STORE"),
    ("mlflow.tracing.constant", "SpansLocation.ARCHIVE_REPO"),
    ("mlflow.tracing.utils.artifact_utils", "TRACE_DATA_FILE_NAME"),
    ("mlflow.tracing.utils.artifact_utils", "get_artifact_uri_for_trace"),
    ("mlflow.tracking.fluent", "_active_experiment_id"),
    ("mlflow.utils.workspace_context", "get_request_workspace"),
    ("mlflow.utils.workspace_context", "ServerWorkspaceContext"),
    ("mlflow.store.tracking.dbmodels.models", "SqlRun.artifact_uri"),
    ("mlflow.store.tracking.dbmodels.models", "SqlTraceInfo.request_preview"),
    ("mlflow.store.tracking.sqlalchemy_workspace_store", "WorkspaceAwareSqlAlchemyStore"),
    ("mlflow.store.tracking.sqlalchemy_workspace_store", "WorkspaceAwareSqlAlchemyStore._workspaces_enabled"),
    ("mlflow.store.tracking.sqlalchemy_workspace_store", "WorkspaceAwareSqlAlchemyStore._get_artifact_location"),
    ("mlflow.store.tracking.sqlalchemy_store", "SqlAlchemyStore._load_dataset_records"),
    ("mlflow.store.tracking.sqlalchemy_store", "SqlAlchemyStore._get_or_create_engine"),
    ("mlflow.store.tracking.sqlalchemy_store", "SqlAlchemyStore._initialize_store_state"),
    ("mlflow.store.workspace.sqlalchemy_store", "SqlAlchemyStore"),
    ("mlflow.utils.search_utils", "SearchTraceUtils.create_page_token"),
    ("mlflow.utils.workspace_utils", "WORKSPACE_HEADER_NAME"),
)

TYPE_ONLY = frozenset({("mlflow.genai.evaluation.utils", "EvaluationDatasetTypes")})
"""Dependencies MLflow defines only for type checkers, so they cannot be looked up at run time."""

MLFLOW_PYTEST_PLUGIN = "mlflow.pytest.plugin"
"""MLflow's pytest plugin module, which the eval gate loads by name."""

RUN_ARTIFACT_URI_LENGTH: int = SqlRun.artifact_uri.type.length
"""The length of MLflow's ``runs.artifact_uri`` column, which keeps a run's artifact location:
200 characters. Postgres enforces it; SQLite does not."""

TRACE_PREVIEW_LENGTH: int = SqlTraceInfo.request_preview.type.length
"""The length of MLflow's ``trace_info.request_preview`` column, and of ``response_preview`` beside
it, which keep a trace's previews: 1,000 characters, the length MLflow tracing shortens its own
previews to. Postgres enforces it; SQLite does not."""


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


class WorkspaceStore(WorkspaceAwareSqlAlchemyStore):
    """MLflow's SQL tracking store with workspaces on for this store only.

    MLflow turns workspaces on with the process-wide ``MLFLOW_ENABLE_WORKSPACES`` variable, which
    would also change every other MLflow use in the process, such as a local suite run. Every
    query of this store still requires a workspace in the request context.

    An experiment it creates keeps its artifacts in the artifact root itself, not in MLflow's
    ``workspaces/<workspace>/<experiment id>`` under it, so a run's artifact location is the root
    and ``/<run id>/artifacts``. For an organisation, whose workspace name is 44 characters,
    MLflow's layout would add 58 or more to a location that must fit in
    :data:`RUN_ARTIFACT_URI_LENGTH`. Run ids are unique in the database, so no two runs share a
    folder. An experiment an earlier release created keeps the location MLflow stored for it.
    """

    @staticmethod
    def _workspaces_enabled() -> bool:
        return True

    def _get_artifact_location(self, experiment_id: object, workspace: str | None = None) -> str:
        root: str = self.artifact_root_uri  # the root as MLflow resolved it
        return root


def hide_sql_parameters(store: WorkspaceAwareSqlAlchemyStore) -> None:
    """Keep the values of the store's SQL statements out of SQLAlchemy's errors and log lines.

    MLflow's SQL store creates its engine itself (``engine``, one per database URL, shared by every
    MLflow store in the process on that URL), and SQLAlchemy writes a failed statement's values
    into its error, which MLflow passes on in its own and logs for some database failures. The
    values of a trace are its request and its response.
    """
    store.engine.hide_parameters = True


def load_dataset_records(store: WorkspaceAwareSqlAlchemyStore, dataset_id: str, page_token: str | None) -> Any:
    """One page of a dataset's records and the next page token. MLflow has no public reader that
    takes a store."""
    return store._load_dataset_records(dataset_id, page_token=page_token)


def trace_page_token(offset: int) -> str | None:
    """MLflow's page token for a trace search that starts after its first ``offset`` traces, or
    ``None`` for the first page. MLflow's SQL store pages traces by offset; the token is its own
    encoding of the offset, which it does not promise to keep."""
    if offset == 0:
        return None
    token: bytes = SearchTraceUtils.create_page_token(offset)  # type: ignore[no-untyped-call]
    return token.decode("ascii")


# --- reading another MLflow's tracking store (martian.evals.mlflow_import) --------------------


def schema_head() -> str:
    """The schema revision of MLflow's tracking store that this MLflow writes and reads: the head
    of its Alembic history."""
    head: str = _get_latest_schema_revision()  # type: ignore[no-untyped-call]
    return head


def schema_history() -> list[str]:
    """Every schema revision of MLflow's tracking store this MLflow knows, newest first."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_get_alembic_config(""))  # type: ignore[no-untyped-call]
    return [revision.revision for revision in script.walk_revisions()]


def has_every_table(engine: Any) -> bool:
    """Whether the database holds every table this MLflow's tracking store defines. Opening one
    that lacks a table makes MLflow create the tables and migrate the database."""
    found: bool = _all_tables_exist(engine)  # type: ignore[no-untyped-call]
    return found


class ReadOnlyTrackingStore(SqlAlchemyStore):
    """MLflow's SQL tracking store over ``engine``, which only reads, for a database whose schema
    revision is :func:`schema_head`.

    MLflow's constructor can write: it creates and migrates the tables of a database that lacks
    one, and creates the default experiment when it is missing. Here the engine is the caller's,
    so every statement that would write fails, and the default experiment is never created.
    ``url`` is the database's URL, from which MLflow reads its dialect.
    """

    def __init__(self, engine: Any, url: str) -> None:
        self._read_only_engine = engine
        super().__init__(url, "unused")  # type: ignore[no-untyped-call]  # the artifact root takes no writes

    def _get_or_create_engine(self, db_uri: str) -> Any:  # type: ignore[override]
        return self._read_only_engine

    def _initialize_store_state(self) -> None:
        """MLflow checks the workspaces and creates the default experiment here; the importer
        checks the workspaces itself, and a store that is only read needs no default experiment."""


def trace_artifact_file(info: Any) -> str | None:
    """The URI of the file holding a trace's spans when MLflow keeps them among the trace's
    artifacts, as releases before the spans table did; ``None`` when the tracking store reads
    them itself, from its tables or from its trace archive. Raises ``MlflowException`` for a trace
    that names no artifact location."""
    stored = (SpansLocation.TRACKING_STORE.value, SpansLocation.ARCHIVE_REPO.value)
    if (info.tags or {}).get(TraceTagKey.SPANS_LOCATION) in stored:
        return None
    location: str = get_artifact_uri_for_trace(info)
    return location.rstrip("/") + "/" + TRACE_DATA_FILE_NAME


def trace_from_file(info: Any, content: str) -> MlflowTrace:
    """A trace with the spans of its artifact file's ``content``, MLflow's ``TraceData`` JSON."""
    return MlflowTrace(info=info, data=TraceData.from_dict(json.loads(content)))


def mlflow_test_attr() -> str:
    """The attribute ``@mlflow.test`` sets on a test function. Imported late: it loads pytest."""
    from mlflow.pytest.decorator import MLFLOW_TEST_ATTR

    name: str = MLFLOW_TEST_ATTR
    return name


__all__ = [
    "MLFLOW_GATEWAY_CALLER_HEADER",
    "MLFLOW_PYTEST_PLUGIN",
    "PRIVATE_DEPENDENCIES",
    "RUN_ARTIFACT_URI_LENGTH",
    "ReadOnlyTrackingStore",
    "TRACE_PREVIEW_LENGTH",
    "TYPE_ONLY",
    "WORKSPACE_HEADER_NAME",
    "AssessmentMetadataKey",
    "ChatCompletionError",
    "EvaluationResult",
    "GatewayAdapter",
    "GatewayCaller",
    "LiteLLMAdapter",
    "ServerWorkspaceContext",
    "SpansLocation",
    "TraceTagKey",
    "WorkspaceSqlAlchemyStore",
    "WorkspaceStore",
    "active_experiment_id",
    "build_chat_request",
    "error_text",
    "get_adapter",
    "get_request_workspace",
    "has_every_table",
    "hide_sql_parameters",
    "invoke_litellm",
    "is_context_window_error",
    "is_response_format_error",
    "load_dataset_records",
    "lookup_model_cost",
    "mlflow_test_attr",
    "parse_model_uri",
    "provider_instance",
    "restore_active_experiment_id",
    "schema_head",
    "schema_history",
    "trace_artifact_file",
    "trace_from_file",
    "trace_page_token",
]
