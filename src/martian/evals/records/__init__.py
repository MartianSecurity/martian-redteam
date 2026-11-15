"""The evals core's own records: what MLflow does not keep.

MLflow (through ``martian.evals.store``) keeps datasets, traces, assessments, registered judges
and review queues. The evals core also needs runs with their lifecycle, case results, the scores
it gates on, scoring jobs, the human review chain, versioned suites, editable dataset drafts, the
Traces page's saved views, and each judge version's calibrations and their re-checks.
``RecordStore`` is that interface, and ``SqlRecordStore`` keeps them in SQLite or Postgres with
one migration history (``martian.evals.records.sql``). Projects live in MLflow, as experiments.
CI gate sessions stay with the server's CI gate.

Every method takes the request's principal first, and the store keeps each organisation's
records apart. A refused request raises ``martian.evals.store.StoreError``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    ScorerVersion,
    ScoringJob,
    Suite,
    Value,
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


class LabelledItem(BaseModel):
    """One labelled item a calibration rests on: a case result (``run_id`` and ``case_id``) or a
    trace of live traffic (``trace_id``), the label people gave it, and the judge's verdict the
    calibration compared with that label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Identifier | None = None
    case_id: Identifier | None = None
    trace_id: Identifier | None = None
    label: Value
    verdict: Value

    @model_validator(mode="after")
    def _a_case_result_or_a_trace(self) -> LabelledItem:
        case = self.run_id is not None and self.case_id is not None
        if case == (self.trace_id is not None) or (self.run_id is None) != (self.case_id is None):
            raise ValueError("a labelled item is a case result (run_id and case_id) or a trace (trace_id)")
        return self


class JudgeCalibration(BaseModel):
    """A judge version's agreement with people's labels, saved as its calibration
    (:mod:`martian.evals.calibration`). The core's own record, so it is not part of the shared
    model.

    ``report`` is the version's item of the agreement report when it was saved, as
    ``GET /v1/evals/judge-agreement`` gives it, with ``labelled`` the items it rests on, and
    ``traces_examined`` and ``traces_truncated`` what the report read. ``items`` are those labelled
    items, each with its label and the verdict compared with it, so that a re-check can grade
    exactly them again. ``model`` and ``endpoint`` are the model the judge version ran on then: its
    own, or the default judge model when it names none. For a judge graded by a panel, ``panel`` is
    its members instead, and the calibration is the panel's, as one judge: its verdicts are the
    panel's. ``definition`` is the digest of what the version meant then: its prompt or rubric and
    everything else that defines it but its model (``martian.evals.calibration.judge_definition``).
    ``recheck_of`` is the calibration whose labelled items a re-check graded again to make this
    one. A calibration is never changed: a newer one of the same judge version takes its place,
    and a score names the one that applied when it was made."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    project_id: Identifier
    judge: Identifier
    version: str = Field(min_length=1, max_length=40)
    model: str | None = None
    endpoint: str | None = None
    panel: list[PanelMember] = Field(default_factory=list)
    definition: str | None = None
    labelled: int = Field(ge=0)
    items: list[LabelledItem] = Field(default_factory=list)
    traces_examined: int = Field(default=0, ge=0)
    traces_truncated: bool = False
    report: dict[str, Any] = Field(default_factory=dict)
    recheck_of: Identifier | None = None
    created_by: Identifier | None = None
    created_at: datetime | None = None


RecheckState = Literal["queued", "running", "completed", "failed"]


class CalibrationRecheck(BaseModel):
    """Grading a calibration's labelled items again with its judge version as the version runs now,
    to measure its agreement again (:mod:`martian.evals.calibration`). The core's own record.

    ``calibration_id`` is the calibration whose items are graded, and ``scorer`` the judge version
    pinned, when the re-check was asked for, to the model it runs on now (or its panel's members),
    with ``definition`` the digest of its definition then. The worker grades each item (``total``):
    ``graded`` got a verdict, ``errors`` did not. A re-check that measures the version saves a new
    calibration, ``result_id``, and is ``completed``; one that cannot, such as one with too few
    verdicts, is ``failed`` with the reason in ``error``, and the calibration stays as it was."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    project_id: Identifier
    judge: Identifier
    version: str = Field(min_length=1, max_length=40)
    calibration_id: Identifier
    scorer: ScorerVersion
    definition: str
    state: RecheckState
    total: int = Field(default=0, ge=0)
    graded: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    error: str = ""
    result_id: Identifier | None = None
    requested_by: Identifier | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


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

    # Re-checks of calibrations: queued, then moved on by the worker.
    async def create_recheck(
        self, principal: Principal, project_id: str, recheck: CalibrationRecheck
    ) -> CalibrationRecheck:
        """Record a re-check. Creating the same id again returns the stored one."""
        ...

    async def get_recheck(
        self, principal: Principal, project_id: str, recheck_id: str
    ) -> CalibrationRecheck | None: ...

    async def update_recheck(
        self,
        principal: Principal,
        project_id: str,
        recheck: CalibrationRecheck,
        *,
        expected_state: RecheckState | None = None,
    ) -> CalibrationRecheck:
        """Replace the stored re-check. With ``expected_state``, only while it is still in that
        state, so two workers cannot both move it on."""
        ...

    async def list_rechecks(
        self, principal: Principal, project_id: str, *, judge: str | None = None
    ) -> list[CalibrationRecheck]:
        """Every re-check of the project, or of one judge's versions, newest first."""
        ...


RECORD_METHODS = frozenset(
    name for name in vars(RecordStore) if not name.startswith("_") and callable(getattr(RecordStore, name))
)

__all__ = [
    "RECORD_METHODS",
    "CalibrationRecheck",
    "Event",
    "EventBus",
    "EventsLost",
    "InProcessEvents",
    "JudgeCalibration",
    "LabelledItem",
    "RecheckState",
    "RecordStore",
    "SavedView",
    "Subscription",
]
