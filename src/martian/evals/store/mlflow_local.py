"""The store interface on mlflow-skinny's SQL store, in this process: no MLflow server.

For the local edition (SQLite) and for on-premises installs (Postgres). Each organisation gets
its own MLflow workspace, and the single local user one of its own; every call sets the
workspace for its own thread through MLflow's per-request context, never the environment. On
Postgres, MLflow's tables live in their own schema so they can share a database with others.

How the shared model sits in MLflow:

- A project is an experiment named by the project id, tagged ``martian.project.*``.
- A dataset version is an evaluation dataset tagged ``martian.dataset.*``; each case is a record
  that keeps its id, position, tags and metadata in ``martian.*`` record tags.
- A run is an MLflow run tagged ``martian.run_id``; the run itself is ``martian/run.json`` among
  its artifacts, and its counts are also logged as metrics.
- A trace keeps MLflow's trace id when the id has MLflow's form; otherwise MLflow's id is
  derived from it and the original is kept in the ``martian.trace_id`` tag. Spans are MLflow
  spans, with the same rule for span ids. A trace's session and user are the trace metadata keys
  MLflow tracing records them in, ``mlflow.trace.session`` and ``mlflow.trace.user``, so traces an
  application records with MLflow tracing group into sessions without Martian's help.
- A trace's request and response are kept as MLflow tracing keeps them: whole as the inputs and
  outputs of the trace's root span, which the binding writes and does not read back as one of the
  trace's spans, and as MLflow's previews in ``trace_info``, shortened to
  :data:`TRACE_PREVIEW_LENGTH` characters. The trace's own spans are that root span's children in
  MLflow. A search reads only the previews, so its text filter matches only them, and the trace's
  metadata says which were shortened (``REQUEST_PREVIEW_TRUNCATED``). A trace an earlier release
  wrote keeps its request and response whole in ``martian.request`` and ``martian.response``
  metadata instead.
- A trace belongs to the run its ``martian.run_id`` tag names. A trace without the tag that MLflow
  tracing recorded while a run's MLflow run was active names that MLflow run in its
  ``mlflow.sourceRun`` metadata, and belongs to that run; one recorded inside an MLflow run that is
  not a run's belongs to no run.
- ``record_trace`` maps a trace's GenAI attributes onto its fields (``martian.evals.trace_conventions``)
  and keeps the mapping version in its ``martian.trace_mapping`` metadata; a trace read without
  one, such as one MLflow tracing recorded, is mapped as it is read. The mapping reads the trace's
  own spans only: ``record_trace`` maps before it writes the root span, so the root span holds the
  request and response the mapping gave, and ``get_trace`` sets the root span aside, and takes the
  request and response from it, before it maps.
- A score is a feedback assessment on its trace.
- A registered scorer is an MLflow scorer whose serialized form carries the scorer.
- A prompt is a prompt of MLflow's prompt registry, in the workspace, with its creator in the
  ``martian.prompt.created_by`` tag; a prompt version is an MLflow prompt version, its text the
  template, its commit message MLflow's, its digest and author in the ``martian.prompt.digest`` and
  ``martian.prompt.author`` version tags. MLflow keeps the registry in its model registry tables,
  beside the tracking tables in the same database.
- A review question is a label schema; a review queue is an MLflow review queue named by the
  queue id, with its definition in an experiment tag.
- Run artifacts are files in the run's artifact folder,
  ``<artifact root>/<MLflow run id>/artifacts``. The artifact root must be local, and on Postgres
  at most :data:`LONGEST_ARTIFACT_ROOT` characters long, so that folder fits MLflow's
  ``runs.artifact_uri``.

A trace search is MLflow's own search, paged by MLflow, with every filter of the query MLflow has:
run, session, user, model (the ``mlflow.trace.model`` metadata the binding writes), tags, ids,
states and time. This process checks each trace MLflow returns
against the whole query, which applies the text filter and a value MLflow's filter syntax cannot
quote; a search with such a filter reads at most :data:`TRACE_SCAN_LIMIT` traces a call and
returns what matched among them, with a cursor that reads on. A search for a run's traces is two
MLflow searches, one on the tag and one on ``mlflow.sourceRun`` for untagged traces, merged in
order. A session search reads its first traces the same way and groups them in this process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from urllib.parse import unquote, urlsplit

from mlflow.entities import (
    AssessmentError,
    AssessmentSource,
    Feedback,
    Metric,
    RunStatus,
    RunTag,
    TraceInfo,
    TraceLocation,
    TraceState,
    ViewType,
    Workspace,
)
from mlflow.entities import Span as MlflowSpan
from mlflow.entities import Trace as MlflowTrace
from mlflow.exceptions import MlflowException
from mlflow.genai.label_schemas import InputCategorical, InputNumeric, InputPassFail, InputText, LabelSchemaType

from martian.evals._mlflow import (
    IS_PROMPT_TAG_KEY,
    RUN_ARTIFACT_URI_LENGTH,
    TRACE_PREVIEW_LENGTH,
    PromptRegistryStore,
    ServerWorkspaceContext,
    SpansLocation,
    TraceTagKey,
    WorkspaceSqlAlchemyStore,
    WorkspaceStore,
    hide_sql_parameters,
    load_dataset_records,
    trace_page_token,
)
from martian.evals._sql import (
    check_schema,
    create_schema,
    database_cause,
    database_refusal,
    is_postgres,
    operation_name,
    postgres_url,
)
from martian.evals.model import (
    SESSION_TRACE_LIMIT,
    Case,
    Counts,
    DatasetVersion,
    Page,
    Project,
    Prompt,
    PromptVersion,
    QueueItem,
    ReviewQuestion,
    ReviewQueue,
    Run,
    RunState,
    Score,
    ScorerVersion,
    ScoreSource,
    SessionSearch,
    Span,
    StoreRef,
    Trace,
    TraceQuery,
    digest,
    prompt_digest,
    sessions_from_traces,
)
from martian.evals.model import TraceState as ModelTraceState
from martian.evals.store import (
    REQUEST_PREVIEW_TRUNCATED,
    RESPONSE_PREVIEW_TRUNCATED,
    StoreError,
    StoreStatus,
    check_imported_prompt,
    check_prompt,
    check_session_search,
)
from martian.evals.trace_conventions import map_trace

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

T = TypeVar("T")

SESSION_KEY = "mlflow.trace.session"
"""The trace metadata key MLflow tracing records a session id in (``TraceMetadataKey.TRACE_SESSION``)."""
USER_KEY = "mlflow.trace.user"
"""The trace metadata key MLflow tracing records the application's user in (``TraceMetadataKey.TRACE_USER``)."""
SOURCE_RUN_KEY = "mlflow.sourceRun"
"""The trace metadata key MLflow tracing records the active MLflow run in (``TraceMetadataKey.SOURCE_RUN``)."""
MODEL_KEY = "mlflow.trace.model"
"""The trace metadata key the binding keeps a trace's model in, which a search filters on."""
LONGEST_ARTIFACT_ROOT = RUN_ARTIFACT_URI_LENGTH - len(f"/{'0' * 32}/artifacts")
"""The longest artifact root the store takes on Postgres, 157 characters: a run's artifact folder,
the root and ``/<MLflow run id>/artifacts``, must fit in MLflow's ``runs.artifact_uri``. SQLite does
not enforce the column's length, so it takes any root."""
TRACE_SPAN_KEY = "martian.trace_span"
"""The attribute that marks a trace's root span, the one the binding writes to hold the trace's
request and response whole."""
TRACE_SCAN_LIMIT = 1000
"""The most traces one ``search_traces`` or ``search_sessions`` call reads from MLflow. A search
MLflow filters whole reads only the traces it returns. One with a filter only this process can
apply, free text above all, stops after this many and returns what matched among them, with a
cursor that reads on: a page may then hold fewer traces than its limit, or none."""
_SCAN_CHUNK = 500
"""Traces read from MLflow at a time by a search this process filters."""
_CURSOR_OFFSET = re.compile(r"[0-9]{1,18}")
"""One MLflow search's offset in a cursor: at most 18 of the digits 0-9, so it fits SQL's 64-bit
``OFFSET`` and ``int()`` reads it as written."""
_CURSOR_REFUSED = "cursor is not a next_cursor this store returned"

