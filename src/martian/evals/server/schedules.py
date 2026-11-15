"""Schedules: a saved suite run against a target unattended, every so many seconds or on a cron
expression, in UTC (MorningHQ/martian-notes#197). The core's own routes; the server's frozen
contract has none.

A schedule names a saved suite version, a target the worker knows, a published dataset and a case
limit, as the local edition's launch does, and when its runs are due (:mod:`martian.evals.slots`).
Creating one records it and hands its work to the worker, which takes a turn at each slot and
admits the slot's run there, under the rules of a launch (:mod:`martian.evals.worker.schedule`);
the request calls no target and no model.

- Scheduling is part of monitoring: every route needs the edition's ``monitoring`` key, and the
  routes that change a schedule the ``run`` capability, as a launch does.
- The worker must run schedules (``RunsSchedules``: a :class:`~martian.evals.worker.QueueWorker`
  given ``schedules``), or creating and resuming one is ``503``.
- The schedule pins the suite version and the dataset version it was created with, and each of the
  suite's judges to its model, so its runs compare with each other. The target is pinned again for
  each run, as a launch pins it.
- A suite with a model judge needs ``acknowledge_model_cost``, as a launch with it does, and a
  worker with a judge.
- The id is derived from ``request_id``: the same request again returns the same schedule. A
  project has at most :data:`SCHEDULE_LIMIT` schedules.
- ``GET /schedules/{id}/runs`` is its history: each slot, newest first, with its run's state, pass
  rate and suite scorecard, and the paired comparison with the schedule's latest earlier run that
  completed (:func:`martian.evals.server.quality.compare_runs`).
- Pausing stops its turns admitting runs; resuming runs its slots from then on, not the ones that
  passed while it was paused. Deleting removes it, its progress and its history; its runs stay.
- ``on_model_change`` also runs the suite when the model behind the target changes, read from the
  project's live traces or from the model the target reported on its runs, debounced
  (:mod:`martian.evals.worker.model_watch`). The history notes each change that started a run.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the core's routes follow the contract's style.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from martian.evals import slots
from martian.evals.model import CaseResult, Run, RunLimits, Score, ScoringJob, Target
from martian.evals.records import ModelWatch, RecordStore, Schedule, ScheduledRun, ScheduleProgress
from martian.evals.server.dependencies import (
    JudgeSettingsDep,
    PrincipalDep,
    RecordsDep,
    StoreDep,
    WorkerDep,
    require_capability,
    require_entitlement,
)
from martian.evals.server.principal import Principal
from martian.evals.server.projection import (
    DEFAULT_PROJECT,
    Assessment,
    derived_id,
    item_out,
    job_config,
    project_key,
    project_out,
    public_job,
    suite_version_id,
    user_out,
)
from martian.evals.server.quality import compare_assessments, compare_runs, scorecard
from martian.evals.server.runs import project_keys, run_results, writable_key
from martian.evals.server.scoring import find_suite_version, with_models
from martian.evals.store import EvalStore
from martian.evals.worker import RunsSchedules, Work, Worker
from martian.evals.worker.schedule import NO_JUDGE, due_slot, suite_job_id

router = APIRouter(prefix="/v1/evals", tags=["evals"])

SCHEDULE_LIMIT = 20
"""The most schedules a project has."""
LOOKBACK = 100
"""How many earlier slots of the history a slot's comparison looks through for the run it
compares with."""
NO_SCHEDULES = "This installation's worker does not run schedules"
NO_ACKNOWLEDGEMENT = "Model scoring requires cost acknowledgement"
COMPARISON_FIELDS = (
    "baseline_id",
    "candidate_id",
    "compatible",
    "reason",
    "coverage",
    "changes",
    "significance",
    "baseline_job_id",
    "candidate_job_id",
    "criteria",
    "criteria_significance",
)
"""What a slot's comparison keeps of the comparison route's answer: everything but the pairs and
the scorecards, which the run detail and Compare pages show."""

MONITORING = Depends(require_entitlement("monitoring"))
RUN = Depends(require_capability("run"))


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    suite_version_id: uuid.UUID
    agent_id: uuid.UUID
    dataset_name: str = Field(min_length=1, max_length=200)
    max_items: int = Field(default=100, ge=1, le=1000)
    every_seconds: int | None = None
    cron: str | None = Field(default=None, max_length=200)
    limits: RunLimits | None = None
    on_model_change: ModelWatch | None = None
    acknowledge_model_cost: bool = False

    @field_validator("cron")
    @classmethod
    def one_space(cls, cron: str | None) -> str | None:
        return None if cron is None else " ".join(cron.split())

    @model_validator(mode="after")
    def valid_timing(self) -> ScheduleRequest:
        slots.timing(every_seconds=self.every_seconds, cron=self.cron)
        if self.every_seconds is not None and self.every_seconds < slots.MIN_INTERVAL_SECONDS:
            raise ValueError(f"every_seconds is at least {slots.MIN_INTERVAL_SECONDS}")
        watch = self.on_model_change
        if watch is not None and watch.min_interval_seconds < slots.MIN_INTERVAL_SECONDS:
            raise ValueError(f"on_model_change.min_interval_seconds is at least {slots.MIN_INTERVAL_SECONDS}")
        return self


def _now() -> datetime:
    return datetime.now(UTC)


def schedule_out(schedule: Schedule, progress: ScheduleProgress | None) -> dict[str, Any]:
    return {
        "id": schedule.id,
        "project_id": project_out(schedule.project_id),
        "name": schedule.name,
        "state": schedule.state,
        "revision": schedule.revision,
        "every_seconds": schedule.every_seconds,
        "cron": schedule.cron,
        "timezone": "UTC",
        "next_slot": due_slot(schedule, progress) if schedule.state == "active" else None,
        "suite": {
            "id": str(suite_version_id(schedule.suite_id, schedule.suite_version)),
            "suite_id": schedule.suite_id,
            "version": schedule.suite_version,
            "name": schedule.suite_name,
        },
        "scorers": [
            {"name": s.name, "kind": s.kind, "version": s.version, "model": s.model, "endpoint": s.endpoint}
            for s in schedule.scorers
        ],
        "agent_id": schedule.target.agent_id,
        "dataset_name": schedule.dataset_name,
        "dataset_id": schedule.dataset_id,
        "dataset_digest": schedule.dataset_digest,
        "max_items": schedule.max_cases,
        "limits": schedule.limits.model_dump(mode="json") if schedule.limits else None,
        "on_model_change": schedule.on_model_change.model_dump(mode="json") if schedule.on_model_change else None,
        "active_since": schedule.active_since,
        "created_by": user_out(schedule.created_by) if schedule.created_by else None,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
        "progress": (progress or ScheduleProgress()).model_dump(mode="json"),
    }


async def _out(records: RecordStore, principal: Principal, schedule: Schedule) -> dict[str, Any]:
    return schedule_out(schedule, await records.schedule_progress(principal, schedule.project_id, schedule.id))


async def _find(store: EvalStore, records: RecordStore, principal: Principal, schedule_id: uuid.UUID) -> Schedule:
    for key in await project_keys(store, principal):
        if schedule := await records.get_schedule(principal, key, str(schedule_id)):
            return schedule
    raise HTTPException(404, "Schedule not found")


def _runs_schedules(worker: Worker) -> None:
    if not isinstance(worker, RunsSchedules) or worker.schedules is None:
        raise HTTPException(503, NO_SCHEDULES)


def _judging(worker: Worker, schedule: Schedule, acknowledged: bool = True) -> None:
    if any(s.kind == "llm_judge" for s in schedule.scorers):
        if not acknowledged:
            raise HTTPException(422, NO_ACKNOWLEDGEMENT)
        if not worker.judges:
            raise HTTPException(422, NO_JUDGE)


async def _submit(worker: Worker, principal: Principal, schedule: Schedule) -> None:
    work = Work(kind="schedule", principal=principal, project_id=schedule.project_id, schedule_id=schedule.id)
    await worker.submit(work)


def _same_request(schedule: Schedule, body: ScheduleRequest, project: str) -> bool:
    return (
        schedule.project_id == project
        and schedule.name == body.name
        and (schedule.every_seconds, schedule.cron) == (body.every_seconds, body.cron)
        and suite_version_id(schedule.suite_id, schedule.suite_version) == body.suite_version_id
        and schedule.target.agent_id == str(body.agent_id)
        and schedule.dataset_name == body.dataset_name
        and schedule.max_cases == body.max_items
        and schedule.limits == body.limits
        and schedule.on_model_change == body.on_model_change
    )


@router.get("/schedules", response_model=None, dependencies=[MONITORING])
async def list_schedules(
    principal: PrincipalDep, store: StoreDep, records: RecordsDep, project_id: uuid.UUID | None = None
) -> dict[str, Any]:
    key = project_key(project_id)
    if project_id is not None and await store.get_project(principal, key) is None:
        raise HTTPException(404, "Not found")
    return {"items": [await _out(records, principal, s) for s in await records.list_schedules(principal, key)]}


@router.post("/schedules", status_code=201, response_model=None, dependencies=[MONITORING, RUN])
async def create_schedule(
    body: ScheduleRequest,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    worker: WorkerDep,
    settings: JudgeSettingsDep,
) -> dict[str, Any]:
    _runs_schedules(worker)
    key = project_key(body.project_id)
    schedule_id = str(derived_id("schedule", str(body.request_id)))
    if existing := await records.get_schedule(principal, key, schedule_id):
        if not _same_request(existing, body, key):
            raise HTTPException(409, "request_id belongs to another schedule")
        return await _out(records, principal, existing)
    await writable_key(store, principal, key)
    suite = await find_suite_version(records, principal, body.suite_version_id)
    if suite is None:
        raise HTTPException(404, "Suite version not found")
    dataset = await store.find_dataset(principal, DEFAULT_PROJECT, body.dataset_name)
    if dataset is None:
        raise HTTPException(422, "Unknown dataset")
    target = Target(kind="agent", agent_id=str(body.agent_id))
    await worker.check_target(principal, target)  # an id the worker does not know is 404, as for a launch
    if len(await records.list_schedules(principal, key)) >= SCHEDULE_LIMIT:
        raise HTTPException(
            409, f"Delete a schedule before adding another ({SCHEDULE_LIMIT}-schedule limit per project)"
        )
    now = _now()
    schedule = Schedule(
        id=schedule_id,
        project_id=key,
        name=body.name,
        every_seconds=body.every_seconds,
        cron=body.cron,
        suite_id=suite.id,
        suite_version=suite.version,
        suite_name=suite.name,
        scorers=await with_models(settings, principal, list(suite.scorers)),
        policies=list(suite.policies),
        target=target,
        dataset_name=dataset.name,
        dataset_id=dataset.id,
        dataset_digest=dataset.digest,
        max_cases=body.max_items,
        limits=body.limits,
        on_model_change=body.on_model_change,
        active_since=now,
        created_by=principal.user_id,
        created_at=now,
    )
    _judging(worker, schedule, body.acknowledge_model_cost)
    schedule = await records.create_schedule(principal, key, schedule)
    try:
        await _submit(worker, principal, schedule)
    except Exception:
        # Not left behind with no worker to run it: the same request can create it again.
        await records.delete_schedule(principal, key, schedule.id)
        raise
    return await _out(records, principal, schedule)


@router.get("/schedules/{schedule_id}", response_model=None, dependencies=[MONITORING])
async def get_schedule(
    schedule_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    return await _out(records, principal, await _find(store, records, principal, schedule_id))


@router.get("/schedules/{schedule_id}/runs", response_model=None, dependencies=[MONITORING])
async def schedule_history(
    schedule_id: uuid.UUID,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    schedule = await _find(store, records, principal, schedule_id)
    window = await records.list_scheduled_runs(
        principal, schedule.project_id, schedule.id, limit=limit + LOOKBACK, cursor=str((page - 1) * limit)
    )
    entries = window.items
    history = _History(records, principal, schedule.project_id)
    items = [await history.entry(entry, entries[number + 1 :]) for number, entry in enumerate(entries[:limit])]
    return {"items": items, "page": page, "limit": limit, "has_more": len(entries) > limit}


@router.post("/schedules/{schedule_id}/pause", response_model=None, dependencies=[MONITORING, RUN])
async def pause_schedule(
    schedule_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    schedule = await _find(store, records, principal, schedule_id)
    if schedule.state == "active":
        paused = schedule.model_copy(update={"state": "paused"})
        schedule = await records.update_schedule(
            principal, paused.project_id, paused, expected_revision=paused.revision
        )
    return await _out(records, principal, schedule)


@router.post("/schedules/{schedule_id}/resume", response_model=None, dependencies=[MONITORING, RUN])
async def resume_schedule(
    schedule_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep, worker: WorkerDep
) -> dict[str, Any]:
    _runs_schedules(worker)
    schedule = await _find(store, records, principal, schedule_id)
    if schedule.state == "paused":
        await writable_key(store, principal, schedule.project_id)
        _judging(worker, schedule)
        active = schedule.model_copy(update={"state": "active", "active_since": _now()})
        schedule = await records.update_schedule(
            principal, active.project_id, active, expected_revision=active.revision
        )
    # Its row stays in the queue while it exists; this puts it back if it was ever lost.
    await _submit(worker, principal, schedule)
    return await _out(records, principal, schedule)


@router.delete("/schedules/{schedule_id}", status_code=204, dependencies=[MONITORING, RUN])
async def delete_schedule(
    schedule_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> Response:
    schedule = await _find(store, records, principal, schedule_id)
    await records.delete_schedule(principal, schedule.project_id, schedule.id)
    return Response(status_code=204)


# --- the history ------------------------------------------------------------------------------


class _History:
    """A schedule's slots as its history route answers them, each run read once per request."""

    def __init__(self, records: RecordStore, principal: Principal, project: str) -> None:
        self._records, self._principal, self._project = records, principal, project
        self._runs: dict[str, Run | None] = {}
        self._results: dict[str, list[CaseResult]] = {}
        self._scores: dict[str, list[Score]] = {}

    async def run(self, run_id: str) -> Run | None:
        if run_id not in self._runs:
            self._runs[run_id] = await self._records.get_run(self._principal, self._project, run_id)
        return self._runs[run_id]

    async def results(self, run: Run) -> list[CaseResult]:
        if run.id not in self._results:
            self._results[run.id] = await run_results(self._records, self._principal, run)
        return self._results[run.id]

    async def suite_job(self, run: Run) -> tuple[ScoringJob | None, list[Assessment]]:
        """The run's suite scoring job and its scores."""
        jobs = await self._records.list_scoring_jobs(self._principal, self._project, run.id)
        job = next((j for j in jobs if j.id == suite_job_id(run.id)), None)
        if job is None:
            return None, []
        if run.id not in self._scores:
            self._scores[run.id] = await self._records.list_scores(self._principal, self._project, run.id)
        return job, [Assessment.of(s) for s in self._scores[run.id] if s.scoring_job_id == job.id]

    async def entry(self, entry: ScheduledRun, earlier: Sequence[ScheduledRun]) -> dict[str, Any]:
        # The work's module imports the routes' projection, so it is imported when a history needs it.
        from martian.evals.worker.execute import run_model

        change = entry.model_change
        found: dict[str, Any] = {
            "slot": entry.slot,
            "missed": entry.missed,
            "fired_at": entry.created_at,
            "trigger": entry.trigger,
            "model_change": {"from": change.previous, "to": change.current} if change else None,
            "note": change.note if change else "",
            "state": entry.state,
            "reason": entry.reason,
            "error": "",
            "run_id": entry.run_id,
            "run_code": None,
            "model": None,
            "pass_rate": None,
            "counts": None,
            "scoring": None,
            "gate": entry.gate,
            "comparison": None,
        }
        run = await self.run(entry.run_id) if entry.run_id else None
        if run is None:
            return found
        counts = run.counts
        graded = counts.passed + counts.failed
        found |= {
            "state": run.state,
            "error": run.error,
            "run_code": run.code,
            "model": run_model(run),
            "pass_rate": counts.passed / graded if graded else None,
            "counts": {
                "passed": counts.passed,
                "failed": counts.failed,
                "skipped": counts.skipped,
                "errored": counts.errors,
            },
        }
        items = [item_out(r) for r in await self.results(run)]
        job, scores = await self.suite_job(run)
        if job is not None:
            card = scorecard(items, scores, job_config(job))
            found["scoring"] = {
                "job_id": job.id,
                "state": job.state,
                "error": job.error,
                "gate": card["gate"],
                "criteria": [
                    {key: criterion[key] for key in ("name", "kind", "pass_rate", "mean", "gate")}
                    for criterion in card["criteria"]
                ],
            }
        if run.state == "completed":
            for older in earlier:
                baseline = await self.run(older.run_id) if older.run_id else None
                if baseline is not None and baseline.state == "completed":
                    found["comparison"] = await self.compare(baseline, run)
                    break
        return found

    async def compare(self, baseline: Run, candidate: Run) -> dict[str, Any]:
        """The paired comparison of two of the schedule's runs, as ``GET /compare`` makes it: by
        exact match, and by each of the suite's scorers once both suite jobs have finished."""
        left = [item_out(r) for r in await self.results(baseline)]
        right = [item_out(r) for r in await self.results(candidate)]
        result = compare_runs(baseline, left, candidate, right)
        (left_job, left_scores), (right_job, right_scores) = (
            await self.suite_job(baseline),
            await self.suite_job(candidate),
        )
        if (
            left_job is not None
            and right_job is not None
            and left_job.state not in ("queued", "running")
            and right_job.state not in ("queued", "running")
        ):
            result = compare_assessments(
                result, left, right, public_job(left_job), public_job(right_job), left_scores, right_scores
            )
        return {key: result[key] for key in COMPARISON_FIELDS if key in result}


__all__ = ["SCHEDULE_LIMIT", "ScheduleRequest", "router", "schedule_out"]
