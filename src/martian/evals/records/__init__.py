"""The evals core's own records: what MLflow does not keep.

MLflow (through ``martian.evals.store``) keeps datasets, traces, assessments, registered judges
and review queues. The evals core also needs runs with their lifecycle, case results, the scores
it gates on, scoring jobs, the human review chain, versioned suites and editable dataset drafts.
``RecordStore`` is that interface, and ``SqlRecordStore`` keeps them in SQLite or Postgres with
one migration history (``martian.evals.records.sql``). Projects live in MLflow, as experiments.
CI gate sessions stay with the server's CI gate.

Every method takes the request's principal first, and the store keeps each organisation's
records apart. A refused request raises ``martian.evals.store.StoreError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from martian.evals.model import (
    Case,
    CaseResult,
    DatasetDraft,
    Page,
    Review,
    Run,
    RunState,
    Score,
    ScoringJob,
    Suite,
)
from martian.evals.records.events import Event, EventBus, EventsLost, InProcessEvents, Subscription

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal


@runtime_checkable
class RecordStore(Protocol):
    events: EventBus

    # Runs.
    async def create_run(self, principal: Principal, run: Run) -> Run:
        """Record a run. Creating the same id again returns the stored run."""
        ...

    async def get_run(self, principal: Principal, project_id: str, run_id: str) -> Run | None: ...

    async def list_runs(
        self,
        principal: Principal,
        project_id: str,
        *,
        states: list[RunState] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Run]:
        """Newest first."""
        ...

    async def update_run(self, principal: Principal, run: Run, *, expected_state: RunState | None = None) -> Run:
        """Replace the stored run. With ``expected_state``, only while the run is still in that state,
        so two workers cannot both move it on."""
        ...

    # Case results and the scores the core gates on.
    async def put_results(self, principal: Principal, project_id: str, results: list[CaseResult]) -> None:
        """Store case results of existing runs, replacing earlier ones for the same case."""
        ...

    async def list_results(
        self, principal: Principal, project_id: str, run_id: str, *, offset: int = 0, limit: int = 100
    ) -> Page[CaseResult]:
        """In case order."""
        ...

    async def put_scores(self, principal: Principal, project_id: str, scores: list[Score]) -> None:
        """Store scores of a run's cases. A score is replaced only by one from the same scorer and
        scoring job; a scoring job's scores never replace the run's own."""
        ...

    async def list_scores(
        self, principal: Principal, project_id: str, run_id: str, *, case_id: str | None = None
    ) -> list[Score]: ...

    # Scoring jobs.
    async def create_scoring_job(self, principal: Principal, project_id: str, job: ScoringJob) -> ScoringJob: ...

    async def update_scoring_job(
        self, principal: Principal, project_id: str, job: ScoringJob, *, expected_state: RunState | None = None
    ) -> ScoringJob: ...

    async def list_scoring_jobs(self, principal: Principal, project_id: str, run_id: str) -> list[ScoringJob]: ...

    # Human reviews: append-only chains.
    async def add_review(self, principal: Principal, project_id: str, review: Review) -> Review:
        """Add a review. One that names ``previous_id`` replaces that review, which must be the
        latest of its chain; a review is replaced at most once."""
        ...

    async def list_reviews(
        self,
        principal: Principal,
        project_id: str,
        run_id: str,
        *,
        case_id: str | None = None,
        current_only: bool = True,
    ) -> list[Review]:
        """With ``current_only``, the latest review of each chain."""
        ...

    # Suites: every change is a new version.
    async def save_suite(self, principal: Principal, project_id: str, suite: Suite) -> Suite:
        """Store the suite as its next version, or return the latest version if it has the same
        digest. The version is assigned."""
        ...

    async def get_suite(
        self, principal: Principal, project_id: str, suite_id: str, *, version: int | None = None
    ) -> Suite | None:
        """The latest version, or the one asked for."""
        ...

    async def list_suites(self, principal: Principal, project_id: str) -> list[Suite]:
        """The latest version of each suite."""
        ...

    # Dataset drafts: editable until published.
    async def create_draft(self, principal: Principal, project_id: str, draft: DatasetDraft) -> DatasetDraft: ...

    async def get_draft(self, principal: Principal, project_id: str, draft_id: str) -> DatasetDraft | None: ...

    async def list_drafts(self, principal: Principal, project_id: str) -> list[DatasetDraft]: ...

    async def put_draft_cases(
        self, principal: Principal, project_id: str, draft_id: str, cases: list[Case], *, expected_revision: int
    ) -> DatasetDraft:
        """Add or replace cases, while the draft is at ``expected_revision``; raises the revision."""
        ...

    async def delete_draft_cases(
        self, principal: Principal, project_id: str, draft_id: str, case_ids: list[str], *, expected_revision: int
    ) -> DatasetDraft: ...

    async def draft_cases(
        self, principal: Principal, project_id: str, draft_id: str, *, offset: int = 0, limit: int = 100
    ) -> Page[Case]:
        """In the order the cases were added."""
        ...

    async def publish_draft(
        self, principal: Principal, project_id: str, draft_id: str, *, expected_revision: int, digest: str
    ) -> DatasetDraft:
        """Mark the draft published as the dataset version with this digest; it takes no more edits."""
        ...


RECORD_METHODS = frozenset(
    name for name in vars(RecordStore) if not name.startswith("_") and callable(getattr(RecordStore, name))
)

__all__ = [
    "RECORD_METHODS",
    "Event",
    "EventBus",
    "EventsLost",
    "InProcessEvents",
    "RecordStore",
    "Subscription",
]
