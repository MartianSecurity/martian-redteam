"""The evals core's own records: what MLflow does not keep.

MLflow (through ``martian.evals.store``) keeps datasets, traces, assessments, registered judges
and review queues. The evals core also needs runs with their lifecycle, case results, the scores
it gates on, scoring jobs, the human review chain, versioned suites, editable dataset drafts, the
Traces page's saved views and each judge version's calibrations.
``RecordStore`` is that interface, and ``SqlRecordStore`` keeps them in SQLite or Postgres with
one migration history (``martian.evals.records.sql``), which also creates the worker's queue
(``martian.evals.records.queue``). Projects live in MLflow, as experiments.
CI gate sessions stay with the server's CI gate.

Every method takes the request's principal first, and the store keeps each organisation's
records apart. A refused request raises ``martian.evals.store.StoreError``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from martian.evals.model import (
    Case,
    CaseResult,
    DatasetDraft,
    Identifier,
    Page,
    PanelMember,
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


class SavedView(BaseModel):
    """A saved query of the Traces page and how it is shown. UI state, not evaluation content, so it
    is not part of the shared model. A ``private`` view is its owner's; an ``organization`` view is
    everyone's in the organisation. Every change raises ``revision``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    project_id: Identifier
    owner: Identifier
    name: str = Field(min_length=1, max_length=120)
    visibility: Literal["private", "organization"] = "private"
    revision: int = Field(default=1, ge=1)
    definition: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class JudgeCalibration(BaseModel):
    """A judge version's agreement with people's labels, saved as its calibration
    (:mod:`martian.evals.calibration`). The core's own record, so it is not part of the shared
    model.

    ``report`` is the version's item of the agreement report when it was saved, as
    ``GET /v1/evals/judge-agreement`` gives it, with ``labelled`` the items it rests on, and
    ``traces_examined`` and ``traces_truncated`` what the report read. ``model`` and ``endpoint``
    are the model the judge version ran on then: its own, or the default judge model when it names
    none. For a judge graded by a panel, ``panel`` is its members instead, and the calibration is
    the panel's, as one judge: its verdicts are the panel's. A calibration is never changed: a
    newer one of the same judge version takes its place, and a score names the one that applied
    when it was made."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    project_id: Identifier
    judge: Identifier
    version: str = Field(min_length=1, max_length=40)
    model: str | None = None
    endpoint: str | None = None
    panel: list[PanelMember] = Field(default_factory=list)
    labelled: int = Field(ge=0)
    traces_examined: int = Field(default=0, ge=0)
    traces_truncated: bool = False
    report: dict[str, Any] = Field(default_factory=dict)
    created_by: Identifier | None = None
    created_at: datetime | None = None


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

    async def count_active_runs(self, principal: Principal, *, up_to: int | None = None) -> int:
        """How many runs of the organisation, in every project, are ``queued`` or ``running``, in
        one query. With ``up_to``, it stops counting there and reads at most that many rows."""
        ...

    async def update_run(self, principal: Principal, run: Run, *, expected_state: RunState | None = None) -> Run:
        """Replace the stored run. With ``expected_state``, only while the run is still in that state,
        so two workers cannot both move it on."""
        ...

    # Case results and the scores the core gates on.
    async def put_results(
        self, principal: Principal, project_id: str, results: list[CaseResult], *, scores: list[Score] | None = None
    ) -> None:
        """Store case results of existing runs, replacing earlier ones for the same case, and
        ``scores`` of those cases as :meth:`put_scores` stores them, all in one transaction: a case
        has its result and its scores, or neither. A worker records each finished case so, and a
        run it takes again resumes after the cases that have a result."""
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

    async def count_active_scoring_jobs(self, principal: Principal, *, up_to: int | None = None) -> int:
        """How many scoring jobs of the organisation's completed runs, in every project, are
        ``queued`` or ``running``, in one query. With ``up_to``, it stops counting there and reads
        at most that many rows."""
        ...

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

    async def edit_draft(
        self,
        principal: Principal,
        project_id: str,
        draft_id: str,
        *,
        expected_revision: int,
        name: str | None = None,
        description: str | None = None,
    ) -> DatasetDraft:
        """Rename or describe the draft while it is at ``expected_revision``; raises the revision.
        ``None`` leaves a field as it is."""
        ...

    async def delete_draft(self, principal: Principal, project_id: str, draft_id: str) -> None:
        """Remove the draft and its cases. A published draft can be removed; its dataset stays."""
        ...

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

    # Saved trace views: every change raises the revision.
    async def save_view(
        self, principal: Principal, project_id: str, view: SavedView, *, expected_revision: int | None = None
    ) -> SavedView:
        """Without ``expected_revision``, create the view, or return the stored one with the same id.
        With it, replace the stored view while it is at that revision, and raise the revision."""
        ...

    async def get_view(self, principal: Principal, project_id: str, view_id: str) -> SavedView | None: ...

    async def list_views(self, principal: Principal, project_id: str) -> list[SavedView]:
        """By name."""
        ...

    async def delete_view(self, principal: Principal, project_id: str, view_id: str, *, expected_revision: int) -> None:
        """Remove the view while it is at ``expected_revision``."""
        ...

    # Judge calibrations: every one is kept, and the newest of a judge version is its calibration.
    async def save_calibration(
        self, principal: Principal, project_id: str, calibration: JudgeCalibration
    ) -> JudgeCalibration:
        """Record a calibration. Saving the same id again returns the stored one."""
        ...

    async def get_calibration(
        self, principal: Principal, project_id: str, calibration_id: str
    ) -> JudgeCalibration | None: ...

    async def list_calibrations(
        self, principal: Principal, project_id: str, *, judge: str | None = None
    ) -> list[JudgeCalibration]:
        """Every calibration of the project, or of one judge's versions, newest first."""
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
    "JudgeCalibration",
    "RecordStore",
    "SavedView",
    "Subscription",
]
