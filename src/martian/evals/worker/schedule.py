"""One turn of a schedule: the run of a saved suite against a target that its slot makes due
(MorningHQ/martian-notes#197).

A schedule (:class:`~martian.evals.records.Schedule`) runs a saved suite version over a pinned
dataset version against a target, at slots every so many seconds or on a cron expression, in UTC
(:mod:`martian.evals.slots`). Its work is a row of the durable queue that comes back at each slot,
as a monitor's does (:mod:`martian.evals.worker.queue`), and the queue's worker takes each turn
itself, in its own process, as the API process takes a launch: a turn calls no target and no
model. One turn, :func:`execute_schedule`:

- **Waits for its slot.** The slot due is the schedule's ``next_slot``, or the first slot after
  ``active_since`` (when it was created or last resumed), whichever is later. Before then the turn
  does nothing, and the row comes back at the slot.
- **Runs a slot once, however many were missed.** When the turn comes after several slots were
  due, because no worker took the row in time (a worker down, or every place of the workers
  taken), it admits one run, for the earliest slot due, and counts the later ones as ``missed``.
  Its next slot is then the first after now. Each slot is recorded once
  (:meth:`~martian.evals.records.RecordStore.add_scheduled_run`), and its run's id is derived from
  the schedule and the slot, so a turn whose worker stopped part way never admits a second run.
- **Admits the run as a launch does.** The edition's ``monitoring`` feature, the project not
  archived, the worker's pin of the target as it is now, the pinned dataset and suite versions
  still there, and the organisation's ``concurrent_runs`` (:func:`~martian.evals.server.launch.admit_run`).
  A run that is not admitted is recorded as ``skipped`` with the refusal as its ``reason``; nothing
  waits for a place.
- **Scores the run with its suite.** The run's suite scoring job is recorded ``queued`` before the
  run is handed to the queue, with the schedule's scorers, each judge on the model it was pinned to
  when the schedule was created. Once the run's work has ended, :func:`after_scheduled_run` queues
  the job if the run completed and cancels it otherwise. As with the local edition's suite job,
  the job is admitted with its run, under ``concurrent_runs``.

Once a scheduled run's suite job has scored it, the worker that ran the job records the run's
gate result on its slot and evaluates the thresholds on the schedule's gate results, then delivers
their due alerts (:func:`record_gate`, :class:`~martian.evals.thresholds.GateSource`), as a
monitor's pass does for the thresholds on its scores.

A schedule with ``on_model_change`` also runs when the model behind its target changes
(MorningHQ/martian-notes#200). Its row then comes back every ``model_watch_seconds`` as well as at
its slots, and each turn first reads the model (:func:`~martian.evals.worker.model_watch.watch_model`).
A change it decides is written to the progress before any run is admitted, with the slot that
will hold it: the regular slot due at the same turn, whose run then stands for the change, or a
slot of its own at the turn's time (``trigger`` ``model_change``). So a turn that stops part way
admits no second run for the change: the next turn finds the change and its slot, as it finds a
regular slot. The slot's history entry notes the change (``model_change``).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException

from martian.evals.alerts import AlertSender
from martian.evals.model import ScoringJob, Target
from martian.evals.records import ModelChange, RecordStore, Schedule, ScheduledRun, ScheduleProgress
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id
from martian.evals.slots import Cron, Interval, timing
from martian.evals.store import EvalStore, StoreError
from martian.evals.thresholds import GateSource, evaluate_thresholds

# The package defines these before it imports this module.
from martian.evals.worker import Work, Worker
from martian.evals.worker.model_watch import SETTLE_SECONDS, WATCH_SECONDS, settled, watch_model

if TYPE_CHECKING:
    from martian.evals.server.entitlements import Entitlements
    from martian.evals.server.principal import Principal

log = logging.getLogger("martian.evals.worker")

SCHEDULE_KEY = "schedule"
"""``Run.provenance`` key of the schedule and slot a run was admitted for."""
MISSED_UP_TO = 1000
"""The most missed slots a turn counts."""
DATASET_GONE = "The schedule's dataset version is no longer in the store"
SUITE_GONE = "The schedule's suite version is no longer saved"
NO_JUDGE = "Model judges require a judge configured for this installation's worker"
NOT_COMPLETED = "The run did not complete, so its suite did not score it"
Trigger = Literal["slot", "model_change"]


@dataclass(frozen=True)
class ScheduleServices:
    """What a worker admits a schedule's runs with, as the API process admits a launch: the
    ``store``, and ``entitlements``, the edition's function from a principal to its
    :class:`~martian.evals.server.Entitlements`, which holds the ``monitoring`` feature and the
    ``concurrent_runs`` limit. A worker given none runs no schedules.

    For a schedule that watches the model behind its target, ``model_watch_seconds`` is how often
    its turn comes back besides its slots, and ``trace_settle_seconds`` how long after its request
    time a trace is first read (:mod:`martian.evals.worker.model_watch`)."""

    store: EvalStore
    entitlements: Callable[[Principal], Entitlements]
    model_watch_seconds: float = WATCH_SECONDS
    trace_settle_seconds: float = SETTLE_SECONDS

    def __post_init__(self) -> None:
        if self.model_watch_seconds <= 0 or self.trace_settle_seconds < 0:
            raise ValueError("model_watch_seconds is positive and trace_settle_seconds not negative")


def _now() -> datetime:
    return datetime.now(UTC)


def schedule_timing(schedule: Schedule) -> Interval | Cron:
    return timing(every_seconds=schedule.every_seconds, cron=schedule.cron)


def due_slot(schedule: Schedule, progress: ScheduleProgress | None) -> datetime:
    """The slot the schedule's next run is for: its ``next_slot``, unless that is not after
    ``active_since``, when it is the first slot after ``active_since``, so the slots of a pause
    are not run."""
    start = schedule.active_since or schedule.created_at or _now()
    due = progress.next_slot if progress is not None else None
    return due if due is not None and due > start else schedule_timing(schedule).after(start)


def next_due(
    schedule: Schedule, progress: ScheduleProgress | None, now: datetime, *, watch_seconds: float | None = None
) -> datetime:
    """When the schedule's row in the queue is next due: its due slot while it is active and that
    slot is still ahead, else the first slot after ``now``. An active schedule that watches its
    model is due sooner, ``watch_seconds`` from now, and at once while its watch has more to read
    or a change to record."""
    active = schedule.state == "active"
    slot = due if active and (due := due_slot(schedule, progress)) > now else schedule_timing(schedule).after(now)
    if not active or schedule.on_model_change is None or watch_seconds is None:
        return slot
    watch = progress.model_watch if progress is not None else None
    if watch is not None and (not watch.caught_up or watch.change is not None):
        return now
    return min(slot, now + timedelta(seconds=watch_seconds))


def scheduled_run_id(schedule_id: str, slot: datetime) -> str:
    """The id of the run a schedule admits for a slot."""
    return str(derived_id("scheduled-run", schedule_id, slot.astimezone(UTC).isoformat()))


def suite_job_id(run_id: str) -> str:
    """The id of a scheduled run's suite scoring job, derived as the local edition's suite job's."""
    return str(derived_id("suite-scoring", run_id))


