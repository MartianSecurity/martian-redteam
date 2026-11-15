"""The evals core's own records: what MLflow does not keep.

MLflow (through ``martian.evals.store``) keeps datasets, traces, assessments, registered judges
and review queues. The evals core also needs runs with their lifecycle, case results, the scores
it gates on, scoring jobs, the human review chain, versioned suites, editable dataset drafts, the
Traces page's saved views, and the monitors that score a project's live traces with what each has
scored.
``RecordStore`` is that interface, and ``SqlRecordStore`` keeps them in SQLite or Postgres with
one migration history (``martian.evals.records.sql``), which also creates the worker's queue
(``martian.evals.records.queue``). Projects live in MLflow, as experiments.
CI gate sessions stay with the server's CI gate.

Every method takes the request's principal first, and the store keeps each organisation's
records apart. A refused request raises ``martian.evals.store.StoreError``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from martian.evals.model import (
    Case,
    CaseResult,
    DatasetDraft,
    Identifier,
    Page,
    Review,
    Run,
    RunState,
    Score,
    ScorerVersion,
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


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MonitorFilter(_Record):
    """Which of a project's new traces a monitor considers: the filters of the trace search
    (``POST /v1/evals/traces/search``), with the same names, but its time range, order and paging,
    which the monitor sets itself. A trace still in progress is never scored, so ``state`` is
    ``OK``, ``ERROR`` or either."""

    q: str = Field(default="", max_length=200)
    state: Literal["", "OK", "ERROR"] = ""
    session_id: str = Field(default="", max_length=200)
    user_id: str = Field(default="", max_length=200)
    min_latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    assessment: str = Field(default="", max_length=80, pattern=r"^$|^[a-zA-Z][a-zA-Z0-9_.-]*$")
    assessment_value: Literal["", "true", "false"] = ""

    @model_validator(mode="after")
    def assessment_with_value(self) -> MonitorFilter:
        if bool(self.assessment) != bool(self.assessment_value):
            raise ValueError("Choose an assessment name and value together")
        return self


class Monitor(_Record):
    """Scorers attached to a project's live traffic (MorningHQ/martian-notes#196): judges and
    deterministic checks that the worker runs on a sample of the project's new traces, storing each
    score on its trace. The core's own record: no suite file, results file or server table
    describes it, so it is not part of the shared model.

    ``scorers`` are pinned when the monitor is created, each judge to its model, as a scoring job's
    are. ``sampling_rate`` is the share of matching traces it scores, chosen by trace id
    (:func:`martian.evals.worker.monitor.sampled`). ``max_judge_tokens_per_day`` caps its judges'
    tokens per UTC day; ``None`` is no cap. It scores traces whose request time is at or after
    ``active_since``: when it was created, or last resumed. Every change raises ``revision``."""

    id: Identifier
    project_id: Identifier
    name: str = Field(min_length=1, max_length=120)
    scorers: list[ScorerVersion] = Field(min_length=1)
    sampling_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    filter: MonitorFilter = MonitorFilter()
    max_judge_tokens_per_day: int | None = Field(default=None, ge=1)
    state: Literal["active", "paused"] = "active"
    revision: int = Field(default=1, ge=1)
    active_since: datetime | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MonitorProgress(_Record):
    """How far a monitor has read its project's traces, written only by the worker.

    ``watermark``: every trace whose request time is before it has been examined. ``day`` is the
    UTC day ``judge_tokens`` counts, and ``capped`` what the daily cap stopped, while it does
    (``limit``, ``value``, ``used``, ``until``, ``message``). ``caught_up`` is False when the last
    pass stopped at its read bound with more traces to read. ``error`` is why the last pass failed,
    or empty."""

    watermark: datetime | None = None
    day: date | None = None
    judge_tokens: int = Field(default=0, ge=0)
    examined: int = Field(default=0, ge=0)
    sampled: int = Field(default=0, ge=0)
    scored: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    capped: dict[str, Any] | None = None
    caught_up: bool = True
    error: str = ""
    last_pass_at: datetime | None = None


class MonitoredTrace(_Record):
    """One trace a monitor took for scoring: ``scoring`` from when a pass takes it until its scores
    are stored, then ``scored``, with the scores it stored on the trace and the ones without a
    verdict. ``pass_id`` is the pass that holds it. ``tokens`` are its judges' tokens, or None when
    a judge reported none."""

    monitor_id: Identifier
    trace_id: Identifier
    state: Literal["scoring", "scored"]
    pass_id: str = Field(min_length=1, max_length=64)
    tokens: int | None = Field(default=0, ge=0)
    scores: list[Score] = Field(default_factory=list)
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

    # Monitors: scorers on a project's live traces, and what each has scored.
    async def create_monitor(self, principal: Principal, project_id: str, monitor: Monitor) -> Monitor:
        """Create the monitor, or return the stored one with the same id."""
        ...

    async def get_monitor(self, principal: Principal, project_id: str, monitor_id: str) -> Monitor | None: ...

    async def list_monitors(self, principal: Principal, project_id: str) -> list[Monitor]:
        """By name."""
        ...

    async def update_monitor(
        self, principal: Principal, project_id: str, monitor: Monitor, *, expected_revision: int
    ) -> Monitor:
        """Replace the stored monitor while it is at ``expected_revision``, and raise the revision.
        Its progress is left as it is."""
        ...

    async def delete_monitor(self, principal: Principal, project_id: str, monitor_id: str) -> None:
        """Remove the monitor, its progress and its record of the traces it scored. The scores it
        stored on traces stay there."""
        ...

    async def monitor_progress(self, principal: Principal, project_id: str, monitor_id: str) -> MonitorProgress | None:
        """The monitor's progress, or None for a monitor that does not exist."""
        ...

    async def set_monitor_progress(
        self, principal: Principal, project_id: str, monitor_id: str, progress: MonitorProgress
    ) -> None:
        """Replace the monitor's progress. Nothing happens once the monitor is gone."""
        ...

    async def claim_monitored_trace(
        self, principal: Principal, project_id: str, monitor_id: str, trace_id: str, *, pass_id: str
    ) -> bool:
        """Take the trace for scoring by the monitor's pass ``pass_id``, in one transaction: True
        when the monitor is active and has not scored the trace, including a trace an earlier pass
        took and did not finish, which this pass takes over. False once the trace is scored, or
        while the monitor is paused or gone."""
        ...

    async def finish_monitored_trace(self, principal: Principal, project_id: str, trace: MonitoredTrace) -> None:
        """Record the trace as the monitor's pass left it, ``scored`` with its scores."""
        ...

    async def list_monitored_traces(
        self, principal: Principal, project_id: str, monitor_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> Page[MonitoredTrace]:
        """The traces the monitor took, most recently taken first."""
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
    "Monitor",
    "MonitorFilter",
    "MonitorProgress",
    "MonitoredTrace",
    "RecordStore",
    "SavedView",
    "Subscription",
]
