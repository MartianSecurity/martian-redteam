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
- A trace belongs to the run its ``martian.run_id`` tag names. A trace without the tag that MLflow
  tracing recorded while a run's MLflow run was active names that MLflow run in its
  ``mlflow.sourceRun`` metadata, and belongs to that run; one recorded inside an MLflow run that is
  not a run's belongs to no run.
- A score is a feedback assessment on its trace.
- A registered scorer is an MLflow scorer whose serialized form carries the scorer.
- A review question is a label schema; a review queue is an MLflow review queue named by the
  queue id, with its definition in an experiment tag.
- Run artifacts are files in the run's artifact folder,
  ``<artifact root>/<MLflow run id>/artifacts``. The artifact root must be local, and on Postgres
  at most :data:`LONGEST_ARTIFACT_ROOT` characters long, so that folder fits MLflow's
  ``runs.artifact_uri``.

Text search over traces is applied in this process after MLflow's own filters, so a search reads
every trace those filters match. A search for a run's traces is two MLflow searches, one on the
tag and one on ``mlflow.sourceRun``. A session search reads the same traces and groups the first
of them in this process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
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
    RUN_ARTIFACT_URI_LENGTH,
    ServerWorkspaceContext,
    SpansLocation,
    TraceTagKey,
    WorkspaceSqlAlchemyStore,
    WorkspaceStore,
    load_dataset_records,
)
from martian.evals._sql import check_schema, create_schema, is_postgres, postgres_url
from martian.evals.model import (
    SESSION_TRACE_LIMIT,
    Case,
    Counts,
    DatasetVersion,
    Page,
    Project,
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
    query_keeps,
    query_order,
    sessions_from_traces,
)
from martian.evals.model import TraceState as ModelTraceState
from martian.evals.store import StoreError, StoreStatus, check_session_search

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

T = TypeVar("T")

SESSION_KEY = "mlflow.trace.session"
"""The trace metadata key MLflow tracing records a session id in (``TraceMetadataKey.TRACE_SESSION``)."""
USER_KEY = "mlflow.trace.user"
"""The trace metadata key MLflow tracing records the application's user in (``TraceMetadataKey.TRACE_USER``)."""
SOURCE_RUN_KEY = "mlflow.sourceRun"
"""The trace metadata key MLflow tracing records the active MLflow run in (``TraceMetadataKey.SOURCE_RUN``)."""
LONGEST_ARTIFACT_ROOT = RUN_ARTIFACT_URI_LENGTH - len(f"/{'0' * 32}/artifacts")
"""The longest artifact root the store takes on Postgres, 157 characters: a run's artifact folder,
the root and ``/<MLflow run id>/artifacts``, must fit in MLflow's ``runs.artifact_uri``. SQLite does
not enforce the column's length, so it takes any root."""