async def execute_schedule(services: ScheduleServices, records: RecordStore, worker: Worker, work: Work) -> None:
    """One turn of the schedule ``work`` names, by ``worker``, which checks the target and queues
    the run. A paused or deleted schedule's turn does nothing. A turn that fails part way leaves
    the slot due, and the next turn looks at it again. A schedule that watches its model reads it
    first, and a change it decides is admitted with the slot due now or in a slot of its own."""
    principal, project, schedule_id = work.principal, work.project_id, work.schedule_id or ""
    schedule = await records.get_schedule(principal, project, schedule_id)
    if schedule is None or schedule.state != "active":
        return
    progress = await records.schedule_progress(principal, project, schedule_id) or ScheduleProgress()
    now, slots = _now(), schedule_timing(schedule)
    due = due_slot(schedule, progress)
    open_slot = now >= due and await records.get_scheduled_run(principal, project, schedule_id, due) is None
    watching = schedule.on_model_change is not None
    if watching:
        earlier = progress.model_watch.change if progress.model_watch is not None else None
        watch = await watch_model(
            services.store,
            records,
            principal,
            schedule,
            progress.model_watch,
            now,
            slot=due if open_slot else None,
            settle_seconds=services.trace_settle_seconds,
        )
        progress = progress.model_copy(update={"model_watch": watch})
        if watch.change is not None and watch.change != earlier:
            # A change is in the progress, with its slot, before any run is admitted for it.
            await records.set_schedule_progress(principal, project, schedule_id, progress)
    change = progress.model_watch.change if progress.model_watch is not None else None
    if open_slot:
        missed = slots.count(due, now, up_to=MISSED_UP_TO)
        rides = change if change is not None and change.at == due else None
        if not await _record(services, records, worker, principal, schedule, due, missed, now, rides, "slot"):
            return
    # A change that did not ride on this turn's slot has a slot of its own, unless a turn that
    # stopped part way already recorded it.
    own_slot = change is not None and not (open_slot and change.at == due)
    if (
        own_slot
        and change is not None
        and await records.get_scheduled_run(principal, project, schedule_id, change.at) is None
        and not await _record(services, records, worker, principal, schedule, change.at, 0, now, change, "model_change")
    ):
        return
    updates: dict[str, Any] = {}
    if change is not None and progress.model_watch is not None:
        updates["model_watch"] = settled(progress.model_watch, change, now)
    if now >= due:
        updates |= {"next_slot": slots.after(now), "last_slot": due, "error": "", "last_turn_at": now}
    elif progress.next_slot != due:
        updates["next_slot"] = due
    if updates or watching:
        await records.set_schedule_progress(principal, project, schedule_id, progress.model_copy(update=updates))