PROMPT_CREATED_BY = "martian.prompt.created_by"
"""The prompt tag that keeps who created a prompt."""
PROMPT_DIGEST = "martian.prompt.digest"
"""The prompt version tag that keeps the digest of its text, which a run's prompt is matched by."""
PROMPT_AUTHOR = "martian.prompt.author"
"""The prompt version tag that keeps who added the version."""
_DIGEST = re.compile(r"[0-9a-f]{64}")
_EPOCH = datetime.min.replace(tzinfo=UTC)

_MLFLOW_TRACE_ID = re.compile(r"^tr-[0-9a-f]{32}$")
_MLFLOW_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_MLFLOW_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_SAFE_FILTER_VALUE = re.compile(r"^[^'`\\]*$")
_QUESTION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
_LENGTH_REFUSAL = re.compile(r"'[A-Za-z0-9_.\[\]]+' exceeds the maximum length of \d+ characters")
"""MLflow's refusal of a value over a length limit, which names the field, not the value."""
_ERROR_CODE = re.compile(r"[A-Z_]+")
_RUN_STATUS = {
    "queued": "SCHEDULED",
    "running": "RUNNING",
    "completed": "FINISHED",
    "partial": "FINISHED",
    "failed": "FAILED",
    "cancelled": "KILLED",
    "stopped": "KILLED",
}
_TRACE_STATE = {"ok": "OK", "error": "ERROR", "in_progress": "IN_PROGRESS", "unknown": "STATE_UNSPECIFIED"}
_TRACE_STATE_BACK: dict[str, ModelTraceState] = {
    "OK": "ok",
    "ERROR": "error",
    "IN_PROGRESS": "in_progress",
    "STATE_UNSPECIFIED": "unknown",
}
_SCORE_SOURCE = {"code": "CODE", "llm_judge": "LLM_JUDGE", "human": "HUMAN"}
_SCORE_SOURCE_BACK: dict[str, ScoreSource] = {"CODE": "code", "LLM_JUDGE": "llm_judge", "HUMAN": "human"}
_SPAN_STATUS: dict[str, ModelTraceState] = {
    "ok": "ok",
    "error": "error",
    "in_progress": "in_progress",
    "unknown": "unknown",
    "OK": "ok",
    "ERROR": "error",
}