_MLFLOW_TRACE_ID = re.compile(r"^tr-[0-9a-f]{32}$")
_MLFLOW_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_MLFLOW_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_SAFE_FILTER_VALUE = re.compile(r"^[^'`\\]*$")
_QUESTION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
_RUN_STATUS = {
    "queued": "SCHEDULED",
    "running": "RUNNING",
    "completed": "FINISHED",
    "partial": "FINISHED",
    "failed": "FAILED",
    "cancelled": "KILLED",
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
        workspace = await asyncio.to_thread(self._workspace, principal)

        def run() -> T:
            with ServerWorkspaceContext(workspace):
                try:
                    return work()
                except MlflowException as error:
                    raise _store_error(error) from None

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
        def work() -> None:
            experiment = self._experiment(trace.project_id, writable=True)
            if trace.run_id is not None:
                self._mlflow_run(experiment.experiment_id, trace.run_id)
            mlflow_id = _mlflow_trace_id(trace.id)
            metadata = dict(trace.metadata)
            for key, value in (
                (SESSION_KEY, trace.session_id),
                (USER_KEY, trace.user_id),
                ("mlflow.trace.model", trace.model),
                (
                    "mlflow.trace.tokenUsage",
                    None if trace.tokens is None else json.dumps({"total_tokens": trace.tokens}),
                ),
                ("mlflow.trace.cost", None if trace.cost is None else json.dumps({"total_cost": trace.cost})),
                ("martian.request", trace.request),
                ("martian.response", trace.response),
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
                request_time=_ms(trace.request_time) or _ms(datetime.now(UTC)) or 0,
                state=TraceState(_TRACE_STATE[trace.state]),
                execution_duration=None if trace.latency_ms is None else int(trace.latency_ms),
                request_preview=trace.request[:1000],
                response_preview=trace.response[:1000],
                trace_metadata=metadata,
                tags=tags,
            )
            self._store.start_trace(info)
            if trace.spans:
                self._store.log_spans(experiment.experiment_id, [_mlflow_span(mlflow_id, s) for s in trace.spans])

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

    def _matching(self, query: TraceQuery) -> list[Trace]:
        """Every trace the query matches, in its order."""
        experiment = self._experiment(query.project_id)
        filters = []
        for key, value in ((SESSION_KEY, query.session_id), (USER_KEY, query.user_id)):
            if value is not None and _SAFE_FILTER_VALUE.match(value):
                filters.append(f"metadata.`{key}` = '{value}'")
        for key, value in query.tags.items():
            if _SAFE_FILTER_VALUE.match(key) and _SAFE_FILTER_VALUE.match(value):
                filters.append(f"tags.`{key}` = '{value}'")
        if query.since is not None:
            filters.append(f"trace.timestamp_ms >= {_ms(query.since)}")
        if query.until is not None:
            filters.append(f"trace.timestamp_ms < {_ms(query.until)}")
        searches = [filters]
        if query.run_id is not None and _SAFE_FILTER_VALUE.match(query.run_id):
            # A run's traces are the ones tagged with it and the ones MLflow tracing recorded in
            # its MLflow run; MLflow's filters have no OR, so each is a search of its own.
            searches = [[*filters, f"tags.`martian.run_id` = '{query.run_id}'"]]
            if (mlflow_run := self._find_mlflow_run(experiment.experiment_id, query.run_id)) is not None:
                searches.append([*filters, f"metadata.`{SOURCE_RUN_KEY}` = '{mlflow_run.info.run_id}'"])
        infos: dict[str, TraceInfo] = {}
        for search in searches:
            token = None
            while True:
                page, token = self._store.search_traces(
                    locations=[experiment.experiment_id],
                    filter_string=" AND ".join(search) or None,
                    max_results=500,
                    page_token=token,
                )
                infos.update((info.trace_id, info) for info in page)
                if not token:
                    break
        runs = self._run_ids(experiment.experiment_id, list(infos.values()))
        found = [
            t
            for t in (_trace(info, query.project_id, runs=runs) for info in infos.values())
            if _matches(t, query) and query_keeps(t, query)
        ]
        return query_order(found, query)

    async def search_traces(self, principal: Principal, query: TraceQuery) -> Page[Trace]:
        def work() -> Page[Trace]:
            found = self._matching(query)
            start = int(query.cursor) if query.cursor and query.cursor.isdigit() else 0
            chunk = found[start : start + query.limit]
            more = start + query.limit < len(found)
            return Page[Trace](items=chunk, next_cursor=str(start + query.limit) if more else None)

        return await self._call(principal, work)

    async def search_sessions(
        self, principal: Principal, query: TraceQuery, *, max_traces: int = SESSION_TRACE_LIMIT
    ) -> SessionSearch:
        check_session_search(query, max_traces)

        def work() -> SessionSearch:
            found = self._matching(query)
            return sessions_from_traces(found[:max_traces], has_more=len(found) > max_traces, limit=max_traces)

        return await self._call(principal, work)

    async def get_trace(self, principal: Principal, project_id: str, trace_id: str) -> Trace | None:
        def work() -> Trace | None:
            experiment = self._experiment(project_id)
            try:
                info = self._trace_info(experiment.experiment_id, trace_id)
            except StoreError:
                return None
            # A trace recorded without spans has no spans to wait for, so a partial read is complete.
            full: MlflowTrace = self._store.get_trace(info.trace_id, allow_partial=True)
            runs = self._run_ids(experiment.experiment_id, [info])
            return _trace(info, project_id, spans=[_span(s) for s in full.data.spans], runs=runs)

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


def _store_error(error: MlflowException) -> StoreError:
    code = str(getattr(error, "error_code", ""))
    if "DOES_NOT_EXIST" in code or "NOT_FOUND" in code:
        return StoreError("not_found", "not found")
    if "ALREADY_EXISTS" in code:
        return StoreError("conflict", "already exists")
    return StoreError("invalid", error.message[:300])


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


def _mlflow_span(trace_id: str, span: Span) -> MlflowSpan:
    def span_id(value: str) -> str:
        return value if _MLFLOW_SPAN_ID.match(value) else _hex(value, 16)

    attributes = {
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
        **{key: json.dumps(value, default=str) for key, value in span.attributes.items()},
    }
    code = {"ok": "STATUS_CODE_OK", "error": "STATUS_CODE_ERROR"}.get(span.status, "STATUS_CODE_UNSET")
    start = int((span.start_ms or 0) * 1_000_000)
    return MlflowSpan.from_dict(
        {
            "trace_id": trace_id,
            "span_id": span_id(span.id),
            "parent_span_id": span_id(span.parent_id) if span.parent_id else None,
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
    info: TraceInfo, project_id: str, *, spans: list[Span] | None = None, runs: dict[str, str] | None = None
) -> Trace:
    """The trace in the model. ``runs`` maps the MLflow run ids traces name in
    ``mlflow.sourceRun`` to run ids, for a trace without a ``martian.run_id`` tag."""
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
        "mlflow.trace.model",
        "mlflow.trace.tokenUsage",
        "mlflow.trace.cost",
        "martian.request",
        "martian.response",
    }
    state = str(getattr(info.state, "value", info.state))
    return Trace(
        id=trace_id,
        project_id=project_id,
        name=name,
        state=_TRACE_STATE_BACK.get(state, "unknown"),
        request_time=_from_ms(info.request_time),
        latency_ms=float(info.execution_duration) if info.execution_duration is not None else None,
        request=metadata.get("martian.request", info.request_preview or ""),
        response=metadata.get("martian.response", info.response_preview or ""),
        run_id=run_id,
        session_id=metadata.get(SESSION_KEY),
        user_id=metadata.get(USER_KEY),
        model=metadata.get("mlflow.trace.model"),
        tokens=usage.get("total_tokens") if isinstance(usage, dict) else None,
        cost=cost.get("total_cost") if isinstance(cost, dict) else None,
        tags={k: v for k, v in tags.items() if not k.startswith("mlflow.")},
        metadata={k: v for k, v in metadata.items() if k not in known and not k.startswith("mlflow.")},
        scores=_scores(info, project_id, trace_id),
        spans=spans or [],
    )


def trace_from_mlflow(trace: MlflowTrace, project_id: str) -> Trace:
    """A trace MLflow recorded, such as one of its evaluation harness's, in the model, with its
    spans and its feedback assessments as scores. Its ``id`` is MLflow's trace id."""
    return _trace(trace.info, project_id, spans=[_span(span) for span in trace.data.spans])


def _matches(trace: Trace, query: TraceQuery) -> bool:
    if query.run_id is not None and trace.run_id != query.run_id:
        return False
    if query.session_id is not None and trace.session_id != query.session_id:
        return False
    if query.user_id is not None and trace.user_id != query.user_id:
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