async def _record(
    services: ScheduleServices,
    records: RecordStore,
    worker: Worker,
    principal: Principal,
    schedule: Schedule,
    slot: datetime,
    missed: int,
    now: datetime,
    change: ModelChange | None,
    trigger: Trigger,
) -> bool:
    """Admit the run of ``slot`` and record the slot; False once the schedule is gone."""
    entry = await _fire(services, records, worker, principal, schedule, slot, missed, now, change, trigger)
    try:
        entry = await records.add_scheduled_run(principal, schedule.project_id, entry)
    except StoreError as error:
        if error.kind != "not_found":
            raise
        return False  # deleted meanwhile; a run it admitted stays an ordinary run
    if entry.state == "skipped":
        log.warning("Schedule %s skipped its slot %s: %s", schedule.id, slot.isoformat(), entry.reason)
    return True


async def _fire(
    services: ScheduleServices,
    records: RecordStore,
    worker: Worker,
    principal: Principal,
    schedule: Schedule,
    slot: datetime,
    missed: int,
    now: datetime,
    change: ModelChange | None = None,
    trigger: Trigger = "slot",
) -> ScheduledRun:
    """Admit the run of ``slot``, and what the slot records of it, with the model ``change`` its
    run stands for, if any."""
    run_id = scheduled_run_id(schedule.id, slot)
    admitted = ScheduledRun.model_validate(
        {
            "schedule_id": schedule.id,
            "slot": slot,
            "state": "admitted",
            "run_id": run_id,
            "missed": missed,
            "created_at": now,
            "trigger": trigger,
            "model_change": change,
        }
    )
    if await records.get_run(principal, schedule.project_id, run_id) is not None:
        return admitted  # admitted by a turn that stopped before it recorded the slot
    try:
        await _admit(services, records, worker, principal, schedule, slot, run_id, admitted)
    except HTTPException as refused:
        return admitted.model_copy(update={"state": "skipped", "run_id": None, "reason": str(refused.detail)})
    except StoreError as refused:
        if refused.kind == "conflict":
            raise  # changed meanwhile: the next turn looks at the slot again
        return admitted.model_copy(update={"state": "skipped", "run_id": None, "reason": refused.detail})
    return admitted