def _hex(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _ms(when: datetime | None) -> int | None:
    return int(when.timestamp() * 1000) if when is not None else None


def _from_ms(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value / 1000, UTC) if value is not None else None


def workspace_name(principal: Principal) -> str:
    """The MLflow workspace of the principal's organisation (``org-`` and a hash of its id), or
    ``local`` for the single local user. The worker fixes it for each run's process."""
    organisation = principal.organisation_id
    return "local" if organisation is None else "org-" + _hex(organisation, 40)


class MlflowStore:
    """An ``EvalStore`` on mlflow-skinny's SQL store in this process.

    ``database_url`` is a SQLAlchemy URL: ``sqlite:///path/mlflow.db`` or
    ``postgresql+psycopg://user:password@host/db`` (install ``martian-sdk[postgres]``). On Postgres,
    MLflow's tables are created in ``schema``. ``artifact_root`` is a local folder; on Postgres a
    root longer than :data:`LONGEST_ARTIFACT_ROOT` is refused with ``ValueError`` before anything
    is written.
    """

    def __init__(self, database_url: str, artifact_root: str | Path, *, schema: str = "mlflow") -> None:
        check_schema(schema)
        self._artifact_root = Path(artifact_root).resolve()
        if is_postgres(database_url) and len(str(self._artifact_root)) > LONGEST_ARTIFACT_ROOT:
            raise ValueError(
                f"artifact_root is {len(str(self._artifact_root))} characters, and on Postgres it can be at most "
                f"{LONGEST_ARTIFACT_ROOT} characters: MLflow keeps a run's artifact folder, the root and "
                f"/<run id>/artifacts, in runs.artifact_uri, which holds {RUN_ARTIFACT_URI_LENGTH}. "
                f"Choose a shorter folder than {self._artifact_root}"
            )
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        url = database_url
        if is_postgres(database_url):
            create_schema(database_url, schema)
            url = postgres_url(database_url, schema)
        # MLflow's stores are untyped.
        self._workspaces: Any = WorkspaceSqlAlchemyStore(url)
        self._store: Any = WorkspaceStore(url, str(self._artifact_root))  # type: ignore[no-untyped-call]
        hide_sql_parameters(self._store)
        self._prompts: Any = PromptRegistryStore(url)  # type: ignore[no-untyped-call]
        hide_sql_parameters(self._prompts)
        self._ready: set[str] = set()

    # --- plumbing ----------------------------------------------------------------------------

    def _workspace(self, principal: Principal) -> str:
        """The MLflow workspace of an organisation, or of the single local user."""
        name = workspace_name(principal)
        if name not in self._ready:
            try:
                self._workspaces.get_workspace(name)
            except MlflowException:
                try:
                    self._workspaces.create_workspace(Workspace(name=name))
                except MlflowException:
                    self._workspaces.get_workspace(name)  # created by a concurrent call
            self._ready.add(name)
        return name

    async def _call(self, principal: Principal, work: Callable[[], T]) -> T:
        """Run ``work`` in the principal's workspace, on a thread. An error MLflow raises becomes a
        ``StoreError`` naming the method ``work`` belongs to (:func:`_store_error`)."""
        workspace = await asyncio.to_thread(self._workspace, principal)
        operation = operation_name(work)

        def run() -> T:
            with ServerWorkspaceContext(workspace):
                try:
                    return work()
                except MlflowException as error:
                    refused = _store_error(error, operation)
                # Raised outside the except clause, so that MLflow's error, and the database's
                # under it, which hold the values written, do not stay on it as its context.
                raise refused

        return await asyncio.to_thread(run)

    def _experiment(self, project_id: str, *, writable: bool = False) -> Any:
        experiment = self._store.get_experiment_by_name(project_id)
        if experiment is None or "martian.project.name" not in experiment.tags:
            raise StoreError("not_found", "project not found")
        if writable and experiment.tags.get("martian.project.archived_at"):
            raise StoreError("conflict", "the project is archived")
        return experiment

    def _find_mlflow_run(self, experiment_id: str, run_id: str) -> Any | None:
        runs = self._store.search_runs(
            [experiment_id], f"tags.`martian.run_id` = '{_quoted(run_id)}'", ViewType.ACTIVE_ONLY, max_results=2
        )
        return runs[0] if runs else None

    def _mlflow_run(self, experiment_id: str, run_id: str) -> Any:
        found = self._find_mlflow_run(experiment_id, run_id)
        if found is None:
            raise StoreError("not_found", "run not found")
        return found

    def _run_file(self, mlflow_run: Any) -> Path:
        return self._local_artifacts(mlflow_run) / "martian" / "run.json"

    def _local_artifacts(self, mlflow_run: Any) -> Path:
        uri = mlflow_run.info.artifact_uri
        path = Path(unquote(urlsplit(uri).path) if uri.startswith("file:") else uri)
        if not path.is_absolute():
            raise StoreError("invalid", "run artifacts must be on a local filesystem")
        return path

    def _trace_info(self, experiment_id: str, trace_id: str) -> TraceInfo:
        try:
            info: TraceInfo = self._store.get_trace_info(_mlflow_trace_id(trace_id))
        except MlflowException:
            raise StoreError("not_found", "trace not found") from None
        if info.experiment_id != experiment_id:
            raise StoreError("not_found", "trace not found")
        return info

    # --- readiness ---------------------------------------------------------------------------

    async def status(self, principal: Principal) -> StoreStatus:
        await self._call(principal, lambda: None)
        return StoreStatus(status="ready", ready=True, provider="mlflow")

    # --- projects ----------------------------------------------------------------------------

    async def create_project(self, principal: Principal, project: Project) -> Project:
        def work() -> Project:
            existing = self._store.get_experiment_by_name(project.id)
            if existing is not None:
                if "martian.project.name" not in existing.tags:
                    # Such as one MLflow tracing created for an application that named the
                    # experiment before the project existed.
                    raise StoreError("conflict", "an MLflow experiment with this id exists and is not a project")
                if existing.tags.get("martian.project.name") != project.name:
                    raise StoreError("conflict", "a project with this id has another name")
                return _project(existing)
            from mlflow.entities import ExperimentTag

            tags = [
                ExperimentTag("martian.project.name", project.name),
                ExperimentTag("martian.project.description", project.description),
            ]
            experiment_id = self._store.create_experiment(project.id, tags=tags)
            return _project(self._store.get_experiment(experiment_id))

        return await self._call(principal, work)

    async def get_project(self, principal: Principal, project_id: str) -> Project | None:
        def work() -> Project | None:
            experiment = self._store.get_experiment_by_name(project_id)
            if experiment is None or "martian.project.name" not in experiment.tags:
                return None
            return _project(experiment)

        return await self._call(principal, work)

    async def list_projects(self, principal: Principal) -> list[Project]:
        def work() -> list[Project]:
            found, token = [], None
            while True:
                page = self._store.search_experiments(
                    view_type=ViewType.ACTIVE_ONLY, max_results=1000, page_token=token
                )
                found += [_project(e) for e in page if "martian.project.name" in e.tags]
                token = page.token
                if not token:
                    return sorted(found, key=lambda p: p.name)

        return await self._call(principal, work)

    async def archive_project(self, principal: Principal, project_id: str) -> Project:
        def work() -> Project:
            from mlflow.entities import ExperimentTag

            experiment = self._experiment(project_id)
            if not experiment.tags.get("martian.project.archived_at"):
                stamp = datetime.now(UTC).isoformat()
                self._store.set_experiment_tag(
                    experiment.experiment_id, ExperimentTag("martian.project.archived_at", stamp)
                )
            return _project(self._store.get_experiment(experiment.experiment_id))

        return await self._call(principal, work)

    # --- datasets ----------------------------------------------------------------------------

    def _datasets(self, experiment_id: str) -> list[Any]:
        found, token = [], None
        while True:
            page = self._store.search_datasets(experiment_ids=[experiment_id], max_results=1000, page_token=token)
            found += [d for d in page if "martian.dataset.name" in (d.tags or {})]
            token = page.token
            if not token:
                return found

    async def create_dataset(
        self, principal: Principal, project_id: str, name: str, cases: list[Case]
    ) -> DatasetVersion:
        if not cases:
            raise StoreError("invalid", "a dataset needs at least one case")
        ids = [case.id for case in cases]
        if len(set(ids)) != len(ids):
            raise StoreError("invalid", "case ids must be unique within a dataset")
        content = digest([case.model_dump(mode="json") for case in cases])

        def work() -> DatasetVersion:
            experiment = self._experiment(project_id, writable=True)
            versions = [d for d in self._datasets(experiment.experiment_id) if d.tags["martian.dataset.name"] == name]
            for existing in versions:
                if existing.tags.get("martian.dataset.digest") == content:
                    return _dataset(existing, project_id)
            version = len(versions) + 1
            created = self._store.create_dataset(
                f"{name} v{version}",
                tags={
                    "martian.dataset.name": name,
                    "martian.dataset.version": str(version),
                    "martian.dataset.digest": content,
                    "martian.dataset.case_count": str(len(cases)),
                    "martian.dataset.created_by": principal.user_id,
                },
                experiment_ids=[experiment.experiment_id],
            )
            records = [
                {
                    "inputs": case.inputs,
                    "expectations": case.expectations or None,
                    "outputs": case.outputs,
                    "tags": {
                        "martian.case_id": case.id,
                        "martian.position": str(position),
                        "martian.case_tags": json.dumps(case.tags, sort_keys=True),
                        "martian.case_metadata": json.dumps(case.metadata, sort_keys=True, default=str),
                    },
                }
                for position, case in enumerate(cases)
            ]
            for start in range(0, len(records), 500):
                self._store.upsert_dataset_records(created.dataset_id, records[start : start + 500])
            return _dataset(self._store.get_dataset(created.dataset_id), project_id)

        return await self._call(principal, work)

    async def list_datasets(self, principal: Principal, project_id: str) -> list[DatasetVersion]:
        def work() -> list[DatasetVersion]:
            experiment = self._experiment(project_id)
            found = [_dataset(d, project_id) for d in self._datasets(experiment.experiment_id)]
            return sorted(found, key=lambda d: (d.name, d.version or 0))

        return await self._call(principal, work)

    async def get_dataset(self, principal: Principal, project_id: str, dataset_id: str) -> DatasetVersion | None:
        def work() -> DatasetVersion | None:
            experiment = self._experiment(project_id)
            found = next((d for d in self._datasets(experiment.experiment_id) if d.dataset_id == dataset_id), None)
            return _dataset(found, project_id) if found else None

        return await self._call(principal, work)

    async def find_dataset(
        self, principal: Principal, project_id: str, name: str, *, digest: str | None = None
    ) -> DatasetVersion | None:
        versions = [d for d in await self.list_datasets(principal, project_id) if d.name == name]
        if digest is not None:
            return next((d for d in versions if d.digest == digest), None)
        return versions[-1] if versions else None

    async def read_cases(
        self, principal: Principal, project_id: str, dataset_id: str, *, offset: int = 0, limit: int = 100
    ) -> Page[Case]:
        if offset < 0 or not 1 <= limit <= 1000:
            raise StoreError("invalid", "offset must be 0 or more and limit between 1 and 1000")

        def work() -> Page[Case]:
            experiment = self._experiment(project_id)
            if not any(d.dataset_id == dataset_id for d in self._datasets(experiment.experiment_id)):
                raise StoreError("not_found", "dataset not found")
            records, token = [], None
            while True:
                page, token = load_dataset_records(self._store, dataset_id, token)
                records += page
                if not token:
                    break
            cases = sorted((_case(r) for r in records), key=lambda pair: pair[0])
            chunk = [case for _, case in cases[offset : offset + limit]]
            return Page[Case](items=chunk, next_cursor=str(offset + limit) if offset + limit < len(cases) else None)

        return await self._call(principal, work)

    # --- runs --------------------------------------------------------------------------------

    async def start_run(self, principal: Principal, run: Run) -> Run:
        def work() -> Run:
            experiment = self._experiment(run.project_id, writable=True)
            try:
                existing = self._mlflow_run(experiment.experiment_id, run.id)
                return Run.model_validate_json(self._run_file(existing).read_text(encoding="utf-8"))
            except StoreError:
                pass
            started = run.started_at or datetime.now(UTC)
            created = self._store.create_run(
                experiment.experiment_id,
                principal.user_id,
                _ms(started),
                [RunTag("martian.run_id", run.id), RunTag("martian.source", run.source)],
                run.code or run.id,
            )
            saved = run.model_copy(
                update={
                    "started_at": started,
                    "store": StoreRef(experiment_id=experiment.experiment_id, run_id=created.info.run_id),
                }
            )
            self._store.update_run_info(created.info.run_id, RunStatus.from_string(_RUN_STATUS[run.state]), None, None)
            path = self._run_file(created)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(saved.model_dump_json(), encoding="utf-8")
            return saved

        return await self._call(principal, work)

    async def get_run(self, principal: Principal, project_id: str, run_id: str) -> Run | None:
        def work() -> Run | None:
            experiment = self._experiment(project_id)
            try:
                found = self._mlflow_run(experiment.experiment_id, run_id)
            except StoreError:
                return None
            return Run.model_validate_json(self._run_file(found).read_text(encoding="utf-8"))

        return await self._call(principal, work)

    async def finish_run(
        self, principal: Principal, project_id: str, run_id: str, *, state: RunState, counts: Counts
    ) -> Run:
        if state in ("queued", "running"):
            raise StoreError("invalid", "a finished run needs a final state")

        def work() -> Run:
            experiment = self._experiment(project_id)
            found = self._mlflow_run(experiment.experiment_id, run_id)
            run = Run.model_validate_json(self._run_file(found).read_text(encoding="utf-8"))
            finished = run.finished_at or datetime.now(UTC)
            run = run.model_copy(update={"state": state, "counts": counts, "finished_at": finished})
            stamp = _ms(finished) or 0
            metrics = [Metric(key, float(value), stamp, 0) for key, value in counts.model_dump().items()]
            self._store.log_batch(found.info.run_id, metrics=metrics, params=[], tags=[])
            self._store.update_run_info(found.info.run_id, RunStatus.from_string(_RUN_STATUS[state]), stamp, None)
            self._run_file(found).write_text(run.model_dump_json(), encoding="utf-8")
            return run

        return await self._call(principal, work)

    # --- traces ------------------------------------------------------------------------------

    async def record_trace(self, principal: Principal, trace: Trace) -> Trace:
        trace = map_trace(trace)

        def work() -> None:
            experiment = self._experiment(trace.project_id, writable=True)
            if trace.run_id is not None:
                self._mlflow_run(experiment.experiment_id, trace.run_id)
            mlflow_id = _mlflow_trace_id(trace.id)
            request_preview, request_shortened = _preview(trace.request)
            response_preview, response_shortened = _preview(trace.response)
            # The binding says which previews it shortened; a trace read back and recorded again
            # brings the keys of the previews it had.
            shortened = {REQUEST_PREVIEW_TRUNCATED, RESPONSE_PREVIEW_TRUNCATED}
            metadata = {key: value for key, value in trace.metadata.items() if key not in shortened}
            for key, value in (
                (SESSION_KEY, trace.session_id),
                (USER_KEY, trace.user_id),
                (MODEL_KEY, trace.model),
                (
                    "mlflow.trace.tokenUsage",
                    None if trace.tokens is None else json.dumps({"total_tokens": trace.tokens}),
                ),
                ("mlflow.trace.cost", None if trace.cost is None else json.dumps({"total_cost": trace.cost})),
                (REQUEST_PREVIEW_TRUNCATED, "true" if request_shortened else None),
                (RESPONSE_PREVIEW_TRUNCATED, "true" if response_shortened else None),
            ):
                if value is not None:
                    metadata[key] = value
            tags = {
                **trace.tags,
                "martian.trace_id": trace.id,
                "mlflow.traceName": trace.name,
                # The spans, if any, are written to this store below; without the tag MLflow
                # looks for them in an artifact store when the trace is read.
                TraceTagKey.SPANS_LOCATION: SpansLocation.TRACKING_STORE.value,
            }
            if trace.run_id is not None:
                tags["martian.run_id"] = trace.run_id
            info = TraceInfo(
                trace_id=mlflow_id,
                trace_location=TraceLocation.from_experiment_id(experiment.experiment_id),
                # A given time is kept even at the epoch, where MLflow puts a trace whose span had no start.
                request_time=_ms(trace.request_time or datetime.now(UTC)) or 0,
                state=TraceState(_TRACE_STATE[trace.state]),
                execution_duration=None if trace.latency_ms is None else int(trace.latency_ms),
                request_preview=request_preview,
                response_preview=response_preview,
                trace_metadata=metadata,
                tags=tags,
            )
            self._store.start_trace(info)
            root = _root_span(mlflow_id, trace, info.request_time)
            spans = [_mlflow_span(mlflow_id, s, info.request_time, root.span_id) for s in trace.spans]
            self._store.log_spans(experiment.experiment_id, [root, *spans])
            if info.state in (TraceState.IN_PROGRESS, TraceState.STATE_UNSPECIFIED):
                # MLflow gives a trace in either state the state of its root span. Written again,
                # with the times MLflow took from the spans, the trace keeps its own.
                spanned = self._store.get_trace_info(mlflow_id)
                self._store.start_trace(
                    replace(info, request_time=spanned.request_time, execution_duration=spanned.execution_duration)
                )

        await self._call(principal, work)
        for score in trace.scores:
            await self.log_score(principal, trace.project_id, trace.id, score)
        saved = await self.get_trace(principal, trace.project_id, trace.id)
        assert saved is not None
        return saved

    def _run_ids(self, experiment_id: str, infos: list[TraceInfo]) -> dict[str, str]:
        """The run id of each MLflow run that the traces without a ``martian.run_id`` tag name in
        ``mlflow.sourceRun``, when that MLflow run is a run's in the same experiment."""
        sources = sorted(
            {
                source
                for info in infos
                if "martian.run_id" not in (info.tags or {})
                and _MLFLOW_RUN_ID.match(source := (info.trace_metadata or {}).get(SOURCE_RUN_KEY, ""))
            }
        )
        found: dict[str, str] = {}
        for start in range(0, len(sources), 100):
            listed = ", ".join(f"'{source}'" for source in sources[start : start + 100])
            for mlflow_run in self._store.search_runs(
                [experiment_id], f"attributes.run_id IN ({listed})", ViewType.ACTIVE_ONLY, max_results=100
            ):
                if run_id := mlflow_run.data.tags.get("martian.run_id"):
                    found[mlflow_run.info.run_id] = run_id
        return found

    def _searches(self, experiment_id: str, query: TraceQuery) -> list[_Search]:
        """The MLflow searches a query is, each marked whole when MLflow applies the whole query to
        it, so that every trace it returns matches."""
        whole = not query.text
        filters = []
        for key, value in ((SESSION_KEY, query.session_id), (USER_KEY, query.user_id), (MODEL_KEY, query.model)):
            if value is not None:
                if _SAFE_FILTER_VALUE.match(value):
                    filters.append(f"metadata.`{key}` = '{value}'")
                else:
                    whole = False
        for key, value in query.tags.items():
            if _SAFE_FILTER_VALUE.match(key) and _SAFE_FILTER_VALUE.match(value):
                filters.append(f"tags.`{key}` = '{value}'")
            else:
                whole = False
        if query.trace_ids:
            listed = ", ".join(f"'{_mlflow_trace_id(trace_id)}'" for trace_id in sorted(set(query.trace_ids)))
            filters.append(f"trace.request_id IN ({listed})")
        if query.states:
            listed = ", ".join(f"'{_TRACE_STATE[state]}'" for state in sorted(set(query.states)))
            filters.append(f"trace.status IN ({listed})")
        if query.since is not None:
            filters.append(f"trace.timestamp_ms >= {_ms_at_or_after(query.since)}")
        if query.until is not None:
            filters.append(f"trace.timestamp_ms < {_ms_at_or_after(query.until)}")
        if query.run_id is None or not _SAFE_FILTER_VALUE.match(query.run_id):
            return [_Search(" AND ".join(filters) or None, whole=whole and query.run_id is None)]
        # A run's traces are the ones tagged with it and the untagged ones MLflow tracing recorded
        # in its MLflow run. MLflow's filters have no OR, so each is a search of its own; the
        # second also finds untagged traces MLflow linked to the MLflow run some other way, which
        # are not the run's, so it is never whole.
        tagged = _Search(" AND ".join([*filters, f"tags.`martian.run_id` = '{query.run_id}'"]), whole=whole)
        mlflow_run = self._find_mlflow_run(experiment_id, query.run_id)
        if mlflow_run is None:
            return [tagged, _Search(None, whole=True, done=True)]
        source = f"metadata.`{SOURCE_RUN_KEY}` = '{mlflow_run.info.run_id}'"
        return [tagged, _Search(" AND ".join([*filters, source, "tags.`martian.run_id` IS NULL"]), whole=False)]

    def _scan(self, query: TraceQuery, want: int) -> tuple[list[Trace], str | None]:
        """Up to ``want`` traces the query matches, in its order from its cursor on, and the
        cursor after them, or ``None`` when no trace follows. MLflow pages each search by offset,
        and a cursor holds each search's offset, joined by ``.``."""
        experiment_id = self._experiment(query.project_id).experiment_id
        searches = self._searches(experiment_id, query)
        for search, offset in zip(searches, _cursor_offsets(query.cursor, len(searches)), strict=True):
            search.offset = offset
        # MLflow breaks a tie in time by its trace id, ascending, either way.
        order_by = ["timestamp_ms ASC" if query.oldest_first else "timestamp_ms DESC"]

        def key(info: TraceInfo) -> tuple[int, str]:
            return (info.request_time if query.oldest_first else -info.request_time, info.trace_id)

        found: list[Trace] = []
        runs: dict[str, str] = {}
        read = 0
        while len(found) < want and read < TRACE_SCAN_LIMIT:
            for search in searches:
                if not search.rows and not search.done:
                    # Every trace of a whole search matches, so it reads no more than are wanted.
                    chunk = want - len(found) if search.whole else max(want, _SCAN_CHUNK)
                    page, token = self._store.search_traces(
                        locations=[experiment_id],
                        filter_string=search.filter,
                        max_results=min(chunk, TRACE_SCAN_LIMIT - read),
                        order_by=order_by,
                        page_token=trace_page_token(search.offset),
                    )
                    search.rows.extend(page)
                    search.done = token is None
                    runs.update(self._run_ids(experiment_id, page))
            ready = [search for search in searches if search.rows]
            if not ready:
                break
            search = min(ready, key=lambda s: key(s.rows[0]))
            trace = _trace(search.rows.popleft(), query.project_id, runs=runs)
            search.offset += 1
            read += 1
            if _matches(trace, query):
                found.append(trace)
        more = any(search.rows or not search.done for search in searches)
        return found, ".".join(str(search.offset) for search in searches) if more else None

    async def search_traces(self, principal: Principal, query: TraceQuery) -> Page[Trace]:
        def work() -> Page[Trace]:
            found, cursor = self._scan(query, query.limit)
            return Page[Trace](items=found, next_cursor=cursor)

        return await self._call(principal, work)

    async def search_sessions(
        self, principal: Principal, query: TraceQuery, *, max_traces: int = SESSION_TRACE_LIMIT
    ) -> SessionSearch:
        check_session_search(query, max_traces)

        def work() -> SessionSearch:
            found, cursor = self._scan(query, max_traces)
            return sessions_from_traces(found, has_more=cursor is not None, limit=max_traces)

        return await self._call(principal, work)

    async def get_trace(self, principal: Principal, project_id: str, trace_id: str) -> Trace | None:
        def work() -> Trace | None:
            experiment = self._experiment(project_id)
            try:
                info = self._trace_info(experiment.experiment_id, trace_id)
            except StoreError:
                return None
            # The binding writes a trace's spans with it, so a partial read is complete.
            full: MlflowTrace = self._store.get_trace(info.trace_id, allow_partial=True)
            runs = self._run_ids(experiment.experiment_id, [info])
            root = next((s for s in full.data.spans if s.get_attribute(TRACE_SPAN_KEY) is True), None)
            # MLflow orders spans root first, then by start time. Below the trace's root span the
            # trace's own spans without a parent are no roots there, so they are put first again.
            spans = sorted((_span(s) for s in full.data.spans if s is not root), key=lambda s: s.parent_id is not None)
            # A trace an earlier release or MLflow tracing itself wrote has no root span of the binding's.
            return _trace(info, project_id, spans=spans, runs=runs, root=root)

        return await self._call(principal, work)

    async def set_trace_tag(self, principal: Principal, project_id: str, trace_id: str, key: str, value: str) -> None:
        if not key:
            raise StoreError("invalid", "a tag needs a key")

        def work() -> None:
            experiment = self._experiment(project_id)
            info = self._trace_info(experiment.experiment_id, trace_id)
            self._store.set_trace_tag(info.trace_id, key, value)

        await self._call(principal, work)

    async def link_traces(self, principal: Principal, project_id: str, run_id: str, trace_ids: list[str]) -> None:
        def work() -> None:
            experiment = self._experiment(project_id)
            found = self._mlflow_run(experiment.experiment_id, run_id)
            infos = [self._trace_info(experiment.experiment_id, trace_id) for trace_id in trace_ids]
            if infos:
                self._store.link_traces_to_run([info.trace_id for info in infos], found.info.run_id)
            for info in infos:
                self._store.set_trace_tag(info.trace_id, "martian.run_id", run_id)

        await self._call(principal, work)

    # --- scores ------------------------------------------------------------------------------

    async def log_score(self, principal: Principal, project_id: str, trace_id: str, score: Score) -> Score:
        def work() -> Score:
            experiment = self._experiment(project_id)
            info = self._trace_info(experiment.experiment_id, trace_id)
            metadata = {
                "martian.trace_id": trace_id,
                "martian.passed": json.dumps(score.passed),
                "martian.scorer_version": score.scorer_version,
                "martian.metadata": json.dumps(score.metadata, sort_keys=True, default=str),
            }
            for key, value in (
                ("martian.run_id", score.run_id),
                ("martian.case_id", score.case_id),
                ("martian.scoring_job_id", score.scoring_job_id),
            ):
                if value is not None:
                    metadata[key] = value
            feedback = Feedback(
                name=score.scorer,
                value=score.value,
                rationale=score.rationale or None,
                source=AssessmentSource(
                    source_type=_SCORE_SOURCE[score.source], source_id=score.source_id or "martian"
                ),
                trace_id=info.trace_id,
                metadata=metadata,
                error=AssessmentError(error_code="MARTIAN_SCORE_ERROR", error_message=score.error)
                if score.error
                else None,
                # A score keeps the time it was given, as an imported one must; MLflow stamps one without.
                create_time_ms=_ms(score.created_at),
            )
            created = self._store.create_assessment(feedback)
            return _score(created, project_id, trace_id)

        return await self._call(principal, work)

    async def list_scores(self, principal: Principal, project_id: str, trace_id: str) -> list[Score]:
        def work() -> list[Score]:
            experiment = self._experiment(project_id)
            info = self._trace_info(experiment.experiment_id, trace_id)
            return _scores(info, project_id, trace_id)

        return await self._call(principal, work)

    # --- registered scorers ------------------------------------------------------------------

    async def register_scorer(self, principal: Principal, project_id: str, scorer: ScorerVersion) -> ScorerVersion:
        def work() -> ScorerVersion:
            experiment = self._experiment(project_id, writable=True)
            # In the shape MLflow expects of a decorator scorer, so MLflow's own tools can list it;
            # the scorer itself is kept in ``martian_scorer``.
            payload = {
                "name": scorer.name,
                "call_source": "",
                "call_signature": "",
                "original_func_name": scorer.name,
                "martian_scorer": scorer.model_dump(mode="json", exclude={"version"}),
            }
            created = self._store.register_scorer(experiment.experiment_id, scorer.name, json.dumps(payload))
            return _scorer(created)

        return await self._call(principal, work)

    async def list_scorers(self, principal: Principal, project_id: str) -> list[ScorerVersion]:
        def work() -> list[ScorerVersion]:
            experiment = self._experiment(project_id)
            return sorted(
                (_scorer(s) for s in self._store.list_scorers(experiment.experiment_id)), key=lambda s: s.name
            )

        return await self._call(principal, work)

    async def scorer_versions(self, principal: Principal, project_id: str, name: str) -> list[ScorerVersion]:
        def work() -> list[ScorerVersion]:
            experiment = self._experiment(project_id)
            try:
                versions = self._store.list_scorer_versions(experiment.experiment_id, name)
            except MlflowException:
                return []
            return sorted((_scorer(v) for v in versions), key=lambda s: int(s.version))

        return await self._call(principal, work)

    # --- prompts -----------------------------------------------------------------------------

    async def create_prompt(self, principal: Principal, name: str, *, description: str = "") -> Prompt:
        check_prompt(name, description=description)

        def work() -> Prompt:
            if (existing := self._prompts.get_prompt(name)) is not None:
                found = _prompt(existing)
                if found.description != description:
                    raise StoreError("conflict", "a prompt with this name already exists")
                return found
            tags = {PROMPT_CREATED_BY: principal.user_id}
            return _prompt(self._prompts.create_prompt(name, description=description or None, tags=tags))

        return await self._call(principal, work)

    async def get_prompt(self, principal: Principal, name: str) -> Prompt | None:
        def work() -> Prompt | None:
            found = self._prompts.get_prompt(name)
            return None if found is None else _prompt(found)

        return await self._call(principal, work)

    async def list_prompts(self, principal: Principal) -> list[Prompt]:
        def work() -> list[Prompt]:
            found: list[Prompt] = []
            token = None
            while True:
                page = self._prompts.search_prompts(max_results=100, page_token=token)
                found.extend(_prompt(prompt) for prompt in page)
                if not (token := page.token):
                    return sorted(found, key=lambda prompt: prompt.name)

        return await self._call(principal, work)

    async def add_prompt_version(
        self, principal: Principal, name: str, text: str, *, message: str = ""
    ) -> PromptVersion:
        check_prompt(name, text, message=message)

        def work() -> PromptVersion:
            if self._prompts.get_prompt(name) is None:
                raise StoreError("not_found", "prompt not found")
            tags = {PROMPT_DIGEST: prompt_digest(text), PROMPT_AUTHOR: principal.user_id}
            return _prompt_version(
                self._prompts.create_prompt_version(name, text, description=message or None, tags=tags)
            )

        return await self._call(principal, work)

    async def prompt_versions(self, principal: Principal, name: str) -> list[PromptVersion]:
        def work() -> list[PromptVersion]:
            if self._prompts.get_prompt(name) is None:
                raise StoreError("not_found", "prompt not found")
            found: list[PromptVersion] = []
            token = None
            while True:
                page = self._prompts.search_prompt_versions(name, max_results=100, page_token=token)
                found.extend(_prompt_version(version) for version in page)
                if not (token := page.token):
                    return sorted(found, key=lambda version: version.version)

        return await self._call(principal, work)

    async def import_prompt(
        self, principal: Principal, prompt: Prompt, versions: list[PromptVersion]
    ) -> list[PromptVersion]:
        check_imported_prompt(prompt, versions)
        name = prompt.name

        def work() -> list[PromptVersion]:
            if self._prompts.get_prompt(name) is not None:
                raise StoreError("conflict", "a prompt with this name already exists")
            made_by = {} if prompt.created_by is None else {PROMPT_CREATED_BY: prompt.created_by}
            self._prompts.create_prompt(name, description=prompt.description or None, tags=made_by)
            for version in versions:
                author = {} if version.created_by is None else {PROMPT_AUTHOR: version.created_by}
                made = self._prompts.create_prompt_version(
                    name,
                    version.text,
                    description=version.message or None,
                    tags={PROMPT_DIGEST: version.digest, **author},
                )
                if int(made.version) != version.version:
                    raise StoreError(
                        "conflict", f"version {version.version} of prompt {name} got number {made.version}"
                    )
            # MLflow stamps the current time on what it creates; keep the export's.
            times = {version.version: _ms(version.created_at) for version in versions}
            self._prompts.keep_prompt_times(name, _ms(prompt.created_at), times)
            return [_prompt_version(self._prompts.get_prompt_version(name, version.version)) for version in versions]

        return await self._call(principal, work)

    async def find_prompt_versions(self, principal: Principal, digest: str) -> list[PromptVersion]:
        if not _DIGEST.fullmatch(digest):
            return []

        def work() -> list[PromptVersion]:
            # MLflow searches prompt versions only when the filter names its prompt tag.
            matching = self._prompts.search_model_versions(
                filter_string=f"tag.`{IS_PROMPT_TAG_KEY}` = 'true' AND tag.`{PROMPT_DIGEST}` = '{digest}'"
            )
            found = [_prompt_version(self._prompts.get_prompt_version(m.name, m.version)) for m in matching]
            return sorted(found, key=lambda version: (version.created_at or _EPOCH, version.name, version.version))

        return await self._call(principal, work)

    # --- review questions and queues --------------------------------------------------------

    def _question(self, experiment_id: str, question: ReviewQuestion) -> Any:
        try:
            existing = self._store.get_label_schema_by_name(experiment_id, question.name)
        except MlflowException:
            existing = None
        if existing is not None:
            if _question(existing) != question:
                raise StoreError("conflict", "a question with this name is defined differently")
            return existing
        return self._store.create_label_schema(
            experiment_id,
            name=question.name,
            type=LabelSchemaType.FEEDBACK,
            input=_label_input(question),
            instruction=question.instruction,
        )

    async def save_question(self, principal: Principal, project_id: str, question: ReviewQuestion) -> ReviewQuestion:
        def work() -> ReviewQuestion:
            experiment = self._experiment(project_id, writable=True)
            return _question(self._question(experiment.experiment_id, question))

        return await self._call(principal, work)

    async def list_questions(self, principal: Principal, project_id: str) -> list[ReviewQuestion]:
        def work() -> list[ReviewQuestion]:
            experiment = self._experiment(project_id)
            found, token = [], None
            while True:
                page = self._store.list_label_schemas(experiment.experiment_id, max_results=100, page_token=token)
                # MLflow adds schemas of its own, such as Feedback; only valid question names are questions.
                found += [_question(schema) for schema in page if _QUESTION_NAME.match(schema.name)]
                token = page.token
                if not token:
                    return sorted(found, key=lambda q: q.name)

        return await self._call(principal, work)

    async def create_queue(
        self, principal: Principal, project_id: str, queue: ReviewQueue, trace_ids: list[str]
    ) -> ReviewQueue:
        if len(trace_ids) != len(queue.case_ids):
            raise StoreError("invalid", "give one trace per case in the queue")

        def work() -> ReviewQueue:
            from mlflow.entities import ExperimentTag

            experiment = self._experiment(project_id, writable=True)
            infos = [self._trace_info(experiment.experiment_id, trace_id) for trace_id in trace_ids]
            key = f"martian.queue.{queue.id}"
            if stored := experiment.tags.get(key):
                return ReviewQueue.model_validate_json(json.loads(stored)["queue"])
            schemas = [self._question(experiment.experiment_id, question) for question in queue.questions]
            created = self._store.create_review_queue(
                experiment.experiment_id,
                name=queue.id,
                queue_type="custom",
                created_by=principal.user_id,
                schema_ids=[schema.schema_id for schema in schemas],
            )
            self._store.add_items_to_review_queue(created.queue_id, item_ids=[info.trace_id for info in infos])
            saved = queue.model_copy(
                update={
                    "created_at": queue.created_at or datetime.now(UTC),
                    "store": StoreRef(experiment_id=experiment.experiment_id),
                }
            )
            record = {
                "queue": saved.model_dump_json(),
                "native_id": created.queue_id,
                "traces": dict(zip(trace_ids, queue.case_ids, strict=True)),
            }
            self._store.set_experiment_tag(experiment.experiment_id, ExperimentTag(key, json.dumps(record)))
            return saved

        return await self._call(principal, work)

    def _queue_record(self, project_id: str, queue_id: str) -> tuple[Any, dict[str, Any]] | None:
        experiment = self._experiment(project_id)
        stored = experiment.tags.get(f"martian.queue.{queue_id}")
        return (experiment, json.loads(stored)) if stored else None

    async def get_queue(self, principal: Principal, project_id: str, queue_id: str) -> ReviewQueue | None:
        def work() -> ReviewQueue | None:
            found = self._queue_record(project_id, queue_id)
            return ReviewQueue.model_validate_json(found[1]["queue"]) if found else None

        return await self._call(principal, work)

    def _items(self, project_id: str, queue_id: str) -> tuple[dict[str, Any], list[QueueItem]]:
        found = self._queue_record(project_id, queue_id)
        if found is None:
            raise StoreError("not_found", "queue not found")
        record = found[1]
        by_mlflow_id = {_mlflow_trace_id(trace_id): trace_id for trace_id in record["traces"]}
        items, token = [], None
        while True:
            page = self._store.list_review_queue_items(record["native_id"], max_results=1000, page_token=token)
            items += list(page)
            token = getattr(page, "token", None)
            if not token:
                break
        order = list(record["traces"])
        result = []
        for item in items:
            trace_id = by_mlflow_id.get(item.item_id)
            if trace_id is None:
                continue
            status: Literal["pending", "completed"] = "completed" if str(item.status.value) == "complete" else "pending"
            result.append(
                QueueItem(queue_id=queue_id, trace_id=trace_id, case_id=record["traces"][trace_id], status=status)
            )
        result.sort(key=lambda item: order.index(item.trace_id))
        return record, result

    async def queue_items(self, principal: Principal, project_id: str, queue_id: str) -> list[QueueItem]:
        return await self._call(principal, lambda: self._items(project_id, queue_id)[1])

    async def set_queue_item(
        self,
        principal: Principal,
        project_id: str,
        queue_id: str,
        trace_id: str,
        status: Literal["pending", "completed"],
    ) -> QueueItem:
        def work() -> QueueItem:
            record, items = self._items(project_id, queue_id)
            item = next((i for i in items if i.trace_id == trace_id), None)
            if item is None:
                raise StoreError("not_found", "the trace is not in this queue")
            self._store.set_review_queue_item_status(
                record["native_id"],
                item_id=_mlflow_trace_id(trace_id),
                status="complete" if status == "completed" else "pending",
                completed_by=principal.user_id if status == "completed" else None,
            )
            return item.model_copy(update={"status": status})

        return await self._call(principal, work)

    # --- run artifacts -----------------------------------------------------------------------

    def _artifact_path(self, project_id: str, run_id: str, name: str, *, writable: bool = False) -> Path:
        parts = name.split("/")
        if not name or name.startswith("/") or any(part in ("", ".", "..") for part in parts):
            raise StoreError("invalid", "an artifact name is a relative path without . or .. parts")
        experiment = self._experiment(project_id, writable=writable)
        found = self._mlflow_run(experiment.experiment_id, run_id)
        return self._local_artifacts(found) / "files" / name

    async def put_artifact(self, principal: Principal, project_id: str, run_id: str, name: str, data: bytes) -> None:
        def work() -> None:
            path = self._artifact_path(project_id, run_id, name, writable=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await self._call(principal, work)

    async def get_artifact(self, principal: Principal, project_id: str, run_id: str, name: str) -> bytes | None:
        def work() -> bytes | None:
            path = self._artifact_path(project_id, run_id, name)
            return path.read_bytes() if path.is_file() else None

        return await self._call(principal, work)

    async def list_artifacts(self, principal: Principal, project_id: str, run_id: str) -> list[str]:
        def work() -> list[str]:
            experiment = self._experiment(project_id)
            found = self._mlflow_run(experiment.experiment_id, run_id)
            root = self._local_artifacts(found) / "files"
            if not root.is_dir():
                return []
            return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())

        return await self._call(principal, work)


# --- conversions -------------------------------------------------------------------------------


def _quoted(value: str) -> str:
    if not _SAFE_FILTER_VALUE.match(value):
        raise StoreError("invalid", "an id must not contain quotes or backslashes")
    return value


def _store_error(error: MlflowException, operation: str) -> StoreError:
    """The store's error for MLflow's: the operation and what was refused, never a value. A
    database error under MLflow's is reported by its table and its code (:func:`database_refusal`).
    MLflow's own message is passed on only when it is its length refusal, which names the field
    and the limit; its other messages, and the database's, can quote what was written."""
    code = str(getattr(error, "error_code", ""))
    if "DOES_NOT_EXIST" in code or "NOT_FOUND" in code:
        return StoreError("not_found", f"{operation}: not found")
    if "ALREADY_EXISTS" in code:
        return StoreError("conflict", f"{operation}: already exists")
    if (cause := database_cause(error)) is not None:
        return database_refusal(operation, cause)
    if _LENGTH_REFUSAL.fullmatch(error.message):
        return StoreError("invalid", f"{operation}: {error.message}")
    said = f" ({code})" if _ERROR_CODE.fullmatch(code) else ""
    return StoreError("invalid", f"{operation}: MLflow refused the request{said}")


def _mlflow_trace_id(trace_id: str) -> str:
    return trace_id if _MLFLOW_TRACE_ID.match(trace_id) else "tr-" + _hex(trace_id, 32)


def _project(experiment: Any) -> Project:
    tags = experiment.tags
    archived = tags.get("martian.project.archived_at")
    return Project(
        id=experiment.name,
        name=tags["martian.project.name"],
        description=tags.get("martian.project.description", ""),
        created_at=_from_ms(experiment.creation_time),
        archived_at=datetime.fromisoformat(archived) if archived else None,
        store=StoreRef(experiment_id=experiment.experiment_id),
    )


def _dataset(found: Any, project_id: str) -> DatasetVersion:
    tags = found.tags
    return DatasetVersion(
        id=found.dataset_id,
        name=tags["martian.dataset.name"],
        version=int(tags["martian.dataset.version"]),
        digest=tags["martian.dataset.digest"],
        case_count=int(tags["martian.dataset.case_count"]),
        source="upload",
        created_at=_from_ms(found.created_time),
        created_by=tags.get("martian.dataset.created_by"),
        store=StoreRef(dataset_id=found.dataset_id),
    )


def _case(record: Any) -> tuple[int, Case]:
    tags = record.tags or {}
    return int(tags.get("martian.position", "0")), Case(
        id=tags.get("martian.case_id") or record.dataset_record_id,
        inputs=record.inputs,
        expectations=record.expectations or {},
        outputs=record.outputs,
        tags=json.loads(tags.get("martian.case_tags", "{}")),
        metadata=json.loads(tags.get("martian.case_metadata", "{}")),
    )


def _preview(text: str) -> tuple[str, bool]:
    """MLflow's preview of a request or a response, and whether it is shortened: the text itself
    when it fits MLflow's preview columns, otherwise its start and ``...``, as MLflow tracing
    shortens its own previews (``mlflow.tracing.utils.truncation``)."""
    if len(text) <= TRACE_PREVIEW_LENGTH:
        return text, False
    return text[: TRACE_PREVIEW_LENGTH - 3] + "...", True


def _root_span(trace_id: str, trace: Trace, trace_start_ms: int) -> MlflowSpan:
    """The trace's root span, as MLflow tracing records one: its inputs are the trace's request
    and its outputs its response, whole, and its status the trace's state. It starts at the trace's
    request time and ends after its latency, or stays open without one, so it moves neither."""
    start = trace_start_ms * 1_000_000
    code = {"ok": "STATUS_CODE_OK", "error": "STATUS_CODE_ERROR"}.get(trace.state, "STATUS_CODE_UNSET")
    return MlflowSpan.from_dict(
        {
            "trace_id": trace_id,
            "span_id": _hex(f"{trace_id}/root", 16),
            "parent_span_id": None,
            "name": trace.name,
            "start_time_unix_nano": start,
            "end_time_unix_nano": None if trace.latency_ms is None else start + int(trace.latency_ms * 1_000_000),
            "status": {"code": code, "message": ""},
            "attributes": {
                "mlflow.traceRequestId": json.dumps(trace_id),
                "mlflow.spanType": json.dumps("UNKNOWN"),
                # Serialized as MLflow tracing serializes inputs and outputs.
                "mlflow.spanInputs": json.dumps(trace.request, ensure_ascii=False),
                "mlflow.spanOutputs": json.dumps(trace.response, ensure_ascii=False),
                TRACE_SPAN_KEY: json.dumps(True),
            },
            "events": [],
        }
    )


def _mlflow_span(trace_id: str, span: Span, trace_start_ms: int, root_id: str) -> MlflowSpan:
    """The span in MLflow. A span without a parent is a child of the trace's root span there, and
    reads back without one (``martian.parent_id``)."""

    def span_id(value: str) -> str:
        return value if _MLFLOW_SPAN_ID.match(value) else _hex(value, 16)

    # The span's own attributes first: its kind, inputs and outputs are MLflow's span type, inputs
    # and outputs attributes, and the store's keys must not be overwritten by an attribute.
    attributes = {
        **{key: json.dumps(value, default=str) for key, value in span.attributes.items()},
        "mlflow.traceRequestId": json.dumps(trace_id),
        "mlflow.spanType": json.dumps(span.kind or "UNKNOWN"),
        "mlflow.spanInputs": json.dumps(span.inputs, default=str),
        "mlflow.spanOutputs": json.dumps(span.outputs, default=str),
        "martian.span_id": json.dumps(span.id),
        "martian.parent_id": json.dumps(span.parent_id),
        "martian.span_status": json.dumps(span.status),
        # MLflow needs a start time; the span's own times, missing or not, are kept here.
        "martian.start_ms": json.dumps(span.start_ms),
        "martian.end_ms": json.dumps(span.end_ms),
    }
    code = {"ok": "STATUS_CODE_OK", "error": "STATUS_CODE_ERROR"}.get(span.status, "STATUS_CODE_UNSET")
    # MLflow starts a trace at its earliest span, so a span with no start time starts with its trace.
    start = int((trace_start_ms if span.start_ms is None else span.start_ms) * 1_000_000)
    return MlflowSpan.from_dict(
        {
            "trace_id": trace_id,
            "span_id": span_id(span.id),
            "parent_span_id": span_id(span.parent_id) if span.parent_id else root_id,
            "name": span.name,
            "start_time_unix_nano": start,
            "end_time_unix_nano": int(span.end_ms * 1_000_000) if span.end_ms is not None else start,
            "status": {"code": code, "message": ""},
            "attributes": attributes,
            "events": [],
        }
    )


def _span(span: Any) -> Span:
    attributes = dict(span.attributes or {})
    ours = {"martian.span_id", "martian.parent_id", "martian.span_status", "martian.start_ms", "martian.end_ms"}
    reserved = {"mlflow.traceRequestId", "mlflow.spanType", "mlflow.spanInputs", "mlflow.spanOutputs"}
    recorded = attributes.get("martian.span_status")
    if recorded is None:
        recorded = str(getattr(span.status.status_code, "value", span.status.status_code))
    status = _SPAN_STATUS.get(str(recorded), "unknown")
    return Span(
        id=attributes.get("martian.span_id") or span.span_id,
        parent_id=attributes.get("martian.parent_id", span.parent_id),
        name=span.name or "",
        kind=str(span.span_type or ""),
        status=status,
        start_ms=attributes["martian.start_ms"]
        if "martian.start_ms" in attributes
        else (span.start_time_ns / 1_000_000 if span.start_time_ns is not None else None),
        end_ms=attributes["martian.end_ms"]
        if "martian.end_ms" in attributes
        else (span.end_time_ns / 1_000_000 if span.end_time_ns is not None else None),
        inputs=span.inputs,
        outputs=span.outputs,
        attributes={k: v for k, v in attributes.items() if k not in ours | reserved},
    )


def _scores(info: TraceInfo, project_id: str, trace_id: str) -> list[Score]:
    found = [_score(a, project_id, trace_id) for a in info.assessments or [] if isinstance(a, Feedback)]
    return sorted(found, key=lambda s: (s.created_at or datetime.min.replace(tzinfo=UTC), s.store.assessment_id or ""))


def _score(feedback: Any, project_id: str, trace_id: str) -> Score:
    metadata = dict(feedback.metadata or {})
    passed = json.loads(metadata.get("martian.passed", "null"))
    source = feedback.source.source_type
    error = feedback.error.error_message if feedback.error is not None else ""
    return Score(
        run_id=metadata.get("martian.run_id"),
        case_id=metadata.get("martian.case_id"),
        trace_id=trace_id,
        scorer=feedback.name,
        scorer_version=metadata.get("martian.scorer_version", ""),
        source=_SCORE_SOURCE_BACK.get(str(source), "code"),
        source_id="" if feedback.source.source_id == "martian" else (feedback.source.source_id or ""),
        value=feedback.value,
        passed=passed if isinstance(passed, bool) else None,
        rationale=feedback.rationale or "",
        error=error or "",
        scoring_job_id=metadata.get("martian.scoring_job_id"),
        metadata=json.loads(metadata.get("martian.metadata", "{}")),
        created_at=_from_ms(feedback.create_time_ms),
        store=StoreRef(experiment_id=None, trace_id=trace_id, assessment_id=feedback.assessment_id),
    )


def _trace(
    info: TraceInfo,
    project_id: str,
    *,
    spans: list[Span] | None = None,
    runs: dict[str, str] | None = None,
    root: MlflowSpan | None = None,
) -> Trace:
    """The trace in the model. ``runs`` maps the MLflow run ids traces name in
    ``mlflow.sourceRun`` to run ids, for a trace without a ``martian.run_id`` tag. ``root`` is the
    root span the binding wrote, whose inputs and outputs are the trace's request and response
    whole; without it they are MLflow's previews. A trace that names no mapping version, such as
    one MLflow tracing recorded, is mapped as it is read, after its request and response are read,
    from ``spans``, which never hold the binding's root span."""
    metadata = dict(info.trace_metadata or {})
    tags = dict(info.tags or {})
    usage = json.loads(metadata.get("mlflow.trace.tokenUsage", "null") or "null")
    cost = json.loads(metadata.get("mlflow.trace.cost", "null") or "null")
    trace_id = tags.pop("martian.trace_id", info.trace_id)
    run_id = tags.pop("martian.run_id", None)
    if run_id is None and runs:
        run_id = runs.get(metadata.get(SOURCE_RUN_KEY, ""))
    name = tags.pop("mlflow.traceName", "")
    known = {
        SESSION_KEY,
        USER_KEY,
        MODEL_KEY,
        "mlflow.trace.tokenUsage",
        "mlflow.trace.cost",
        "martian.request",
        "martian.response",
    }
    state = str(getattr(info.state, "value", info.state))
    trace = Trace(
        id=trace_id,
        project_id=project_id,
        name=name,
        state=_TRACE_STATE_BACK.get(state, "unknown"),
        request_time=_from_ms(info.request_time),
        latency_ms=float(info.execution_duration) if info.execution_duration is not None else None,
        request=root.inputs if root is not None else metadata.get("martian.request", info.request_preview or ""),
        response=root.outputs if root is not None else metadata.get("martian.response", info.response_preview or ""),
        run_id=run_id,
        session_id=metadata.get(SESSION_KEY),
        user_id=metadata.get(USER_KEY),
        model=metadata.get(MODEL_KEY),
        tokens=usage.get("total_tokens") if isinstance(usage, dict) else None,
        cost=cost.get("total_cost") if isinstance(cost, dict) else None,
        tags={k: v for k, v in tags.items() if not k.startswith("mlflow.")},
        metadata={k: v for k, v in metadata.items() if k not in known and not k.startswith("mlflow.")},
        scores=_scores(info, project_id, trace_id),
        spans=spans or [],
    )
    return map_trace(trace)


def trace_from_mlflow(trace: MlflowTrace, project_id: str) -> Trace:
    """A trace MLflow recorded, such as one of its evaluation harness's, in the model, with its
    spans and its feedback assessments as scores. Its ``id`` is MLflow's trace id."""
    return _trace(trace.info, project_id, spans=[_span(span) for span in trace.data.spans])


@dataclass
class _Search:
    """One MLflow search of a trace query: its filter, whether MLflow applies the whole query in it,
    how many of its traces were read, and the ones read from MLflow but not yet looked at."""

    filter: str | None
    whole: bool
    done: bool = False
    offset: int = 0
    rows: deque[TraceInfo] = field(default_factory=deque)


def _cursor_offsets(cursor: str | None, searches: int) -> list[int]:
    """Where each of a query's MLflow searches resumes: none without a cursor, else the offsets a
    ``next_cursor`` holds, one per search. Any other cursor is refused as ``invalid``."""
    if not cursor:
        return [0] * searches
    parts = cursor.split(".")
    if len(parts) != searches or not all(_CURSOR_OFFSET.fullmatch(part) for part in parts):
        raise StoreError("invalid", _CURSOR_REFUSED)
    return [int(part) for part in parts]


def _ms_at_or_after(when: datetime) -> int:
    """The first whole millisecond at or after ``when``. MLflow keeps a trace's time in whole
    milliseconds, so a trace is at or after ``when`` exactly when its milliseconds are at or after
    this, and before ``when`` exactly when they are before it."""
    since_epoch = (when if when.tzinfo is not None else when.astimezone(UTC)) - datetime(1970, 1, 1, tzinfo=UTC)
    return -(-since_epoch // timedelta(milliseconds=1))


def _matches(trace: Trace, query: TraceQuery) -> bool:
    if query.run_id is not None and trace.run_id != query.run_id:
        return False
    if query.session_id is not None and trace.session_id != query.session_id:
        return False
    if query.user_id is not None and trace.user_id != query.user_id:
        return False
    if query.model is not None and trace.model != query.model:
        return False
    if query.trace_ids and trace.id not in query.trace_ids:
        return False
    if query.states and trace.state not in query.states:
        return False
    if any(trace.tags.get(key) != value for key, value in query.tags.items()):
        return False
    if query.text and query.text.lower() not in f"{trace.name}\n{trace.request}\n{trace.response}".lower():
        return False
    when = trace.request_time
    if query.since is not None and (when is None or when < query.since):
        return False
    return not (query.until is not None and (when is None or when >= query.until))


def _scorer(found: Any) -> ScorerVersion:
    payload = json.loads(found.to_proto().serialized_scorer)  # the stored JSON, unparsed by MLflow
    stored = payload.get("martian_scorer")
    if stored is None:
        return ScorerVersion(name=found.scorer_name, kind="llm_judge", type="mlflow", version=str(found.scorer_version))
    return ScorerVersion.model_validate({**stored, "version": str(found.scorer_version)})


def _prompt(found: Any) -> Prompt:
    tags = dict(found.tags or {})
    return Prompt(
        name=found.name,
        description=found.description or "",
        created_at=_from_ms(found.creation_timestamp),
        created_by=tags.get(PROMPT_CREATED_BY),
    )


def _prompt_version(found: Any) -> PromptVersion:
    tags = dict(found.tags or {})
    return PromptVersion(
        name=found.name,
        version=int(found.version),
        text=found.template,
        digest=prompt_digest(found.template),
        message=found.commit_message or "",
        created_at=_from_ms(found.creation_timestamp),
        created_by=tags.get(PROMPT_AUTHOR),
    )


def _label_input(question: ReviewQuestion) -> Any:
    if question.kind == "boolean":
        return InputPassFail(positive_label="yes", negative_label="no")
    if question.kind == "numeric":
        return InputNumeric(min_value=question.minimum, max_value=question.maximum)
    if question.kind == "categorical":
        return InputCategorical(options=list(question.options))
    return InputText()


def _question(schema: Any) -> ReviewQuestion:
    found = schema.input
    if isinstance(found, InputNumeric):
        return ReviewQuestion(
            name=schema.name,
            kind="numeric",
            instruction=schema.instruction or "",
            minimum=found.min_value if found.min_value is not None else 0,
            maximum=found.max_value if found.max_value is not None else 1,
        )
    if isinstance(found, InputCategorical):
        return ReviewQuestion(
            name=schema.name, kind="categorical", instruction=schema.instruction or "", options=list(found.options)
        )
    kind: Literal["boolean", "text"] = "boolean" if isinstance(found, InputPassFail) else "text"
    return ReviewQuestion(name=schema.name, kind=kind, instruction=schema.instruction or "")
