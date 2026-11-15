"""Thresholds on a monitor's scores and the alerts their breaches and recoveries send
(MorningHQ/martian-notes#198). The core's own routes; the server's frozen contract has none.

A threshold names its source, a measure (``pass_rate`` of boolean scores or ``mean`` of numeric
ones) that is healthy ``at_least`` or ``at_most`` a value, a window, and the fewest scores it is
judged on. Its source is a monitor and one of its scorers (``kind="monitor"``), which the worker
evaluates after each of the monitor's passes, or a schedule's gate results (``kind="gate"``,
MorningHQ/martian-notes#197), measured by their ``pass_rate`` and evaluated after each of the
schedule's runs is scored. Each evaluation is on the 95% interval of the window's scores
(:mod:`martian.evals.thresholds`) and sends one alert per breach and one per recovery to its
webhook, its email recipients, or both (:mod:`martian.evals.alerts`).

- Thresholds are part of monitoring: every route needs the edition's ``monitoring`` key, and the
  routes that change one the ``score`` capability, as the monitor routes do.
- A webhook or email channel needs a worker that sends it (``SendsAlerts``); an installation sends
  neither unless it is configured to, so either is ``422`` until then.
- A webhook's ``secret`` is written once, with the threshold, and no route returns it: a
  threshold shows ``"signed": true`` in its place. To change it, delete the threshold and create
  it again.
- The id is derived from ``request_id``: the same request again returns the same threshold. A
  project has at most :data:`THRESHOLD_LIMIT` thresholds. Deleting the monitor or the schedule
  deletes its thresholds and their alerts.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the core's routes follow the contract's style.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from martian.evals.alerts import NO_EMAIL, NO_WEBHOOKS, webhook_url_problem
from martian.evals.records import (
    EmailChannel,
    MonitorScores,
    RecordStore,
    ScheduleGates,
    Threshold,
    ThresholdStatus,
    WebhookChannel,
    threshold_problem,
)
from martian.evals.server.dependencies import (
    OptionalWorkerDep,
    PrincipalDep,
    RecordsDep,
    StoreDep,
    require_capability,
    require_entitlement,
)
from martian.evals.server.principal import Principal
from martian.evals.server.projection import derived_id, project_key, project_out, user_out
from martian.evals.server.runs import project_keys, writable_key
from martian.evals.store import EvalStore
from martian.evals.worker import alert_channels

router = APIRouter(prefix="/v1/evals", tags=["evals"])

THRESHOLD_LIMIT = 50
"""The most thresholds a project has."""
PASS_RATE_ONLY = frozenset({"valid_json", "safety", "policy"})
MEAN_ONLY = frozenset({"response_length"})

MONITORING = Depends(require_entitlement("monitoring"))
SCORE = Depends(require_capability("score"))


class WebhookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    secret: str = Field(min_length=16, max_length=256)

    @field_validator("url")
    @classmethod
    def usable(cls, url: str) -> str:
        if (problem := webhook_url_problem(url)) is not None:
            raise ValueError(problem)
        return url

    @field_validator("secret")
    @classmethod
    def printable(cls, secret: str) -> str:
        if not secret.isascii() or not secret.isprintable() or secret != secret.strip():
            raise ValueError("A webhook secret is printable ASCII with no space at either end")
        return secret


class SourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["monitor"] = "monitor"
    monitor_id: uuid.UUID
    scorer: str = Field(min_length=1, max_length=120)


class GateSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["gate"]
    schedule_id: uuid.UUID


class ThresholdRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    source: SourceRequest | GateSourceRequest
    measure: Literal["pass_rate", "mean"]
    direction: Literal["at_least", "at_most"] = "at_least"
    value: float = Field(allow_inf_nan=False)
    window_seconds: int = Field(default=3600, ge=60, le=30 * 86400)
    min_count: int = Field(default=30, ge=1, le=5000)
    webhook: WebhookRequest | None = None
    email: EmailChannel | None = None

    @model_validator(mode="after")
    def decidable(self) -> ThresholdRequest:
        if (problem := threshold_problem(self.measure, self.value, self.min_count)) is not None:
            raise ValueError(problem)
        if any(ord(c) < 32 or ord(c) == 127 for c in self.name):
            raise ValueError("A threshold's name is one line of text")
        return self


def threshold_out(threshold: Threshold, status: ThresholdStatus | None) -> dict[str, Any]:
    """A threshold as a route returns it: its webhook's secret is never in it, only that it signs."""
    return {
        "id": threshold.id,
        "project_id": project_out(threshold.project_id),
        "name": threshold.name,
        "source": threshold.source.model_dump(mode="json"),
        "measure": threshold.measure,
        "direction": threshold.direction,
        "value": threshold.value,
        "window_seconds": threshold.window_seconds,
        "min_count": threshold.min_count,
        "webhook": None if threshold.webhook is None else {"url": threshold.webhook.url, "signed": True},
        "email": None if threshold.email is None else {"to": list(threshold.email.to)},
        "status": (status or ThresholdStatus()).model_dump(mode="json"),
        "created_by": user_out(threshold.created_by) if threshold.created_by else None,
        "created_at": threshold.created_at,
    }


async def _out(records: RecordStore, principal: Principal, threshold: Threshold) -> dict[str, Any]:
    return threshold_out(threshold, await records.threshold_status(principal, threshold.project_id, threshold.id))


async def _find(store: EvalStore, records: RecordStore, principal: Principal, threshold_id: uuid.UUID) -> Threshold:
    for key in await project_keys(store, principal):
        if threshold := await records.get_threshold(principal, key, str(threshold_id)):
            return threshold
    raise HTTPException(404, "Threshold not found")