async def _admit(
    services: ScheduleServices,
    records: RecordStore,
    worker: Worker,
    principal: Principal,
    schedule: Schedule,
    slot: datetime,
    run_id: str,
    entry: ScheduledRun,
) -> None:
    """Admit the slot's run under the rules of a launch, or raise the refusal."""
    # The routes' modules import the worker package, so they are imported when a turn needs them.
    from martian.evals.server.dependencies import ensure_entitled
    from martian.evals.server.launch import admit_run
    from martian.evals.server.runs import writable_key
    from martian.evals.worker.execute import LIMITS_KEY

    store = services.store
    entitlements = services.entitlements(principal)
    ensure_entitled(entitlements, "monitoring")
    project = await writable_key(store, principal, schedule.project_id)
    pin = await worker.check_target(principal, schedule.target)
    dataset = await store.get_dataset(principal, DEFAULT_PROJECT, schedule.dataset_id)
    if dataset is None or dataset.digest != schedule.dataset_digest:
        raise HTTPException(422, DATASET_GONE)
    suite = await records.get_suite(principal, DEFAULT_PROJECT, schedule.suite_id, version=schedule.suite_version)
    if suite is None:
        raise HTTPException(422, SUITE_GONE)
    if any(scorer.kind == "llm_judge" for scorer in schedule.scorers) and not worker.judges:
        raise HTTPException(422, NO_JUDGE)
    scheduled: dict[str, Any] = {"id": schedule.id, "name": schedule.name, "slot": slot.astimezone(UTC).isoformat()}
    if entry.trigger != "slot":
        scheduled["trigger"] = entry.trigger
    if (change := entry.model_change) is not None:
        scheduled["model_change"] = {"from": change.previous, "to": change.current}
    provenance: dict[str, Any] = {SCHEDULE_KEY: scheduled}
    if schedule.limits is not None:
        provenance[LIMITS_KEY] = schedule.limits.model_dump(mode="json")
    await admit_run(
        store,
        records,
        _SuiteJobFirst(worker, records, schedule),
        principal,
        entitlements=entitlements,
        run_id=run_id,
        project=project,
        target=schedule.target,
        pin=pin,
        dataset=dataset,
        max_cases=schedule.max_cases,
        suite=suite,
        provenance=provenance,
    )


class _SuiteJobFirst:
    """The worker a scheduled run is admitted through: before it hands the run to ``worker``, it
    records the run's suite scoring job, ``queued``, with the schedule's pinned scorers and
    policies, so the job is there when the run ends. If the run cannot be handed on, the job is
    cancelled, as the launch fails the run."""

    def __init__(self, worker: Worker, records: RecordStore, schedule: Schedule) -> None:
        self._worker, self._records, self._schedule = worker, records, schedule

    @property
    def judges(self) -> bool:
        return self._worker.judges

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        return await self._worker.check_target(principal, target)

    async def submit(self, work: Work) -> None:
        schedule = self._schedule
        await self._records.create_scoring_job(
            work.principal,
            work.project_id,
            ScoringJob(
                id=suite_job_id(work.run_id),
                run_id=work.run_id,
                scorers=list(schedule.scorers),
                policies=list(schedule.policies),
                suite_id=schedule.suite_id,
                suite_version=schedule.suite_version,
                state="queued",
                requested_by=work.principal.user_id,
                created_at=_now(),
            ),
        )
        try:
            await self._worker.submit(work)
        except Exception:
            await _cancel_suite_job(self._records, work)
            raise


async def _cancel_suite_job(records: RecordStore, work: Work) -> None:
    """Cancel the suite scoring job of a scheduled run that did not complete, if it is still queued."""
    jobs = await records.list_scoring_jobs(work.principal, work.project_id, work.run_id)
    job = next((job for job in jobs if job.id == suite_job_id(work.run_id)), None)
    if job is None or job.state != "queued":
        return
    changes = {"state": "cancelled", "error": NOT_COMPLETED, "finished_at": _now()}
    with contextlib.suppress(StoreError):  # it moved on meanwhile
        await records.update_scoring_job(
            work.principal, work.project_id, job.model_copy(update=changes), expected_state="queued"
        )


