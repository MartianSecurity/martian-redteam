"""The store interface the evals core reads and writes through.

The core never talks to MLflow or a database directly: an edition gives it an ``EvalStore``. The
local edition binds ``mlflow-skinny`` in-process (MorningHQ/martian-notes#106) and the server
binds its MLflow server over REST (#105). ``MemoryStore`` is the reference implementation, and
``tests/evals/store_contract.py`` is the behaviour every binding must pass.

Every method takes the request's principal first. A binding keeps each organisation's objects
apart (one MLflow workspace per organisation), and the single local user has one space of its
own. Every method takes and returns the shared eval data model's objects, never MLflow's.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

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
    Trace,
    TraceQuery,
)

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

StoreState = Literal["not_configured", "pending", "provisioning", "ready", "failed", "needs_review"]

REQUEST_PREVIEW_TRUNCATED = "martian.request_preview_truncated"
"""The trace metadata key that is ``"true"`` when a binding's search gives a shortened preview of
the trace's request instead of the whole; ``get_trace`` gives it whole. Absent otherwise."""
RESPONSE_PREVIEW_TRUNCATED = "martian.response_preview_truncated"
"""The same for the trace's response."""


class StoreStatus(BaseModel):
    """Whether the store can take evaluation work for a principal. The same shape as the server's
    ``GET /v1/evals/status`` (``EvalsStatusOut`` in the evals API contract)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: StoreState
    ready: bool
    error_code: str | None = None
    updated_at: datetime | None = None
    provider: Literal["mlflow"] | None = None
    admission_paused: bool = False
    routing_epoch: int = 0


class StoreError(Exception):
    """A request the store refuses. ``kind`` is what an API maps to a status code."""

    def __init__(self, kind: Literal["not_found", "conflict", "invalid"], detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def check_session_search(query: TraceQuery, max_traces: int) -> None:
    """Refuse a session search no binding can answer: one that continues from a cursor, or reads
    more traces than a session search may."""
    if query.cursor is not None:
        raise StoreError("invalid", "a session search starts from the first trace; leave out the cursor")
    if not 1 <= max_traces <= SESSION_TRACE_LIMIT:
        raise StoreError("invalid", f"a session search reads between 1 and {SESSION_TRACE_LIMIT} traces")


@runtime_checkable
class EvalStore(Protocol):
    async def status(self, principal: Principal) -> StoreStatus:
        """The store's readiness for this principal's organisation, or for the single local user."""
        ...

    # Projects. In MLflow, one experiment each.
    async def create_project(self, principal: Principal, project: Project) -> Project:
        """Create the project, or return the existing one with the same id. Fills ``store``."""
        ...

    async def get_project(self, principal: Principal, project_id: str) -> Project | None: ...

    async def list_projects(self, principal: Principal) -> list[Project]: ...

    async def archive_project(self, principal: Principal, project_id: str) -> Project: ...

    # Datasets. Immutable: a new version is a new dataset.
    async def create_dataset(
        self, principal: Principal, project_id: str, name: str, cases: list[Case]
    ) -> DatasetVersion:
        """Store the cases as one dataset version; the digest is computed over them. Creating the
        same name with the same cases again returns the existing version."""
        ...

    async def list_datasets(self, principal: Principal, project_id: str) -> list[DatasetVersion]: ...

    async def get_dataset(self, principal: Principal, project_id: str, dataset_id: str) -> DatasetVersion | None: ...

    async def find_dataset(
        self, principal: Principal, project_id: str, name: str, *, digest: str | None = None
    ) -> DatasetVersion | None:
        """The newest version with this name, or the one with this digest."""
        ...

    async def read_cases(
        self, principal: Principal, project_id: str, dataset_id: str, *, offset: int = 0, limit: int = 100
    ) -> Page[Case]: ...

    # Run telemetry. A run's verdicts are case results and scores; this is its record in the store.
    async def start_run(self, principal: Principal, run: Run) -> Run:
        """Record that a run started, linked to its dataset. Starting the same run id again returns
        the existing record. Fills ``store``."""
        ...

    async def get_run(self, principal: Principal, project_id: str, run_id: str) -> Run | None: ...

    async def finish_run(
        self,
        principal: Principal,
        project_id: str,
        run_id: str,
        *,
        state: RunState,
        counts: Counts,
    ) -> Run: ...

    # Traces.
    async def record_trace(self, principal: Principal, trace: Trace) -> Trace:
        """Store a trace (with its spans), for a case result or for live traffic."""
        ...

    async def search_traces(self, principal: Principal, query: TraceQuery) -> Page[Trace]:
        """Traces without their spans. A page may hold fewer than ``limit`` traces, or none, and
        still have a ``next_cursor``: only a ``next_cursor`` of ``None`` ends the search. A binding
        may give a preview of a long request or response, and says so in the trace's metadata
        (``REQUEST_PREVIEW_TRUNCATED``)."""
        ...

    async def search_sessions(
        self, principal: Principal, query: TraceQuery, *, max_traces: int = SESSION_TRACE_LIMIT
    ) -> SessionSearch:
        """The sessions among the first ``max_traces`` traces ``search_traces`` would return for
        the query, grouped by ``Trace.session_id`` (``sessions_from_traces``). The query's
        ``limit`` does not apply; a ``cursor`` is refused as ``invalid``, since a session search
        starts from the first trace. A session's turns are ``search_traces`` with its id."""
        ...

    async def get_trace(self, principal: Principal, project_id: str, trace_id: str) -> Trace | None:
        """One trace with its spans and scores, and its request and response whole."""
        ...

    async def set_trace_tag(
        self, principal: Principal, project_id: str, trace_id: str, key: str, value: str
    ) -> None: ...

    async def link_traces(self, principal: Principal, project_id: str, run_id: str, trace_ids: list[str]) -> None:
        """Make these traces part of the run."""
        ...

    # Scores: judge, check and human verdicts on a trace.
    async def log_score(self, principal: Principal, project_id: str, trace_id: str, score: Score) -> Score:
        """Fills ``store.assessment_id``."""
        ...

    async def list_scores(self, principal: Principal, project_id: str, trace_id: str) -> list[Score]: ...

    # Registered scorers (judges), versioned by name.
    async def register_scorer(self, principal: Principal, project_id: str, scorer: ScorerVersion) -> ScorerVersion:
        """Register a new version of the scorer's name; the version is assigned."""
        ...

    async def list_scorers(self, principal: Principal, project_id: str) -> list[ScorerVersion]:
        """The newest version of each registered name."""
        ...

    async def scorer_versions(self, principal: Principal, project_id: str, name: str) -> list[ScorerVersion]: ...

    # Review questions and review queues.
    async def save_question(self, principal: Principal, project_id: str, question: ReviewQuestion) -> ReviewQuestion:
        """Create the question, or return the existing one if it is identical."""
        ...

    async def list_questions(self, principal: Principal, project_id: str) -> list[ReviewQuestion]: ...

    async def create_queue(
        self, principal: Principal, project_id: str, queue: ReviewQueue, trace_ids: list[str]
    ) -> ReviewQueue:
        """Create the queue with one pending item per case: ``trace_ids[i]`` is the trace of
        ``queue.case_ids[i]``. Fills ``store``."""
        ...

    async def get_queue(self, principal: Principal, project_id: str, queue_id: str) -> ReviewQueue | None: ...

    async def queue_items(self, principal: Principal, project_id: str, queue_id: str) -> list[QueueItem]: ...

    async def set_queue_item(
        self,
        principal: Principal,
        project_id: str,
        queue_id: str,
        trace_id: str,
        status: Literal["pending", "completed"],
    ) -> QueueItem: ...

    # Run artifacts: exports and reports kept with a run.
    async def put_artifact(
        self, principal: Principal, project_id: str, run_id: str, name: str, data: bytes
    ) -> None: ...

    async def get_artifact(self, principal: Principal, project_id: str, run_id: str, name: str) -> bytes | None: ...

    async def list_artifacts(self, principal: Principal, project_id: str, run_id: str) -> list[str]: ...


STORE_METHODS = frozenset(
    name for name in vars(EvalStore) if not name.startswith("_") and callable(getattr(EvalStore, name))
)
"""Every method of the interface, for bindings and tests to check against."""

from martian.evals.store.memory import MemoryStore  # noqa: E402  (MemoryStore implements the interface above)

__all__ = [
    "REQUEST_PREVIEW_TRUNCATED",
    "RESPONSE_PREVIEW_TRUNCATED",
    "STORE_METHODS",
    "EvalStore",
    "MemoryStore",
    "StoreError",
    "StoreState",
    "StoreStatus",
    "check_session_search",
]