def _definition(threshold: Threshold) -> tuple[Any, ...]:
    """What a repeated request must ask again to get the same threshold back."""
    return (
        threshold.name,
        threshold.source,
        threshold.measure,
        threshold.direction,
        threshold.value,
        threshold.window_seconds,
        threshold.min_count,
        threshold.webhook,
        threshold.email,
    )


async def _source(
    records: RecordStore, principal: Principal, key: str, body: ThresholdRequest
) -> MonitorScores | ScheduleGates:
    """The source a request names, checked: a monitor and one of its scorers, measured as its
    scores can be, or a schedule's gate results, measured by their pass rate."""
    if isinstance(body.source, GateSourceRequest):
        schedule = await records.get_schedule(principal, key, str(body.source.schedule_id))
        if schedule is None:
            raise HTTPException(404, "Schedule not found")
        if body.measure != "pass_rate":
            raise HTTPException(422, "A schedule's gate results are measured by their pass_rate")
        return ScheduleGates(schedule_id=schedule.id)
    monitor = await records.get_monitor(principal, key, str(body.source.monitor_id))
    if monitor is None:
        raise HTTPException(404, "Monitor not found")
    if body.source.scorer not in {s.name for s in monitor.scorers}:
        raise HTTPException(422, "The monitor has no scorer of that name")
    if (body.source.scorer in PASS_RATE_ONLY and body.measure != "pass_rate") or (
        body.source.scorer in MEAN_ONLY and body.measure != "mean"
    ):
        wanted = "pass_rate" if body.source.scorer in PASS_RATE_ONLY else "mean"
        raise HTTPException(422, f"{body.source.scorer} scores are measured by their {wanted}")
    return MonitorScores(monitor_id=monitor.id, scorer=body.source.scorer)


@router.get("/thresholds", response_model=None, dependencies=[MONITORING])
async def list_thresholds(
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    project_id: uuid.UUID | None = None,
    monitor_id: uuid.UUID | None = None,
    schedule_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    key = project_key(project_id)
    if project_id is not None and await store.get_project(principal, key) is None:
        raise HTTPException(404, "Not found")
    if monitor_id is not None and schedule_id is not None:
        raise HTTPException(422, "Name one source, a monitor or a schedule")
    if monitor_id is not None:
        found = await records.list_thresholds(principal, key, source_kind="monitor", source_id=str(monitor_id))
    elif schedule_id is not None:
        found = await records.list_thresholds(principal, key, source_kind="gate", source_id=str(schedule_id))
    else:
        found = await records.list_thresholds(principal, key)
    return {"items": [await _out(records, principal, t) for t in found]}


@router.post("/thresholds", status_code=201, response_model=None, dependencies=[MONITORING, SCORE])
async def create_threshold(
    body: ThresholdRequest, principal: PrincipalDep, store: StoreDep, records: RecordsDep, worker: OptionalWorkerDep
) -> dict[str, Any]:
    sends = alert_channels(worker)
    if body.webhook is not None and "webhook" not in sends:
        raise HTTPException(422, f"{NO_WEBHOOKS}: its worker's alert settings name no webhook")
    if body.email is not None and "email" not in sends:
        raise HTTPException(422, f"{NO_EMAIL}: its worker's alert settings name no SMTP server")
    key = project_key(body.project_id)
    source = await _source(records, principal, key, body)
    threshold = Threshold(
        id=str(derived_id("threshold", str(body.request_id))),
        project_id=key,
        name=body.name,
        source=source,
        measure=body.measure,
        direction=body.direction,
        value=body.value,
        window_seconds=body.window_seconds,
        min_count=body.min_count,
        webhook=None if body.webhook is None else WebhookChannel(url=body.webhook.url),
        email=body.email,
        created_by=principal.user_id,
    )
    if existing := await records.get_threshold(principal, key, threshold.id):
        if _definition(existing) != _definition(threshold):
            raise HTTPException(409, "request_id belongs to another threshold")
        return await _out(records, principal, existing)
    await writable_key(store, principal, key)
    if len(await records.list_thresholds(principal, key)) >= THRESHOLD_LIMIT:
        raise HTTPException(
            409, f"Delete a threshold before adding another ({THRESHOLD_LIMIT}-threshold limit per project)"
        )
    secret = None if body.webhook is None else body.webhook.secret
    made = await records.create_threshold(principal, key, threshold, webhook_secret=secret)
    return await _out(records, principal, made)


@router.get("/thresholds/{threshold_id}", response_model=None, dependencies=[MONITORING])
async def get_threshold(
    threshold_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    return await _out(records, principal, await _find(store, records, principal, threshold_id))


@router.get("/thresholds/{threshold_id}/alerts", response_model=None, dependencies=[MONITORING])
async def list_alerts(
    threshold_id: uuid.UUID,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> dict[str, Any]:
    threshold = await _find(store, records, principal, threshold_id)
    page = await records.list_alerts(principal, threshold.project_id, threshold.id, limit=limit, cursor=cursor)
    return {"items": [a.model_dump(mode="json") for a in page.items], "next_cursor": page.next_cursor}


@router.delete("/thresholds/{threshold_id}", status_code=204, dependencies=[MONITORING, SCORE])
async def delete_threshold(
    threshold_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> Response:
    threshold = await _find(store, records, principal, threshold_id)
    await records.delete_threshold(principal, threshold.project_id, threshold.id)
    return Response(status_code=204)


__all__ = ["THRESHOLD_LIMIT", "ThresholdRequest", "router", "threshold_out"]
