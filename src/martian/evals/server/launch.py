"""Launching and cancelling a run: a port of ``POST /v1/evals`` and ``POST /v1/evals/{run_id}/cancel``
in the server's ``app/api/evals.py`` (MorningHQ/MartianBackend).

A launch admits the run and returns at once. It checks the request, asks the worker whether it can
call the run's target, records the run as ``queued`` with the published dataset version it will
read, and submits it to the worker. The worker calls the target later; the request never does.

- ``request_id`` identifies the request: the run's id is derived from it, so sending the same
  request again returns the same run, and the same ``request_id`` with other parameters is ``409``.
- One run at a time per organisation: another queued or running run is ``409``.
- Run codes count up from ``EV-1001``, as on the server.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from martian.evals.manifests import PAUSED, publishing
from martian.evals.model import Run, RunState, StoreRef, Target
from martian.evals.records import RecordStore
from martian.evals.server.dependencies import (
    AgentNamesDep,
    PrincipalDep,
    RecordsDep,
    StoreDep,
    WorkerDep,
    require_capability,
)
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id, project_key, run_out
from martian.evals.server.runs import (
    agent_names,
    find_run,
    lookup_run,
    name_of,
    project_keys,
    project_runs,
    writable_project,
)
from martian.evals.server.schemas import EvalRunOut
from martian.evals.server.write_schemas import EvalRunRequest
from martian.evals.store import EvalStore, StoreError
from martian.evals.worker import Work
from martian.evals.worker.scorers import builtin_scorer

router = APIRouter(prefix="/v1/evals", tags=["evals"])

RUN_CODE = re.compile(r"^EV-(\d+)$")
FIRST_CODE = 1001
ACTIVE: tuple[RunState, ...] = ("queued", "running")


def _now() -> datetime:
    return datetime.now(UTC)


def run_id_for(request_id: uuid.UUID) -> str:
    return str(derived_id("run", str(request_id)))


async def _next_code(store: EvalStore, records: RecordStore, principal: Principal) -> str:
    highest = FIRST_CODE - 1
    for key in await project_keys(store, principal):
        for run in await project_runs(records, principal, key):
            if match := RUN_CODE.match(run.code):
                highest = max(highest, int(match[1]))
    return f"EV-{highest + 1}"


async def _active(store: EvalStore, records: RecordStore, principal: Principal) -> bool:
    for key in await project_keys(store, principal):
        if (await records.list_runs(principal, key, states=list(ACTIVE), limit=1)).items:
            return True
    return False


def _same_request(run: Run, body: EvalRunRequest, principal: Principal) -> bool:
    return (
        run.target is not None
        and run.target.agent_id == str(body.agent_id)
        and run.dataset_name == body.dataset_name
        and run.project_id == project_key(body.project_id)
        and run.max_cases == body.max_items
        and run.requested_by == principal.user_id
    )


@router.post("", response_model=EvalRunOut, status_code=202, dependencies=[Depends(require_capability("run"))])
async def request_run(
    body: EvalRunRequest,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    worker: WorkerDep,
    names: AgentNamesDep,
) -> EvalRunOut:
    run_id = run_id_for(body.request_id)
    if existing := await lookup_run(store, records, principal, run_id):
        if not _same_request(existing, body, principal):
            raise HTTPException(409, "request_id was already used for a different eval")
        return run_out(existing, name_of(await agent_names(names, principal, [existing]), existing))
    project = await writable_project(store, principal, body.project_id)
    target = Target(kind="agent", agent_id=str(body.agent_id))
    pin = await worker.check_target(principal, target)
    dataset = await store.find_dataset(principal, DEFAULT_PROJECT, body.dataset_name)
    if dataset is None:
        raise HTTPException(422, "Unknown dataset")
    if await publishing(records, principal, DEFAULT_PROJECT, _now()):
        raise HTTPException(409, PAUSED)
    if await _active(store, records, principal):
        raise HTTPException(409, "An eval run is already in progress")
    run = Run(
        id=run_id,
        project_id=project,
        code=await _next_code(store, records, principal),
        source="console",
        state="queued",
        dataset_name=dataset.name,
        dataset_digest=dataset.digest,
        target=target,
        scorers=[builtin_scorer("exact_match")],
        max_cases=body.max_items,
        provenance={
            "dataset": {"id": dataset.id, "digest": dataset.digest, "content_sha256": None},
            "agent": {"id": str(body.agent_id), "artifact_revision": None, "revision_source": "unavailable"},
            "model_version": None,
            "prompt_version": None,
            "target": pin,
        },
        requested_by=principal.user_id,
        created_at=_now(),
        store=StoreRef(dataset_id=dataset.id),
    )
    run = await records.create_run(principal, run)
    await worker.submit(Work(kind="run", principal=principal, project_id=project, run_id=run.id))
    return run_out(run, name_of(await agent_names(names, principal, [run]), run))


@router.post(
    "/{run_id:uuid}/cancel",
    response_model=EvalRunOut,
    status_code=202,
    dependencies=[Depends(require_capability("run"))],
)
async def cancel_eval(
    run_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep, names: AgentNamesDep
) -> EvalRunOut:
    # A queued run is cancelled at once; a running one when its worker reaches the next case.
    run = await find_run(store, records, principal, run_id)
    named = await agent_names(names, principal, [run])
    for _ in range(3):
        if run.cancel_requested_at is not None:
            return run_out(run, name_of(named, run))
        if run.state not in ACTIVE or run.finished_at is not None:
            raise HTTPException(409, "Execution has finished; its recorded results remain available")
        now = _now()
        changes: dict[str, object] = {"cancel_requested_at": now}
        if run.state == "queued":
            changes |= {"state": "cancelled", "finished_at": now}
        try:
            stopped = await records.update_run(principal, run.model_copy(update=changes), expected_state=run.state)
            return run_out(stopped, name_of(named, stopped))
        except StoreError as error:
            if error.kind != "conflict":
                raise
        run = await find_run(store, records, principal, run_id)
    raise HTTPException(409, "The run changed while stopping it; try again")


__all__ = ["router", "run_id_for"]
