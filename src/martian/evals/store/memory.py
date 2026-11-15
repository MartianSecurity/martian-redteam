"""The reference store: every interface method, in memory, with each organisation kept apart.

For tests and for trying the core. Nothing persists past the process.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

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
    SessionSearch,
    StoreRef,
    Trace,
    TraceQuery,
    digest,
    query_keeps,
    query_order,
    sessions_from_traces,
)
from martian.evals.store import StoreError, StoreStatus, check_session_search, cursor_offset
from martian.evals.trace_conventions import map_trace

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

_LOCAL = "~local"


def _space(principal: Principal) -> str:
    """The key that separates organisations; the single local user has a space of its own."""
    return principal.organisation_id or _LOCAL


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _page[T](items: list[T], cursor: str | None, limit: int) -> Page[T]:
    start = cursor_offset(cursor)
    chunk = items[start : start + limit]
    return Page[T](items=chunk, next_cursor=str(start + limit) if start + limit < len(items) else None)


class MemoryStore:
    """An in-memory ``EvalStore``. ``callers`` records the principal of every ``status`` call."""

    def __init__(self, status: StoreStatus | None = None) -> None:
        self._status = status or StoreStatus(status="ready", ready=True)
        self.callers: list[Principal] = []
        self._projects: dict[tuple[str, str], Project] = {}
        self._datasets: dict[tuple[str, str, str], tuple[DatasetVersion, list[Case]]] = {}
        self._runs: dict[tuple[str, str, str], Run] = {}
        self._traces: dict[tuple[str, str, str], Trace] = {}
        self._scores: dict[tuple[str, str, str], list[Score]] = {}
        self._scorers: dict[tuple[str, str, str], list[ScorerVersion]] = {}
        self._questions: dict[tuple[str, str, str], ReviewQuestion] = {}
        self._queues: dict[tuple[str, str, str], ReviewQueue] = {}
        self._items: dict[tuple[str, str, str], dict[str, QueueItem]] = {}
        self._artifacts: dict[tuple[str, str, str], dict[str, bytes]] = {}

    async def status(self, principal: Principal) -> StoreStatus:
        self.callers.append(principal)
        return self._status

    # --- helpers -----------------------------------------------------------------------------

    def _project(self, principal: Principal, project_id: str, *, writable: bool = False) -> Project:
        project = self._projects.get((_space(principal), project_id))
        if project is None:
            raise StoreError("not_found", "project not found")
        if writable and project.archived_at is not None:
            raise StoreError("conflict", "the project is archived")
        return project

    def _run(self, principal: Principal, project_id: str, run_id: str) -> Run:
        self._project(principal, project_id)
        run = self._runs.get((_space(principal), project_id, run_id))
        if run is None:
            raise StoreError("not_found", "run not found")
        return run

    def _trace(self, principal: Principal, project_id: str, trace_id: str) -> Trace:
        self._project(principal, project_id)
        trace = self._traces.get((_space(principal), project_id, trace_id))
        if trace is None:
            raise StoreError("not_found", "trace not found")
        return trace

    # --- projects ----------------------------------------------------------------------------

    async def create_project(self, principal: Principal, project: Project) -> Project:
        key = (_space(principal), project.id)
        if existing := self._projects.get(key):
            if existing.name != project.name:
                raise StoreError("conflict", "a project with this id has another name")
            return existing
        saved = project.model_copy(
            update={"created_at": project.created_at or _now(), "store": StoreRef(experiment_id=project.id)}
        )
        self._projects[key] = saved
        return saved

    async def get_project(self, principal: Principal, project_id: str) -> Project | None:
        return self._projects.get((_space(principal), project_id))

    async def list_projects(self, principal: Principal) -> list[Project]:
        space = _space(principal)
        return sorted((p for (s, _), p in self._projects.items() if s == space), key=lambda p: p.name)

    async def archive_project(self, principal: Principal, project_id: str) -> Project:
        project = self._project(principal, project_id)
        if project.archived_at is None:
            project = project.model_copy(update={"archived_at": _now()})
            self._projects[(_space(principal), project_id)] = project
        return project

    # --- datasets ----------------------------------------------------------------------------

    async def create_dataset(
        self, principal: Principal, project_id: str, name: str, cases: list[Case]
    ) -> DatasetVersion:
        self._project(principal, project_id, writable=True)
        if not cases:
            raise StoreError("invalid", "a dataset needs at least one case")
        ids = [case.id for case in cases]
        if len(set(ids)) != len(ids):
            raise StoreError("invalid", "case ids must be unique within a dataset")
        content = digest([case.model_dump(mode="json") for case in cases])
        space = _space(principal)
        versions = [
            d for (s, p, _), (d, _c) in self._datasets.items() if s == space and p == project_id and d.name == name
        ]
        for existing in versions:
            if existing.digest == content:
                return existing
        dataset_id = _new_id("ds")
        dataset = DatasetVersion(
            id=dataset_id,
            name=name,
            version=len(versions) + 1,
            digest=content,
            case_count=len(cases),
            source="upload",
            created_at=_now(),
            created_by=principal.user_id,
            store=StoreRef(experiment_id=project_id, dataset_id=dataset_id),
        )
        self._datasets[(space, project_id, dataset_id)] = (dataset, list(cases))
        return dataset

    async def list_datasets(self, principal: Principal, project_id: str) -> list[DatasetVersion]:
        self._project(principal, project_id)
        space = _space(principal)
        found = [d for (s, p, _), (d, _c) in self._datasets.items() if s == space and p == project_id]
        return sorted(found, key=lambda d: (d.name, d.version or 0))

    async def get_dataset(self, principal: Principal, project_id: str, dataset_id: str) -> DatasetVersion | None:
        self._project(principal, project_id)
        entry = self._datasets.get((_space(principal), project_id, dataset_id))
        return entry[0] if entry else None

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
        self._project(principal, project_id)
        entry = self._datasets.get((_space(principal), project_id, dataset_id))
        if entry is None:
            raise StoreError("not_found", "dataset not found")
        if offset < 0 or not 1 <= limit <= 1000:
            raise StoreError("invalid", "offset must be 0 or more and limit between 1 and 1000")
        return _page(entry[1], str(offset), limit)

    # --- runs --------------------------------------------------------------------------------

    async def start_run(self, principal: Principal, run: Run) -> Run:
        self._project(principal, run.project_id, writable=True)
        key = (_space(principal), run.project_id, run.id)
        if existing := self._runs.get(key):
            return existing
        saved = run.model_copy(
            update={
                "started_at": run.started_at or _now(),
                "store": StoreRef(experiment_id=run.project_id, run_id=run.id),
            }
        )
        self._runs[key] = saved
        return saved

    async def get_run(self, principal: Principal, project_id: str, run_id: str) -> Run | None:
        self._project(principal, project_id)
        return self._runs.get((_space(principal), project_id, run_id))

    async def finish_run(
        self, principal: Principal, project_id: str, run_id: str, *, state: RunState, counts: Counts
    ) -> Run:
        run = self._run(principal, project_id, run_id)
        if state in ("queued", "running"):
            raise StoreError("invalid", "a finished run needs a final state")
        run = run.model_copy(update={"state": state, "counts": counts, "finished_at": run.finished_at or _now()})
        self._runs[(_space(principal), project_id, run_id)] = run
        return run

    # --- traces ------------------------------------------------------------------------------

    async def record_trace(self, principal: Principal, trace: Trace) -> Trace:
        self._project(principal, trace.project_id, writable=True)
        if trace.run_id is not None:
            self._run(principal, trace.project_id, trace.run_id)
        saved = map_trace(trace).model_copy(update={"scores": []})
        self._traces[(_space(principal), trace.project_id, trace.id)] = saved
        for score in trace.scores:
            await self.log_score(principal, trace.project_id, trace.id, score)
        return await self.get_trace(principal, trace.project_id, trace.id) or saved

    def _matching(self, principal: Principal, query: TraceQuery) -> list[Trace]:
        """Every trace the query matches, in its order, with the scores of each."""
        self._project(principal, query.project_id)
        space = _space(principal)
        found = []
        for (s, p, _), trace in self._traces.items():
            if s != space or p != query.project_id:
                continue
            if query.run_id is not None and trace.run_id != query.run_id:
                continue
            if query.session_id is not None and trace.session_id != query.session_id:
                continue
            if query.user_id is not None and trace.user_id != query.user_id:
                continue
            if query.trace_ids and trace.id not in query.trace_ids:
                continue
            if query.states and trace.state not in query.states:
                continue
            if any(trace.tags.get(key) != value for key, value in query.tags.items()):
                continue
            if query.text and query.text.lower() not in f"{trace.name}\n{trace.request}\n{trace.response}".lower():
                continue
            when = trace.request_time
            if query.since is not None and (when is None or when < query.since):
                continue
            if query.until is not None and (when is None or when >= query.until):
                continue
            found.append(trace.model_copy(update={"spans": [], "scores": self._scores.get((space, p, trace.id), [])}))
        return query_order([t for t in found if query_keeps(t, query)], query)

    async def search_traces(self, principal: Principal, query: TraceQuery) -> Page[Trace]:
        return _page(self._matching(principal, query), query.cursor, query.limit)

    async def search_sessions(
        self, principal: Principal, query: TraceQuery, *, max_traces: int = SESSION_TRACE_LIMIT
    ) -> SessionSearch:
        check_session_search(query, max_traces)
        found = self._matching(principal, query)
        return sessions_from_traces(found[:max_traces], has_more=len(found) > max_traces, limit=max_traces)

    async def get_trace(self, principal: Principal, project_id: str, trace_id: str) -> Trace | None:
        self._project(principal, project_id)
        key = (_space(principal), project_id, trace_id)
        trace = self._traces.get(key)
        return trace.model_copy(update={"scores": self._scores.get(key, [])}) if trace else None

    async def set_trace_tag(self, principal: Principal, project_id: str, trace_id: str, key: str, value: str) -> None:
        trace = self._trace(principal, project_id, trace_id)
        if not key:
            raise StoreError("invalid", "a tag needs a key")
        self._traces[(_space(principal), project_id, trace_id)] = trace.model_copy(
            update={"tags": {**trace.tags, key: value}}
        )

    async def link_traces(self, principal: Principal, project_id: str, run_id: str, trace_ids: list[str]) -> None:
        self._run(principal, project_id, run_id)
        traces = [self._trace(principal, project_id, trace_id) for trace_id in trace_ids]
        for trace in traces:
            self._traces[(_space(principal), project_id, trace.id)] = trace.model_copy(update={"run_id": run_id})

    # --- scores ------------------------------------------------------------------------------

    async def log_score(self, principal: Principal, project_id: str, trace_id: str, score: Score) -> Score:
        self._trace(principal, project_id, trace_id)
        saved = score.model_copy(
            update={
                "trace_id": trace_id,
                "created_at": score.created_at or _now(),
                "store": StoreRef(experiment_id=project_id, trace_id=trace_id, assessment_id=_new_id("a")),
            }
        )
        self._scores.setdefault((_space(principal), project_id, trace_id), []).append(saved)
        return saved

    async def list_scores(self, principal: Principal, project_id: str, trace_id: str) -> list[Score]:
        self._trace(principal, project_id, trace_id)
        return list(self._scores.get((_space(principal), project_id, trace_id), []))

    # --- scorers -----------------------------------------------------------------------------

    async def register_scorer(self, principal: Principal, project_id: str, scorer: ScorerVersion) -> ScorerVersion:
        self._project(principal, project_id, writable=True)
        versions = self._scorers.setdefault((_space(principal), project_id, scorer.name), [])
        saved = scorer.model_copy(update={"version": str(len(versions) + 1)})
        versions.append(saved)
        return saved

    async def list_scorers(self, principal: Principal, project_id: str) -> list[ScorerVersion]:
        self._project(principal, project_id)
        space = _space(principal)
        latest = [v[-1] for (s, p, _), v in self._scorers.items() if s == space and p == project_id and v]
        return sorted(latest, key=lambda scorer: scorer.name)

    async def scorer_versions(self, principal: Principal, project_id: str, name: str) -> list[ScorerVersion]:
        self._project(principal, project_id)
        return list(self._scorers.get((_space(principal), project_id, name), []))

    # --- review questions and queues --------------------------------------------------------

    async def save_question(self, principal: Principal, project_id: str, question: ReviewQuestion) -> ReviewQuestion:
        self._project(principal, project_id, writable=True)
        key = (_space(principal), project_id, question.name)
        if existing := self._questions.get(key):
            if existing != question:
                raise StoreError("conflict", "a question with this name is defined differently")
            return existing
        self._questions[key] = question
        return question

    async def list_questions(self, principal: Principal, project_id: str) -> list[ReviewQuestion]:
        self._project(principal, project_id)
        space = _space(principal)
        found = [q for (s, p, _), q in self._questions.items() if s == space and p == project_id]
        return sorted(found, key=lambda q: q.name)

    async def create_queue(
        self, principal: Principal, project_id: str, queue: ReviewQueue, trace_ids: list[str]
    ) -> ReviewQueue:
        self._project(principal, project_id, writable=True)
        if len(trace_ids) != len(queue.case_ids):
            raise StoreError("invalid", "give one trace per case in the queue")
        for trace_id in trace_ids:
            self._trace(principal, project_id, trace_id)
        key = (_space(principal), project_id, queue.id)
        if existing := self._queues.get(key):
            return existing
        saved = queue.model_copy(
            update={"created_at": queue.created_at or _now(), "store": StoreRef(experiment_id=project_id)}
        )
        self._queues[key] = saved
        self._items[key] = {
            trace_id: QueueItem(queue_id=queue.id, trace_id=trace_id, case_id=case_id)
            for case_id, trace_id in zip(queue.case_ids, trace_ids, strict=True)
        }
        return saved

    async def get_queue(self, principal: Principal, project_id: str, queue_id: str) -> ReviewQueue | None:
        self._project(principal, project_id)
        return self._queues.get((_space(principal), project_id, queue_id))

    async def queue_items(self, principal: Principal, project_id: str, queue_id: str) -> list[QueueItem]:
        self._project(principal, project_id)
        key = (_space(principal), project_id, queue_id)
        if key not in self._queues:
            raise StoreError("not_found", "queue not found")
        return list(self._items[key].values())

    async def set_queue_item(
        self,
        principal: Principal,
        project_id: str,
        queue_id: str,
        trace_id: str,
        status: Literal["pending", "completed"],
    ) -> QueueItem:
        items = {item.trace_id: item for item in await self.queue_items(principal, project_id, queue_id)}
        if trace_id not in items:
            raise StoreError("not_found", "the trace is not in this queue")
        updated = items[trace_id].model_copy(update={"status": status})
        self._items[(_space(principal), project_id, queue_id)][trace_id] = updated
        return updated

    # --- artifacts ---------------------------------------------------------------------------

    @staticmethod
    def _artifact_name(name: str) -> str:
        parts = name.split("/")
        if not name or name.startswith("/") or any(part in ("", ".", "..") for part in parts):
            raise StoreError("invalid", "an artifact name is a relative path without . or .. parts")
        return name

    async def put_artifact(self, principal: Principal, project_id: str, run_id: str, name: str, data: bytes) -> None:
        self._project(principal, project_id, writable=True)
        self._run(principal, project_id, run_id)
        files = self._artifacts.setdefault((_space(principal), project_id, run_id), {})
        files[self._artifact_name(name)] = bytes(data)

    async def get_artifact(self, principal: Principal, project_id: str, run_id: str, name: str) -> bytes | None:
        self._run(principal, project_id, run_id)
        return self._artifacts.get((_space(principal), project_id, run_id), {}).get(self._artifact_name(name))

    async def list_artifacts(self, principal: Principal, project_id: str, run_id: str) -> list[str]:
        self._run(principal, project_id, run_id)
        return sorted(self._artifacts.get((_space(principal), project_id, run_id), {}))
