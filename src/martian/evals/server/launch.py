"""Launching and cancelling a run: a port of ``POST /v1/evals`` and ``POST /v1/evals/{run_id}/cancel``
in the server's ``app/api/evals.py`` (MorningHQ/MartianBackend).

A launch admits the run and returns at once. It checks the request, asks the worker whether it can
call the run's target, records the run as ``queued`` with the published dataset version it will
read, and submits it to the worker. The worker calls the target later; the request never does.

- ``request_id`` identifies the request: the run's id is derived from it, so sending the same
  request again returns the same run, and the same ``request_id`` with other parameters is ``409``.
- One run at a time per organisation: another queued or running run is ``409``.
- Run codes count up from ``EV-1001``, as on the server.

:func:`admit_run` is the admission itself, which the local edition's launch and run again share
(:mod:`martian.evals.local_runs`).

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from martian.evals.model import DatasetVersion, Run, RunState, StoreRef, Suite, Target
from martian.evals.records import RecordStore
from martian.evals.server.dependencies import (
    PrincipalDep,
    RecordsDep,
    StoreDep,
    WorkerDep,
    require_capability,
)
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id, project_key, run_out, suite_version_id
from martian.evals.server.runs import find_run, lookup_run, project_keys, project_runs, writable_project
from martian.evals.server.schemas import EvalRunOut
from martian.evals.server.write_schemas import EvalRunRequest
from martian.evals.store import EvalStore, StoreError
from martian.evals.worker import Work, Worker
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


async def admit_run(
    store: EvalStore,
    records: RecordStore,
    worker: Worker,
    principal: Principal,
    *,
    run_id: str,
    project: str,
    target: Target,
    pin: dict[str, Any],
    dataset: DatasetVersion,
    max_cases: int,
    suite: Suite | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> Run:
    """Record a queued run of ``target`` over the published ``dataset`` version and submit it to
    the worker, once the request is checked and the worker has pinned the target (``pin``). One
    run at a time per organisation: another queued or running run is ``409``.

    A launch admits every run this way. The local edition's launch also names a saved ``suite``
    version, which the run keeps (``suite_id``, ``suite_version``, ``suite_digest`` and
    ``provenance.suite``), and adds its own ``provenance``."""
    if await _active(store, records, principal):
        raise HTTPException(409, "An eval run is already in progress")
    admitted: dict[str, Any] = {
        "dataset": {"id": dataset.id, "digest": dataset.digest, "content_sha256": None},
        "agent": {"id": target.agent_id, "artifact_revision": None, "revision_source": "unavailable"},
        "model_version": None,
        "prompt_version": None,
        "target": pin,
    }
    if suite is not None:
        admitted["suite"] = {
            "id": suite.id,
            "version": suite.version,
            "version_id": str(suite_version_id(suite.id, suite.version)),
            "name": suite.name,
            "digest": suite.digest,
        }
    run = Run(
        id=run_id,
        project_id=project,
        code=await _next_code(store, records, principal),
        source="console",
        state="queued",
        suite_id=suite.id if suite else None,
        suite_version=suite.version if suite else None,
        suite_digest=suite.digest if suite else None,
        dataset_name=dataset.name,
        dataset_digest=dataset.digest,
        target=target,
        scorers=[builtin_scorer("exact_match")],
        max_cases=max_cases,
        provenance={**admitted, **(provenance or {})},
        requested_by=principal.user_id,
        created_at=_now(),
        store=StoreRef(dataset_id=dataset.id),
    )
    run = await records.create_run(principal, run)
    await worker.submit(Work(kind="run", principal=principal, project_id=project, run_id=run.id))
    return run


@router.post("", response_model=EvalRunOut, status_code=202, dependencies=[Depends(require_capability("run"))])
async def request_run(
    body: EvalRunRequest, principal: PrincipalDep, store: StoreDep, records: RecordsDep, worker: WorkerDep
) -> EvalRunOut:
    run_id = run_id_for(body.request_id)
    if existing := await lookup_run(store, records, principal, run_id):
        if not _same_request(existing, body, principal):
            raise HTTPException(409, "request_id was already used for a different eval")
        return run_out(existing)
    project = await writable_project(store, principal, body.project_id)
    target = Target(kind="agent", agent_id=str(body.agent_id))
    pin = await worker.check_target(principal, target)
    dataset = await store.find_dataset(principal, DEFAULT_PROJECT, body.dataset_name)
    if dataset is None:
        raise HTTPException(422, "Unknown dataset")
    run = await admit_run(
        store,
        records,
        worker,
        principal,
        run_id=run_id,
        project=project,
        target=target,
        pin=pin,
        dataset=dataset,
        max_cases=body.max_items,
    )
    return run_out(run)


@router.post(
    "/{run_id:uuid}/cancel",
    response_model=EvalRunOut,
    status_code=202,
    dependencies=[Depends(require_capability("run"))],
)
async def cancel_eval(run_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep) -> EvalRunOut:
    # A queued run is cancelled at once; a running one when its worker reaches the next case.
    run = await find_run(store, records, principal, run_id)
    for _ in range(3):
        if run.cancel_requested_at is not None:
            return run_out(run)
        if run.state not in ACTIVE or run.finished_at is not None:
            raise HTTPException(409, "Execution has finished; its recorded results remain available")
        now = _now()
        changes: dict[str, object] = {"cancel_requested_at": now}
        if run.state == "queued":
            changes |= {"state": "cancelled", "finished_at": now}
        try:
            return run_out(
                await records.update_run(principal, run.model_copy(update=changes), expected_state=run.state)
            )
        except StoreError as error:
            if error.kind != "conflict":
                raise
        run = await find_run(store, records, principal, run_id)
    raise HTTPException(409, "The run changed while stopping it; try again")


__all__ = ["ACTIVE", "admit_run", "router", "run_id_for"]
