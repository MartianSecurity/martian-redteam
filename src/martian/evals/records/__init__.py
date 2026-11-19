"""The evals core's own records: what MLflow does not keep.

MLflow (through ``martian.evals.store``) keeps datasets, traces, assessments, registered judges
and review queues. The evals core also needs runs with their lifecycle, case results, the scores
it gates on, scoring jobs, the human review chain, versioned suites and their audits, editable
dataset drafts, the Traces page's saved views, each judge version's calibrations and their
re-checks, the monitors that score a project's live traces with what each has scored, the
thresholds on their scores with the alerts they raise, and the schedules that run a suite against
a target with the slots each has run, and with how far each has watched the model behind its
target; also what publishing each dataset version wrote, and the outbox of store writes not yet
delivered.
``RecordStore`` is that interface, and ``SqlRecordStore`` keeps them in SQLite or Postgres with
one migration history (``martian.evals.records.sql``), which also creates the worker's queue
(``martian.evals.records.queue``). A server that admits a launched run by rules of its own gives the
store a ``RunAdmission`` (``martian.evals.records.admission``). Projects live in MLflow, as
experiments. CI gate sessions stay with the server's CI gate.

Every method takes the request's principal first, and the store keeps each organisation's
records apart. A refused request raises ``martian.evals.store.StoreError``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from martian.evals.model import (
    MODEL_NAME_LENGTH,
    Case,
    CaseResult,
    DatasetDraft,
    Digest,
    Identifier,
    Page,
    PanelMember,
    Policy,
    Review,
    Run,
    RunKind,
    RunLimits,
    RunState,
    Score,
    ScorerVersion,
    ScoringJob,
    Suite,
    Target,
    Value,
)
from martian.evals.records.admission import LAUNCHED, RunAdmission
from martian.evals.records.events import Event, EventBus, EventsLost, InProcessEvents, Subscription
from martian.evals.slots import timing

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


class SuiteAudit(BaseModel):
    """The latest audit of one saved suite version (``martian.evals.audit``): the dataset it was
    run over, whether it flagged the suite, and the whole result. The evals core's own record: the
    server keeps no audits, so the shared model has no object for one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: Identifier
    suite_version: int = Field(ge=1)
    suite_digest: Digest
    dataset_name: str = Field(min_length=1, max_length=200)
    dataset_digest: Digest
    flagged: bool
    result: dict[str, Any]
    created_at: datetime | None = None
    created_by: str | None = None


class DatasetManifest(BaseModel):
    """What publishing a dataset version wrote, kept apart from the store: the version's digest and
    each case's id and digest, in order. A run reads the version only while the store still holds
    exactly that (``martian.evals.manifests``). ``state`` is ``publishing`` from the moment publishing
    starts until the version is written and read back, then ``published``; while a publication is
    ``publishing``, new runs wait."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    project_id: Identifier
    name: str = Field(min_length=1, max_length=200)
    digest: Digest
    case_ids: list[Identifier]
    case_digests: list[Digest]
    state: Literal["publishing", "published"] = "publishing"
    dataset_id: str | None = None
    draft_id: str | None = None
    published_by: str | None = None
    started_at: datetime | None = None
    published_at: datetime | None = None


class Delivery(BaseModel):
    """A store write the record store keeps until it reaches the store (``martian.evals.outbox``):
    a score on its case's trace, or a review queue item's status. ``attempts`` and
    ``next_attempt_at`` pace the retries; ``failed`` is a write the store refused for good."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    project_id: Identifier
    kind: Literal["score", "queue_item"]
    payload: dict[str, Any]
    state: Literal["pending", "delivered", "failed"] = "pending"
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime | None = None
    error: str = ""
    created_at: datetime | None = None
    delivered_at: datetime | None = None


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