async def after_scheduled_run(records: RecordStore, worker: Worker, work: Work) -> None:
    """Once a run's work has ended: for a run a schedule admitted, queue its suite scoring job if
    the run completed, and cancel the job otherwise. Nothing resumes a scheduled run that stopped
    at one of its limits, so its job is cancelled too; the schedule's next slot is a new run. A
    job that is no longer queued is left alone, so doing this twice changes nothing."""
    run = await records.get_run(work.principal, work.project_id, work.run_id)
    if run is None or SCHEDULE_KEY not in run.provenance or run.state in ("queued", "running"):
        return
    if run.state != "completed":
        await _cancel_suite_job(records, work)
        return
    jobs = await records.list_scoring_jobs(work.principal, work.project_id, work.run_id)
    if any(job.id == suite_job_id(run.id) and job.state == "queued" for job in jobs):
        scoring = Work(
            kind="scoring",
            principal=work.principal,
            project_id=work.project_id,
            run_id=run.id,
            job_id=suite_job_id(run.id),
        )
        await worker.submit(scoring)


async def record_gate(records: RecordStore, work: Work, *, alerts: AlertSender) -> None:
    """After a scoring job's work: when it is the suite job of a run a schedule admitted and it
    has scored the run, record the run's gate result, its suite scorecard's gate, on its slot,
    evaluate the thresholds on the schedule's gate results and deliver their due alerts, those
    just raised and those an earlier attempt left to retry. A job that was cancelled or stopped
    gives no gate result. A failure here is logged; the schedule's next scored run evaluates
    again."""
    try:
        await _record_gate(records, work, alerts)
    except Exception:  # noqa: BLE001 - the run and its scores stand; the next scored run evaluates again
        log.exception("The gate of scheduled run %s was not recorded or its thresholds not evaluated", work.run_id)


async def _record_gate(records: RecordStore, work: Work, alerts: AlertSender) -> None:
    # The routes' modules import the worker package, so they are imported when a gate needs them.
    from martian.evals.server.projection import Assessment, item_out, job_config
    from martian.evals.server.quality import scorecard
    from martian.evals.server.runs import run_results

    principal, project = work.principal, work.project_id
    run = await records.get_run(principal, project, work.run_id)
    scheduled = run.provenance.get(SCHEDULE_KEY) if run is not None else None
    if run is None or not isinstance(scheduled, dict) or work.job_id != suite_job_id(run.id):
        return
    job = next((j for j in await records.list_scoring_jobs(principal, project, run.id) if j.id == work.job_id), None)
    if job is None or job.state not in ("completed", "partial", "failed"):
        return
    items = [item_out(result) for result in await run_results(records, principal, run)]
    scores = [
        Assessment.of(s) for s in await records.list_scores(principal, project, run.id) if s.scoring_job_id == job.id
    ]
    gate = scorecard(items, scores, job_config(job))["gate"]
    schedule_id, slot = str(scheduled["id"]), datetime.fromisoformat(str(scheduled["slot"]))
    if await records.set_scheduled_run_gate(principal, project, schedule_id, slot, gate) is None:
        return  # the schedule, or its slot, is gone
    on_gates = await records.list_thresholds(principal, project, source_kind="gate", source_id=schedule_id)
    if not on_gates:
        return
    await evaluate_thresholds(records, principal, project, on_gates, GateSource(records), now=_now())
    await alerts.deliver_due(records, principal, project, [t.id for t in on_gates])


__all__ = [
    "DATASET_GONE",
    "MISSED_UP_TO",
    "NOT_COMPLETED",
    "SCHEDULE_KEY",
    "SUITE_GONE",
    "ScheduleServices",
    "after_scheduled_run",
    "due_slot",
    "execute_schedule",
    "next_due",
    "record_gate",
    "schedule_timing",
    "scheduled_run_id",
    "suite_job_id",
]
