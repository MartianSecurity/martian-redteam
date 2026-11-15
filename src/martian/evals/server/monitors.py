"""Monitors: judges and deterministic checks attached to a project's live traces
(MartianSecurity/martian-notes#196). The core's own routes; the server's frozen contract has none.

A monitor names its scorers as a scoring request does (the console's scorers by name, registered
judges by name and version), a sampling rate from 0 to 1, a filter of the trace search's, and a
daily judge token cap. Creating one records it and hands its work to the worker, which scores
matching new traces in passes (:mod:`martian.evals.worker.monitor`); the request calls no model.

- Monitoring is a paid feature: every route needs the edition's ``monitoring`` key, as the
  Monitoring page does, and the routes that change a monitor the ``score`` capability.
- The worker must run monitors (``RunsMonitors``, a :class:`~martian.evals.worker.QueueWorker`),
  or creating and resuming one is ``503``; judges need a worker with a judge (``422``).
- A production trace has no expected response, so the scorers that compare with one
  (``exact_match``, ``contains_expected``, ``correctness``) are refused, and a monitor with judges
  needs a daily judge token cap.
- Each judge is pinned to its model when the monitor is created, the default judge model for one
  that names none, as a scoring job's are.
- The id is derived from ``request_id``: the same request again returns the same monitor. A
  project has at most :data:`MONITOR_LIMIT` monitors.
- Pausing stops the monitor before its next trace; resuming scores the traces from then on, not
  the ones recorded while it was paused. Deleting removes it and its progress; the scores it
  stored stay on their traces.
- ``GET /monitors/{id}/trends`` is each scorer's pass rate or mean in consecutive windows, with its
  95% interval (:mod:`martian.evals.trends`), read from the monitor's record of the traces it
  scored, by time and bounded as a threshold's window is: never the project's traces.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the core's routes follow the contract's style.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from martian.evals.model import ScorerVersion
from martian.evals.records import THRESHOLD_WINDOW_BOUND, Monitor, MonitorFilter, MonitorProgress, RecordStore
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
from martian.evals.server.projection import derived_id, project_key, project_out, user_out
from martian.evals.server.runs import project_keys, writable_key
from martian.evals.server.scoring import with_models
from martian.evals.server.thresholds import MEAN_ONLY, PASS_RATE_ONLY
from martian.evals.server.write_schemas import JudgeReference, ScorerName
from martian.evals.store import EvalStore
from martian.evals.trends import TREND_POINTS, Measure, monitor_trends, trend_windows
from martian.evals.worker import RunsMonitors, Work, Worker
from martian.evals.worker.scorers import CODE_SCORERS, JUDGE_SCORERS, builtin_scorer

router = APIRouter(prefix="/v1/evals", tags=["evals"])

MONITOR_LIMIT = 20
"""The most monitors a project has."""
NEEDS_EXPECTED = ("exact_match", "contains_expected", "correctness")
"""Scorers that compare an output with an expected response, which a production trace has none of."""
NO_MONITORS = "This installation's worker does not run monitors"

MONITORING = Depends(require_entitlement("monitoring"))
SCORE = Depends(require_capability("score"))


class MonitorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    scorers: list[ScorerName] = Field(default_factory=list, max_length=7)
    judges: list[JudgeReference] = Field(default_factory=list, max_length=7)
    guidelines: str = Field(default="", max_length=4000)
    sampling_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    filter: MonitorFilter = MonitorFilter()
    max_judge_tokens_per_day: int | None = Field(default=None, ge=1)
    acknowledge_model_cost: bool = False

    @model_validator(mode="after")
    def valid_scorers(self) -> MonitorRequest:
        names = [j.name for j in self.judges]
        if (
            len(set(self.scorers)) != len(self.scorers)
            or not 1 <= len(self.scorers) + len(names) <= 7
            or len(set(names)) != len(names)
            or set(names) & {*CODE_SCORERS, *JUDGE_SCORERS}
        ):
            raise ValueError("Choose one to seven unique criteria")
        if needs := [name for name in self.scorers if name in NEEDS_EXPECTED]:
            raise ValueError(
                f"{', '.join(needs)} compares with an expected response, which a production trace does not have"
            )
        if "policy" in self.scorers and not self.guidelines.strip():
            raise ValueError("Policy scoring requires guidelines")
        if "policy" not in self.scorers and self.guidelines:
            raise ValueError("Guidelines require the policy scorer")
        if set(JUDGE_SCORERS) & set(self.scorers) or self.judges:
            if not self.acknowledge_model_cost:
                raise ValueError("Model scoring requires cost acknowledgement")
            if self.max_judge_tokens_per_day is None:
                raise ValueError("A monitor with judges needs a daily judge token cap (max_judge_tokens_per_day)")
        return self


def _now() -> datetime:
    return datetime.now(UTC)


def monitor_out(monitor: Monitor, progress: MonitorProgress | None) -> dict[str, Any]:
    return {
        "id": monitor.id,
        "project_id": project_out(monitor.project_id),
        "name": monitor.name,
        "state": monitor.state,
        "revision": monitor.revision,
        "sampling_rate": monitor.sampling_rate,
        "filter": monitor.filter.model_dump(mode="json"),
        "max_judge_tokens_per_day": monitor.max_judge_tokens_per_day,
        "scorers": [
            {
                "name": s.name,
                "kind": s.kind,
                "type": s.type,
                "version": s.version,
                "model": s.model,
                "endpoint": s.endpoint,
                "guidelines": s.criteria.get("guidelines") if s.name == "policy" else None,
            }
            for s in monitor.scorers
        ],
        "active_since": monitor.active_since,
        "created_by": user_out(monitor.created_by) if monitor.created_by else None,
        "created_at": monitor.created_at,
        "updated_at": monitor.updated_at,
        "progress": (progress or MonitorProgress()).model_dump(mode="json"),
    }


async def _out(records: RecordStore, principal: Principal, monitor: Monitor) -> dict[str, Any]:
    return monitor_out(monitor, await records.monitor_progress(principal, monitor.project_id, monitor.id))


async def _find(store: EvalStore, records: RecordStore, principal: Principal, monitor_id: uuid.UUID) -> Monitor:
    for key in await project_keys(store, principal):
        if monitor := await records.get_monitor(principal, key, str(monitor_id)):
            return monitor
    raise HTTPException(404, "Monitor not found")


async def _judges(store: EvalStore, principal: Principal, project: str, body: MonitorRequest) -> list[ScorerVersion]:
    pinned = []
    for reference in body.judges:
        versions = await store.scorer_versions(principal, project, reference.name)
        found = next((v for v in versions if v.version == str(reference.version) and v.kind == "llm_judge"), None)
        if found is None:
            raise HTTPException(404, "Judge version not found")
        pinned.append(found)
    return pinned


def _runs_monitors(worker: Worker) -> None:
    if not isinstance(worker, RunsMonitors):
        raise HTTPException(503, NO_MONITORS)


def _judging(worker: Worker, scorers: list[ScorerVersion]) -> None:
    if any(s.kind == "llm_judge" for s in scorers) and not worker.judges:
        raise HTTPException(422, "Model judges require a judge configured for this installation's worker")


async def _submit(worker: Worker, principal: Principal, monitor: Monitor) -> None:
    await worker.submit(Work(kind="monitor", principal=principal, project_id=monitor.project_id, monitor_id=monitor.id))


@router.get("/monitors", response_model=None, dependencies=[MONITORING])
async def list_monitors(
    principal: PrincipalDep, store: StoreDep, records: RecordsDep, project_id: uuid.UUID | None = None
) -> dict[str, Any]:
    key = project_key(project_id)
    if project_id is not None and await store.get_project(principal, key) is None:
        raise HTTPException(404, "Not found")
    return {"items": [await _out(records, principal, m) for m in await records.list_monitors(principal, key)]}


@router.post("/monitors", status_code=201, response_model=None, dependencies=[MONITORING, SCORE])
async def create_monitor(
    body: MonitorRequest,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    worker: WorkerDep,
    settings: JudgeSettingsDep,
) -> dict[str, Any]:
    _runs_monitors(worker)
    key = project_key(body.project_id)
    monitor_id = str(derived_id("monitor", str(body.request_id)))
    scorers = [builtin_scorer(name, guidelines=body.guidelines) for name in sorted(body.scorers)]
    scorers = sorted(scorers + await _judges(store, principal, key, body), key=lambda s: s.name)
    asked = (body.name, body.sampling_rate, body.filter, body.max_judge_tokens_per_day)
    if existing := await records.get_monitor(principal, key, monitor_id):
        kept = (existing.name, existing.sampling_rate, existing.filter, existing.max_judge_tokens_per_day)
        if kept != asked or [(s.name, s.version) for s in existing.scorers] != [(s.name, s.version) for s in scorers]:
            raise HTTPException(409, "request_id belongs to another monitor")
        return await _out(records, principal, existing)
    await writable_key(store, principal, key)
    _judging(worker, scorers)
    if len(await records.list_monitors(principal, key)) >= MONITOR_LIMIT:
        raise HTTPException(409, f"Delete a monitor before adding another ({MONITOR_LIMIT}-monitor limit per project)")
    now = _now()
    monitor = await records.create_monitor(
        principal,
        key,
        Monitor(
            id=monitor_id,
            project_id=key,
            name=body.name,
            scorers=await with_models(settings, principal, scorers),
            sampling_rate=body.sampling_rate,
            filter=body.filter,
            max_judge_tokens_per_day=body.max_judge_tokens_per_day,
            active_since=now,
            created_by=principal.user_id,
            created_at=now,
        ),
    )
    try:
        await _submit(worker, principal, monitor)
    except Exception:
        # Not left behind with no worker to run it: the same request can create it again.
        await records.delete_monitor(principal, key, monitor.id)
        raise
    return await _out(records, principal, monitor)


@router.get("/monitors/{monitor_id}", response_model=None, dependencies=[MONITORING])
async def get_monitor(
    monitor_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    return await _out(records, principal, await _find(store, records, principal, monitor_id))


def scorer_measure(scorer: ScorerVersion) -> Measure | None:
    """How a trend measures a scorer's values, as a threshold on it may: a built-in check or
    judge by the measure it has, a registered judge by the kind of its answers, ``pass_rate`` for
    yes or no and ``mean`` for a number. None for a judge whose answers are neither."""
    if scorer.name in PASS_RATE_ONLY:
        return "pass_rate"
    if scorer.name in MEAN_ONLY:
        return "mean"
    kind = scorer.criteria.get("kind", "boolean")
    return "pass_rate" if kind == "boolean" else "mean" if kind == "numeric" else None


@router.get("/monitors/{monitor_id}/trends", response_model=None, dependencies=[MONITORING])
async def monitor_trend(
    monitor_id: uuid.UUID,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    span_seconds: Annotated[int, Query(ge=60, le=30 * 86400)] = 86400,
    bucket_seconds: Annotated[int, Query(ge=60, le=86400)] = 3600,
) -> dict[str, Any]:
    if math.ceil(span_seconds / bucket_seconds) > TREND_POINTS:
        raise HTTPException(
            422, f"A trend has at most {TREND_POINTS} points: widen bucket_seconds or shorten span_seconds"
        )
    monitor = await _find(store, records, principal, monitor_id)
    now = _now()
    windows = trend_windows(now, span_seconds, bucket_seconds)
    rows = await records.scored_monitored_traces(
        principal, monitor.project_id, monitor.id, since=windows[0][0], until=now, limit=THRESHOLD_WINDOW_BOUND
    )
    measures = {scorer.name: scorer_measure(scorer) for scorer in monitor.scorers}
    return {"monitor_id": monitor.id, "bucket_seconds": bucket_seconds, **monitor_trends(rows, measures, windows)}


@router.post("/monitors/{monitor_id}/pause", response_model=None, dependencies=[MONITORING, SCORE])
async def pause_monitor(
    monitor_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    monitor = await _find(store, records, principal, monitor_id)
    if monitor.state == "active":
        paused = monitor.model_copy(update={"state": "paused"})
        monitor = await records.update_monitor(principal, paused.project_id, paused, expected_revision=paused.revision)
    return await _out(records, principal, monitor)


@router.post("/monitors/{monitor_id}/resume", response_model=None, dependencies=[MONITORING, SCORE])
async def resume_monitor(
    monitor_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep, worker: WorkerDep
) -> dict[str, Any]:
    _runs_monitors(worker)
    monitor = await _find(store, records, principal, monitor_id)
    if monitor.state == "paused":
        await writable_key(store, principal, monitor.project_id)
        _judging(worker, monitor.scorers)
        active = monitor.model_copy(update={"state": "active", "active_since": _now()})
        monitor = await records.update_monitor(principal, active.project_id, active, expected_revision=active.revision)
    # Its row stays in the queue while it exists; this puts it back if it was ever lost.
    await _submit(worker, principal, monitor)
    return await _out(records, principal, monitor)


@router.delete("/monitors/{monitor_id}", status_code=204, dependencies=[MONITORING, SCORE])
async def delete_monitor(
    monitor_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> Response:
    monitor = await _find(store, records, principal, monitor_id)
    await records.delete_monitor(principal, monitor.project_id, monitor.id)
    return Response(status_code=204)


__all__ = ["MONITOR_LIMIT", "MonitorRequest", "monitor_out", "router"]