class BlindQueue(BaseModel):
    """A review queue with blind double review (:mod:`martian.evals.server.blind`): each reviewer
    answers an item without reading the other reviewers' answers to it until they have given their
    own. ``id`` is the queue's id in the store. The core's own record, kept apart from the queue in
    the store, so that every store binding keeps it: it is recorded before the queue is created and
    never changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    run_id: Identifier
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
    """Scorers attached to a project's live traffic (MartianSecurity/martian-notes#196): judges and
    deterministic checks that the worker runs on a sample of the project's new traces, storing each
    score on its trace. The core's own record: no suite file, results file or server table
    describes it, so it is not part of the shared model.

    ``scorers`` are pinned when the monitor is created, each judge to its model, as a scoring job's
    are. ``sampling_rate`` is the share of matching traces it scores, chosen by trace id
    (:func:`martian.evals.worker.monitor.sampled`). ``max_judge_tokens_per_day`` caps its judges'
    tokens per UTC day; ``None`` is no cap. It scores traces whose request time is at or after
    ``active_since``: when it was created, or last resumed. Every change raises ``revision``.

    A monitor holds no ``code`` scorer, such as a custom Python scorer a worker installs
    (:data:`~martian.evals.worker.execute.InstalledScorers`): one reads a case's inputs and
    expectations, and a production trace has a request and a response only."""

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

    @model_validator(mode="after")
    def no_custom_scorer(self) -> Monitor:
        if custom := [scorer.name for scorer in self.scorers if scorer.kind == "code"]:
            raise ValueError(
                f"A monitor cannot run the custom Python scorer {', '.join(custom)}: it reads a case's inputs "
                "and expectations, and a production trace has a request and a response only"
            )
        return self


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


class ModelWatch(_Record):
    """What makes a schedule also run its suite when the model behind its target changes
    (MartianSecurity/martian-notes#200), besides its slots.

    ``source`` is where the model is read: ``traces``, the model of the project's live traces
    (not those of eval runs), read by time as a monitor reads them; or ``runs``, the model the
    target reported on the project's runs of it (``Run.provenance["model"]``). A new model is a
    change once it has been the only one seen for ``settle_seconds``, and a change starts the
    suite at most once every ``min_interval_seconds``, so a model that flaps back and forth starts
    no storm of runs (:mod:`martian.evals.worker.schedule`)."""

    source: Literal["traces", "runs"]
    settle_seconds: float = Field(default=300, ge=0, le=7 * 86400, allow_inf_nan=False)
    min_interval_seconds: float = Field(default=3600, ge=0, le=30 * 86400, allow_inf_nan=False)


class ModelChange(_Record):
    """A change of the model behind a schedule's target, from ``previous`` to ``current``, that
    starts the schedule's suite: the run its slot ``at`` holds in the history, a slot of its own or
    the regular slot due at the same turn."""

    previous: str = Field(min_length=1, max_length=MODEL_NAME_LENGTH)
    current: str = Field(min_length=1, max_length=MODEL_NAME_LENGTH)
    at: datetime

    @property
    def note(self) -> str:
        return f"model changed from {self.previous} to {self.current}"


class ModelWatchProgress(_Record):
    """How far a schedule's model watch has read, written only by the worker.

    ``model`` is the model the schedule last ran its suite for, or the first it saw. ``candidate``
    is another model seen since, with ``candidate_since`` when it was first seen after the last
    sight of any other: a change once ``settle_seconds`` pass with nothing else seen. ``watermark``:
    every trace with an earlier request time, or every run that finished at or before it, has been
    read. ``last_change_at`` is when a change last started the suite, and ``change`` a change
    decided but not yet in the history. ``caught_up`` is False when the last read stopped at its
    bound with more to read; ``error`` is why the last read failed, or empty."""

    model: str | None = Field(default=None, max_length=MODEL_NAME_LENGTH)
    candidate: str | None = Field(default=None, max_length=MODEL_NAME_LENGTH)
    candidate_since: datetime | None = None
    watermark: datetime | None = None
    observed: int = Field(default=0, ge=0)
    last_change_at: datetime | None = None
    change: ModelChange | None = None
    caught_up: bool = True
    error: str = ""
    checked_at: datetime | None = None


class Schedule(_Record):
    """A saved suite version run against a target on a schedule (MartianSecurity/martian-notes#197): a
    regression run, admitted by the worker at each slot as a launch is admitted. The core's own
    record, so it is not part of the shared model.

    Its slots are every ``every_seconds`` or on ``cron``, in UTC (:mod:`martian.evals.slots`).
    ``suite_id`` and ``suite_version`` name the saved suite version each run is scored with, and
    ``scorers`` and ``policies`` are that version's, each judge pinned to its model when the
    schedule was created. ``dataset_id`` and ``dataset_digest`` pin the published dataset version
    every run reads, so the schedule's runs compare with each other. ``target`` is pinned again
    at each run's admission, as a launch pins it: a changed target is what a regression run is
    for. ``limits`` are each run's own (``RunLimits``), or None for the worker's. Its runs are due
    from ``active_since``: when it was created or last resumed. ``on_model_change``, when set,
    also runs the suite when the model behind the target changes (:class:`ModelWatch`). Every
    change raises ``revision``."""

    id: Identifier
    project_id: Identifier
    name: str = Field(min_length=1, max_length=120)
    every_seconds: int | None = Field(default=None, ge=1)
    cron: str | None = Field(default=None, max_length=200)
    on_model_change: ModelWatch | None = None
    suite_id: Identifier
    suite_version: int = Field(ge=1)
    suite_name: str = ""
    scorers: list[ScorerVersion] = Field(min_length=1)
    policies: list[Policy] = Field(default_factory=list)
    target: Target
    dataset_name: str = Field(min_length=1, max_length=200)
    dataset_id: Identifier
    dataset_digest: Digest | None = None
    max_cases: int = Field(ge=1)
    limits: RunLimits | None = None
    state: Literal["active", "paused"] = "active"
    revision: int = Field(default=1, ge=1)
    active_since: datetime | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def one_timing(self) -> Schedule:
        timing(every_seconds=self.every_seconds, cron=self.cron)
        return self


class ScheduleProgress(_Record):
    """Where a schedule stands, written only by the worker: ``next_slot``, when its next run is
    due, ``last_slot``, the slot it last ran or skipped, ``error``, why its last turn failed, or
    empty, and ``model_watch``, how far its model watch has read, for a schedule with
    ``on_model_change``."""

    next_slot: datetime | None = None
    last_slot: datetime | None = None
    error: str = ""
    last_turn_at: datetime | None = None
    model_watch: ModelWatchProgress | None = None


class ScheduledRun(_Record):
    """One slot of a schedule, as its turn left it: ``admitted`` with the run it started
    (``run_id``), or ``skipped`` with the ``reason`` the run was not admitted. ``missed`` counts
    the later slots that were also due when the turn came, run by this one turn instead of one
    each, and ``created_at`` is when the turn came. ``gate`` is its run's gate result once the
    run's suite has scored it: the suite scorecard's gate, ``passed``, ``failed``,
    ``insufficient_coverage`` or ``unconfigured``; None until then, and for a run that did not
    complete.

    ``trigger`` is why the slot ran: ``slot``, its schedule's timing, or ``model_change``, a slot
    of its own that a change of the model behind the target started. ``model_change`` is the
    change the slot's run was started for, on either kind: a change noticed at a turn whose regular
    slot was due rides on that slot's run."""

    schedule_id: Identifier
    slot: datetime
    state: Literal["admitted", "skipped"]
    run_id: Identifier | None = None
    reason: str = ""
    missed: int = Field(default=0, ge=0)
    created_at: datetime | None = None
    gate: Literal["passed", "failed", "insufficient_coverage", "unconfigured"] | None = None
    trigger: Literal["slot", "model_change"] = "slot"
    model_change: ModelChange | None = None

    @model_validator(mode="after")
    def run_when_admitted(self) -> ScheduledRun:
        if (self.state == "admitted") != (self.run_id is not None):
            raise ValueError("an admitted slot names its run, and a skipped one none")
        if self.trigger == "model_change" and self.model_change is None:
            raise ValueError("a slot a model change started names the change")
        return self


THRESHOLD_WINDOW_BOUND = 5000
"""The most scores one evaluation of a threshold reads from its window: the newest."""


class MonitorScores(_Record):
    """What a threshold judges: the scores one scorer of a monitor stored in the window."""

    kind: Literal["monitor"] = "monitor"
    monitor_id: Identifier
    scorer: str = Field(min_length=1, max_length=120)

    @property
    def source_id(self) -> str:
        return self.monitor_id


class ScheduleGates(_Record):
    """What a threshold judges, the second kind of source (MartianSecurity/martian-notes#197): the gate
    results of a schedule's runs whose slots are in the window, each ``passed`` as True and
    ``failed`` or ``insufficient_coverage`` as False (:class:`ScheduledRun`). A pass rate is the
    share of its runs that passed their suite's gate."""

    kind: Literal["gate"] = "gate"
    schedule_id: Identifier

    @property
    def source_id(self) -> str:
        return self.schedule_id


class WebhookChannel(_Record):
    """Where a threshold's webhook alerts go. The secret they are signed with is kept beside the
    threshold, never in it (:meth:`RecordStore.threshold_secret`)."""

    url: str = Field(min_length=1, max_length=2000)


class EmailChannel(_Record):
    """Who a threshold's email alerts go to."""

    to: list[str] = Field(min_length=1, max_length=10)

    @field_validator("to")
    @classmethod
    def addresses(cls, to: list[str]) -> list[str]:
        for address in to:
            local, at, domain = address.partition("@")
            if not (at and local and "." in domain and len(address) <= 254) or any(
                c.isspace() or ord(c) < 32 or c in '<>,;"' for c in address
            ):
                raise ValueError("Give each recipient as one plain address, such as oncall@example.com")
        if len(set(to)) != len(to):
            raise ValueError("Give each recipient once")
        return to


class Threshold(_Record):
    """A threshold on a measure of scores over a window (MartianSecurity/martian-notes#198): healthy
    while ``measure`` is ``direction`` ``value``, judged on the 95% interval of the scores in the
    last ``window_seconds`` once there are ``min_count`` of them (:mod:`martian.evals.thresholds`).

    ``pass_rate`` counts boolean scores; ``mean`` averages numeric ones. A pass rate's ``value`` is
    strictly between 0 and 1, since no interval can clear 0 or 1, and a mean needs two scores for
    an interval. ``webhook`` and ``email`` are where its alerts go; with neither it changes state
    and sends nothing. The core's own record, as a monitor is."""

    id: Identifier
    project_id: Identifier
    name: str = Field(min_length=1, max_length=120)
    source: MonitorScores | ScheduleGates
    measure: Literal["pass_rate", "mean"]
    direction: Literal["at_least", "at_most"]
    value: float = Field(allow_inf_nan=False)
    window_seconds: int = Field(ge=60, le=30 * 86400)
    min_count: int = Field(ge=1, le=THRESHOLD_WINDOW_BOUND)
    webhook: WebhookChannel | None = None
    email: EmailChannel | None = None
    created_by: str | None = None
    created_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def printable(cls, name: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError("A threshold's name is one line of text")
        return name

    @model_validator(mode="after")
    def decidable(self) -> Threshold:
        if (problem := threshold_problem(self.measure, self.value, self.min_count)) is not None:
            raise ValueError(problem)
        return self


def threshold_problem(measure: str, value: float, min_count: int) -> str | None:
    """Why a threshold could never be decided, or None: a pass rate's value is strictly between 0
    and 1, since no interval clears 0 or 1, and a mean needs two scores for an interval."""
    if measure == "pass_rate" and not 0 < value < 1:
        return "A pass rate threshold is between 0 and 1, both left out"
    if measure == "mean" and min_count < 2:
        return "A mean threshold needs a min_count of at least 2"
    return None


class Observation(_Record):
    """One evaluation of a threshold: the ``count`` scores in ``[window_start, at)``, their
    ``estimate`` (a pass rate or a mean) and its 95% ``interval``, and the ``verdict``:
    ``insufficient`` below ``min_count`` or with no interval, ``breach`` when the whole interval is
    on the wrong side of the value, ``healthy`` when it is wholly on the right side, and
    ``undecided`` while it holds the value. ``truncated`` when the window held more scores than
    one evaluation reads (:data:`THRESHOLD_WINDOW_BOUND`)."""

    at: datetime
    window_start: datetime
    count: int = Field(ge=0)
    estimate: float | None = None
    interval: list[float] | None = None
    verdict: Literal["insufficient", "undecided", "breach", "healthy"]
    truncated: bool = False


class ThresholdStatus(_Record):
    """Where a threshold's state machine is, written only by its evaluations: ``unknown`` until a
    verdict first decides it, then ``ok`` or ``breached``. ``sequence`` counts its changes of state,
    and an alert names the one it is for. ``since`` is when the state began; ``last`` the latest
    evaluation."""

    state: Literal["unknown", "ok", "breached"] = "unknown"
    sequence: int = Field(default=0, ge=0)
    since: datetime | None = None
    last: Observation | None = None


class Alert(_Record):
    """One alert of a threshold to one channel: a breach (``breached``) or a recovery
    (``recovered``), the threshold's change of state number ``sequence``. Its id is derived from the
    threshold, the sequence and the channel, so each change has one alert per channel.

    ``payload`` is what is sent, without the webhook's secret. It is ``pending`` until a delivery
    succeeds (``delivered``) or the last attempt fails (``failed``); ``next_attempt_at`` is when a
    pending alert is next tried, ``attempts`` how often it was, and ``last_error`` why the last
    attempt failed, naming a status or an error class, never an address, a body or a secret."""

    id: Identifier
    threshold_id: Identifier
    channel: Literal["webhook", "email"]
    event: Literal["breached", "recovered"]
    sequence: int = Field(ge=1)
    payload: dict[str, Any]
    state: Literal["pending", "delivered", "failed"] = "pending"
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime | None = None
    last_error: str = ""
    created_at: datetime | None = None
    delivered_at: datetime | None = None


class RedteamRecords(BaseModel):
    """What only a red team run has, as JSON objects of :mod:`martian.redteam.model`: its probes,
    evidence, verifications and findings. The core keeps them beside the run's case results and
    scores and does not interpret them; :mod:`martian.redteam.store` reads and writes them."""

    model_config = ConfigDict(extra="forbid")

    probes: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    verifications: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)


@runtime_checkable
class RecordStore(Protocol):
    events: EventBus
    admission: RunAdmission | None

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
        kind: RunKind | None = "eval",
    ) -> Page[Run]:
        """Newest first. Runs of one ``kind`` only, eval runs unless asked; ``None`` lists both."""
        ...

    async def count_active_runs(self, principal: Principal, *, up_to: int | None = None) -> int:
        """How many runs of the organisation, in every project, are ``queued`` or ``running``, in
        one query. With ``up_to``, it stops counting there and reads at most that many rows."""
        ...

    async def update_run(
        self,
        principal: Principal,
        run: Run,
        *,
        expected_state: RunState | None = None,
        expected_checkpoint: Run | None = None,
    ) -> Run:
        """Replace the stored run. Match its state when ``expected_state`` is supplied.
        With ``expected_checkpoint``, also match the saved checkpoint's state and completion time."""
        ...

    async def list_finished_runs(
        self,
        principal: Principal,
        project_id: str,
        *,
        created_before: datetime,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[Run]:
        """The project's runs that are neither ``queued`` nor ``running`` and were created before
        ``created_before``, oldest first: the runs a retention policy may remove with
        :meth:`delete_run`. A caller that deletes what it lists asks again without a cursor."""
        ...

    async def delete_run(self, principal: Principal, project_id: str, run_id: str) -> dict[str, int]:
        """Remove the run and every record keyed to it, in one transaction: its case results, its
        scores of every source and scoring job, its scoring jobs, its reviews, the blind mode of its
        review queues (:class:`BlindQueue`), its work left in the worker's queue, the outbox's
        deliveries of its scores, whatever their state, and the slot of the schedule that started
        it. Returns how many rows it removed from each of those tables, by table name.

        Refused as a ``conflict`` while anything still works on the run: the run or one of its
        scoring jobs is ``queued`` or ``running``, or a worker's claim whose lease has not run out
        holds its work. Cancel it, or let it end, first. A run that does not exist is
        ``not_found``. The run's traces and its record in the store (``EvalStore``) are the
        store's, and stay."""
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
        """Store scores of a run's cases. A score is replaced only by one from the same scorer,
        scoring job and source: a scoring job's scores never replace the run's own, and a person's
        answer never replaces a check's or a judge's score of the same name."""
        ...

    async def list_scores(
        self, principal: Principal, project_id: str, run_id: str, *, case_id: str | None = None
    ) -> list[Score]: ...

    # Red team runs: what only they have, beside their case results and scores.
    async def put_redteam_records(
        self, principal: Principal, project_id: str, run_id: str, records: RedteamRecords
    ) -> None:
        """Store a red team run's probes, evidence, verifications and findings, replacing what the run
        had, in one transaction. The run must exist and be a red team run."""
        ...

    async def get_redteam_records(self, principal: Principal, project_id: str, run_id: str) -> RedteamRecords:
        """A red team run's probes, evidence, verifications and findings, each in the order stored;
        empty for a run without them. A run that does not exist is ``not_found``."""
        ...

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

    async def put_suite_audit(self, principal: Principal, project_id: str, audit: SuiteAudit) -> SuiteAudit:
        """Store the audit with its suite version, replacing an earlier audit of that version. A
        suite version not saved raises ``not_found``."""
        ...

    async def get_suite_audit(
        self, principal: Principal, project_id: str, suite_id: str, version: int
    ) -> SuiteAudit | None: ...

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

    # Dataset manifests: what publishing wrote, checked when a run reads the dataset.
    async def begin_publication(
        self, principal: Principal, project_id: str, manifest: DatasetManifest
    ) -> DatasetManifest:
        """Record a publication as ``publishing``. The same id again returns the stored manifest."""
        ...

    async def finish_publication(
        self, principal: Principal, project_id: str, manifest_id: str, dataset_id: str
    ) -> DatasetManifest:
        """Mark the publication ``published``, as the store's dataset ``dataset_id``."""
        ...

    async def manifest_of(self, principal: Principal, project_id: str, dataset_id: str) -> DatasetManifest | None:
        """The published manifest of a store dataset, or ``None`` for one Martian did not publish."""
        ...

    async def list_publications(self, principal: Principal, project_id: str) -> list[DatasetManifest]:
        """Every publication of the project, newest first."""
        ...

    # The outbox: store writes kept until they are delivered.
    async def put_delivery(self, principal: Principal, project_id: str, delivery: Delivery) -> Delivery:
        """Keep a delivery to make. One with the same id is replaced and pending again, so the newest
        write of the same thing is the one delivered."""
        ...

    async def settle_delivery(self, principal: Principal, project_id: str, delivery: Delivery) -> Delivery:
        """Record the outcome of an attempt: the delivery as it is now."""
        ...

    async def get_delivery(self, principal: Principal, project_id: str, delivery_id: str) -> Delivery | None: ...

    async def due_deliveries(self, principal: Principal, *, now: datetime, limit: int = 100) -> list[Delivery]:
        """Pending deliveries of every project of the principal's space whose next attempt is due,
        oldest first."""
        ...

    # Review queues with blind double review: recorded once, before the queue is created.
    async def add_blind_queue(self, principal: Principal, project_id: str, queue: BlindQueue) -> BlindQueue:
        """Record that a review queue of a run of the project has blind double review. Recording the
        same queue again returns the stored one; a run the project does not have is ``not_found``."""
        ...

    async def list_blind_queues(
        self, principal: Principal, project_id: str, *, run_id: str | None = None
    ) -> list[BlindQueue]:
        """The project's review queues with blind double review, or one run's, oldest first."""
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
        """Remove the monitor, its progress, its record of the traces it scored, and the
        thresholds on its scores with their alerts. The scores it stored on traces stay there."""
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

    async def scored_monitored_traces(
        self, principal: Principal, project_id: str, monitor_id: str, *, since: datetime, until: datetime, limit: int
    ) -> list[MonitoredTrace]:
        """The monitor's ``scored`` traces it took in ``[since, until)``, newest first, at most
        ``limit``: the window a threshold on its scores reads."""
        ...

    async def forget_traces(self, principal: Principal, project_id: str, trace_ids: list[str]) -> dict[str, int]:
        """Remove what the records keep of traces the store deleted (``EvalStore.delete_traces``),
        at most ``TRACE_DELETE_LIMIT`` of them a call, in one transaction: each monitor's record of
        having taken them, with the scores it kept there, and the outbox's deliveries to them,
        whatever their state. Returns how many rows it removed from each of those tables, by table
        name. The case results and scores of a run that name a trace stay with their run."""
        ...

    # Thresholds on scores, their state, and the alerts they raise.
    async def create_threshold(
        self, principal: Principal, project_id: str, threshold: Threshold, *, webhook_secret: str | None
    ) -> Threshold:
        """Create the threshold, with status ``unknown``, or return the stored one with the same
        id. ``webhook_secret`` is kept beside it for signing its webhook alerts, and only
        :meth:`threshold_secret` returns it. A threshold without a webhook has none. One with a
        webhook has one, unless an import wrote it (``martian.evals.data_export``), which leaves
        the secret out: it is given again with :meth:`set_threshold_secret`."""
        ...

    async def get_threshold(self, principal: Principal, project_id: str, threshold_id: str) -> Threshold | None: ...

    async def list_thresholds(
        self, principal: Principal, project_id: str, *, source_kind: str | None = None, source_id: str | None = None
    ) -> list[Threshold]:
        """By name; only those on one source when ``source_kind`` and ``source_id`` name it."""
        ...

    async def delete_threshold(self, principal: Principal, project_id: str, threshold_id: str) -> None:
        """Remove the threshold, its status, its secret and its alerts, delivered or not."""
        ...

    async def threshold_status(
        self, principal: Principal, project_id: str, threshold_id: str
    ) -> ThresholdStatus | None:
        """The threshold's status, or None for a threshold that does not exist."""
        ...

    async def set_threshold_status(
        self,
        principal: Principal,
        project_id: str,
        threshold_id: str,
        status: ThresholdStatus,
        *,
        expected_sequence: int,
        alerts: Sequence[Alert] = (),
    ) -> bool:
        """In one transaction, replace the status and add ``alerts``, while the stored status's
        ``sequence`` is ``expected_sequence``. True when it was; False when another evaluation
        changed the state first or the threshold is gone, which leaves everything as it was. So a
        change of state and its alerts are written together, once."""
        ...

    async def threshold_secret(self, principal: Principal, project_id: str, threshold_id: str) -> str | None:
        """The secret the threshold's webhook alerts are signed with, for the worker that signs
        them. No route calls it."""
        ...

    async def threshold_signed(self, principal: Principal, project_id: str, threshold_id: str) -> bool:
        """Whether the threshold has a webhook secret, without reading it: False for a threshold
        an import wrote until its secret is given again, and for one that is gone."""
        ...

    async def set_threshold_secret(self, principal: Principal, project_id: str, threshold_id: str, secret: str) -> bool:
        """Give a threshold with a webhook and no secret, such as one an import wrote, the secret
        its alerts are signed with. True when it did; False when the threshold is gone, has no
        webhook or has a secret already, which stays: a secret is written once."""
        ...

    async def take_due_alerts(
        self,
        principal: Principal,
        project_id: str,
        threshold_ids: Sequence[str],
        *,
        now: datetime,
        hold_until: datetime,
        limit: int = 20,
    ) -> list[Alert]:
        """The ``pending`` alerts of these thresholds due by ``now``, oldest first, at most
        ``limit``, each taken by moving its ``next_attempt_at`` to ``hold_until``, so that no other
        delivery takes it before then."""
        ...

    async def finish_alert(self, principal: Principal, project_id: str, alert: Alert) -> None:
        """Record the alert as a delivery attempt left it. Nothing happens once it is gone."""
        ...

    async def list_alerts(
        self, principal: Principal, project_id: str, threshold_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> Page[Alert]:
        """The threshold's alerts, newest first."""
        ...

    # Schedules: suites run against a target on a schedule, and the slots each has run.
    async def create_schedule(self, principal: Principal, project_id: str, schedule: Schedule) -> Schedule:
        """Create the schedule, or return the stored one with the same id."""
        ...

    async def get_schedule(self, principal: Principal, project_id: str, schedule_id: str) -> Schedule | None: ...

    async def list_schedules(self, principal: Principal, project_id: str) -> list[Schedule]:
        """By name."""
        ...

    async def update_schedule(
        self, principal: Principal, project_id: str, schedule: Schedule, *, expected_revision: int
    ) -> Schedule:
        """Replace the stored schedule while it is at ``expected_revision``, and raise the
        revision. Its progress is left as it is."""
        ...

    async def delete_schedule(self, principal: Principal, project_id: str, schedule_id: str) -> None:
        """Remove the schedule, its progress, its slots, and the thresholds on its gate results with
        their alerts. The runs it started stay."""
        ...

    async def schedule_progress(
        self, principal: Principal, project_id: str, schedule_id: str
    ) -> ScheduleProgress | None:
        """The schedule's progress, or None for a schedule that does not exist."""
        ...

    async def set_schedule_progress(
        self, principal: Principal, project_id: str, schedule_id: str, progress: ScheduleProgress
    ) -> None:
        """Replace the schedule's progress. Nothing happens once the schedule is gone."""
        ...

    async def add_scheduled_run(self, principal: Principal, project_id: str, entry: ScheduledRun) -> ScheduledRun:
        """Record a slot of the schedule, or return the one recorded for that slot already, so a
        slot is recorded once. Raises ``StoreError`` (``not_found``) once the schedule is gone."""
        ...

    async def get_scheduled_run(
        self, principal: Principal, project_id: str, schedule_id: str, slot: datetime
    ) -> ScheduledRun | None: ...

    async def list_scheduled_runs(
        self, principal: Principal, project_id: str, schedule_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> Page[ScheduledRun]:
        """The schedule's slots, the latest first."""
        ...

    async def set_scheduled_run_gate(
        self, principal: Principal, project_id: str, schedule_id: str, slot: datetime, gate: str
    ) -> ScheduledRun | None:
        """Record the gate result of the slot's run, or None when the slot or the schedule is gone."""
        ...


RECORD_METHODS = frozenset(
    name for name in vars(RecordStore) if not name.startswith("_") and callable(getattr(RecordStore, name))
)

__all__ = [
    "LAUNCHED",
    "RECORD_METHODS",
    "THRESHOLD_WINDOW_BOUND",
    "Alert",
    "BlindQueue",
    "CalibrationRecheck",
    "DatasetManifest",
    "Delivery",
    "EmailChannel",
    "Event",
    "EventBus",
    "EventsLost",
    "InProcessEvents",
    "JudgeCalibration",
    "LabelledItem",
    "ModelChange",
    "ModelWatch",
    "ModelWatchProgress",
    "Monitor",
    "MonitorFilter",
    "MonitorProgress",
    "MonitorScores",
    "MonitoredTrace",
    "Observation",
    "RecheckState",
    "RecordStore",
    "RedteamRecords",
    "RunAdmission",
    "SavedView",
    "Schedule",
    "ScheduleGates",
    "ScheduleProgress",
    "ScheduledRun",
    "Subscription",
    "SuiteAudit",
    "Threshold",
    "ThresholdStatus",
    "WebhookChannel",
    "threshold_problem",
]
