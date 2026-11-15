"""Runs of the local edition: started in the Evals UI against a local target, each in a process of
its own, and run again with the settings they were started with.

``martian ui`` (:func:`martian.evals.local.create_local_app`) serves the core with
:class:`LocalWorker` and adds two routes, the local edition's, outside the frozen ``/v1/evals``
contract:

- ``POST /v1/local/runs``: the contract's launch (``EvalRunRequest``: a local target's id as
  ``agent_id``, a published dataset, ``max_items``) and, optionally, ``suite_version_id``, a saved
  suite version. It answers ``202`` with the queued run, as the launch does. The run grades each
  output by exact match, as every launched run does. With a suite, the worker records a queued
  scoring job of that suite version beside the run, and runs it once the run completes, so the
  suite's scorers and policies apply to its results; a run that does not complete cancels it.
- ``POST /v1/local/runs/{run_id}/again``: a new run of a finished run with its saved settings: the
  same target, the same dataset version, the same suite version and the same case limit. The new
  run names the one it repeats in ``provenance.repeat_of``.

Both take a ``request_id`` and are idempotent by it, as the launch is. A suite with a model judge
is refused: the local edition's worker has no judge.

The key of a local target reaches the target call only inside each run's process: the worker's
setup (:func:`worker_services`) builds a call for each target there
(:meth:`~martian.evals.local_targets.LocalTargets.call`), which fills in ``${MARTIAN_TARGET_KEY}``
from :meth:`~martian.evals.local_targets.LocalTargets.credential`, and so from
:meth:`~martian.evals.local_targets.LocalTargets.key`, each time it calls the target. The server's
own process never reads a key, and no request, answer, log line, trace or record holds one.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from martian.evals.local_targets import LocalTargets
from martian.evals.model import Run, ScoringJob, Suite, Target
from martian.evals.records import RecordStore
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, StoreDep, WorkerDep, require_capability
from martian.evals.server.launch import ACTIVE, admit_run, run_id_for
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id, project_key, run_out
from martian.evals.server.runs import find_run, lookup_run, writable_key, writable_project
from martian.evals.server.schemas import EvalRunOut
from martian.evals.server.scoring import find_suite_version
from martian.evals.server.write_schemas import EvalRunRequest
from martian.evals.store import StoreError
from martian.evals.worker import ProcessWorker, Work, Worker, WorkerServices

log = logging.getLogger("martian.evals.worker")

TARGET_TIMEOUT_SECONDS = 120.0
"""How long one call of a local target may take before its case is an error."""

NO_JUDGE = "Model judges require a judge configured for this installation's worker"
USED = "request_id was already used for a different eval"
NOT_COMPLETED = "The run did not complete, so its suite did not score it"


# --- the worker -------------------------------------------------------------------------------


def worker_services(home: str) -> WorkerServices:
    """:class:`LocalWorker`'s setup, called in each run's process: the stores in the data folder
    ``home``, and a call for each local target, which reads the target's key in that process
    (``LocalTargets.call``)."""
    from martian.evals.local import open_stores

    folder = Path(home)
    store, records = open_stores(folder)
    targets = LocalTargets(folder)
    calls = {target.id: targets.call(target, timeout_seconds=TARGET_TIMEOUT_SECONDS) for target in targets.defined()}
    return WorkerServices(store, records, targets=calls, target_timeout_seconds=TARGET_TIMEOUT_SECONDS)


class LocalWorker:
    """The local edition's worker. Each piece of work runs in a new process of its own, started by
    a :class:`~martian.evals.worker.ProcessWorker` whose setup is :func:`worker_services`, never in
    the server's process: MLflow keeps its state for the whole process. It runs no model judge.

    A run admitted with a suite gets a scoring job of that suite version, recorded ``queued`` when
    the run is submitted, so the UI shows it from the start. Once the run's process ends, the job
    runs in a process of its own if the run completed, and is cancelled otherwise.

    The work runs while ``martian ui`` does: a run its process had not finished when the server
    stopped stays ``running``, as with any worker until the durable queue (MorningHQ/martian-notes#139).
    """

    def __init__(self, home: Path, records: RecordStore, targets: LocalTargets) -> None:
        self._home = home
        self._records = records
        self._targets = targets
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def judges(self) -> bool:
        return False

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        """A local target by its id (``agent_id``), pinned by its name and its definition as the
        shared model's target, with the key as a reference (``LocalTargets.model_target``)."""
        found = self._targets.get(target.agent_id) if target.kind == "agent" and target.agent_id else None
        if found is None:
            raise StoreError("not_found", "Target not found")
        endpoint = self._targets.model_target(found).model_dump(mode="json", exclude_defaults=True)
        return {"kind": "agent", "agent_id": found.id, "name": found.name, "endpoint": endpoint}

    async def submit(self, work: Work) -> None:
        """Record the suite's scoring job of a run, start the work's process and return; a task
        waits for the process, then runs or cancels the scoring job."""
        scoring = await self._suite_job(work) if work.kind == "run" else None
        processes = ProcessWorker(worker_services, self._records, options={"home": str(self._home)})
        await processes.submit(work)
        task = asyncio.get_running_loop().create_task(self._then(processes, work, scoring))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait until every piece of work submitted so far, and the scoring it led to, has finished."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def _suite_job(self, work: Work) -> Work | None:
        """The scoring job of the suite the run was admitted with, recorded ``queued``."""
        records, principal = self._records, work.principal
        run = await records.get_run(principal, work.project_id, work.run_id)
        if run is None or run.suite_id is None or run.suite_version is None:
            return None
        suite = await records.get_suite(principal, DEFAULT_PROJECT, run.suite_id, version=run.suite_version)
        if suite is None:
            raise StoreError("not_found", "Suite version not found")
        job = await records.create_scoring_job(
            principal,
            run.project_id,
            ScoringJob(
                id=str(derived_id("suite-scoring", run.id)),
                run_id=run.id,
                scorers=list(suite.scorers),
                policies=list(suite.policies),
                suite_id=suite.id,
                suite_version=suite.version,
                state="queued",
                requested_by=run.requested_by,
                created_at=datetime.now(UTC),
            ),
        )
        return Work(kind="scoring", principal=principal, project_id=run.project_id, run_id=run.id, job_id=job.id)

    async def _then(self, processes: ProcessWorker, work: Work, scoring: Work | None) -> None:
        await processes.drain()
        if scoring is None:
            return
        try:
            run = await self._records.get_run(work.principal, work.project_id, work.run_id)
            if run is not None and run.state == "completed":
                await processes.submit(scoring)
            else:
                await self._cancel(scoring)
        except Exception:  # noqa: BLE001 - the run stays as it finished; only its suite's scores are missing
            log.exception("The suite of run %s could not be started", work.run_id)
        await processes.drain()

    async def _cancel(self, scoring: Work) -> None:
        """Cancel the suite's scoring job of a run that did not complete, if it is still queued."""
        principal, project = scoring.principal, scoring.project_id
        jobs = await self._records.list_scoring_jobs(principal, project, scoring.run_id)
        job = next((job for job in jobs if job.id == scoring.job_id), None)
        if job is None or job.state != "queued":
            return
        changes = {"state": "cancelled", "error": NOT_COMPLETED, "finished_at": datetime.now(UTC)}
        try:
            await self._records.update_scoring_job(
                principal, project, job.model_copy(update=changes), expected_state="queued"
            )
        except StoreError as error:
            if error.kind != "conflict":
                raise


# --- the routes -------------------------------------------------------------------------------


class LocalRunRequest(EvalRunRequest):
    suite_version_id: uuid.UUID | None = None


class RunAgainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)


def _suite_version_id(run: Run) -> str | None:
    suite = run.provenance.get("suite")
    return str(suite.get("version_id")) if isinstance(suite, dict) else None


def _same_launch(run: Run, body: LocalRunRequest, principal: Principal) -> bool:
    return (
        run.target is not None
        and run.target.agent_id == str(body.agent_id)
        and run.dataset_name == body.dataset_name
        and run.project_id == project_key(body.project_id)
        and run.max_cases == body.max_items
        and _suite_version_id(run) == (str(body.suite_version_id) if body.suite_version_id else None)
        and run.requested_by == principal.user_id
        and "repeat_of" not in run.provenance
    )


def _runnable(suite: Suite, worker: Worker) -> Suite:
    if any(scorer.kind == "llm_judge" for scorer in suite.scorers) and not worker.judges:
        raise HTTPException(422, NO_JUDGE)
    return suite


router = APIRouter(prefix="/v1/local/runs", tags=["local runs"])


@router.post("", response_model=EvalRunOut, status_code=202, dependencies=[Depends(require_capability("run"))])
async def launch_local_run(
    body: LocalRunRequest, principal: PrincipalDep, store: StoreDep, records: RecordsDep, worker: WorkerDep
) -> EvalRunOut:
    run_id = run_id_for(body.request_id)
    if existing := await lookup_run(store, records, principal, run_id):
        if not _same_launch(existing, body, principal):
            raise HTTPException(409, USED)
        return run_out(existing)
    project = await writable_project(store, principal, body.project_id)
    target = Target(kind="agent", agent_id=str(body.agent_id))
    pin = await worker.check_target(principal, target)
    dataset = await store.find_dataset(principal, DEFAULT_PROJECT, body.dataset_name)
    if dataset is None:
        raise HTTPException(422, "Unknown dataset")
    suite = None
    if body.suite_version_id is not None:
        suite = await find_suite_version(records, principal, body.suite_version_id)
        if suite is None:
            raise HTTPException(404, "Suite version not found")
        _runnable(suite, worker)
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
        suite=suite,
    )
    return run_out(run)


@router.post(
    "/{run_id:uuid}/again",
    response_model=EvalRunOut,
    status_code=202,
    dependencies=[Depends(require_capability("run"))],
)
async def run_again(
    run_id: uuid.UUID,
    body: RunAgainRequest,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    worker: WorkerDep,
) -> EvalRunOut:
    original = await find_run(store, records, principal, run_id)
    again_id = run_id_for(body.request_id)
    if existing := await lookup_run(store, records, principal, again_id):
        if existing.provenance.get("repeat_of") != original.id or existing.requested_by != principal.user_id:
            raise HTTPException(409, USED)
        return run_out(existing)
    if original.state in ACTIVE:
        raise HTTPException(409, "Run again repeats a finished run; this one has not finished")
    target, dataset_id = original.target, original.store.dataset_id
    if target is None or target.kind != "agent" or dataset_id is None or original.max_cases is None:
        raise HTTPException(422, "This run was not started in the UI, so it has no saved settings to run again")
    project = await writable_key(store, principal, original.project_id)
    pin = await worker.check_target(principal, target)
    dataset = await store.get_dataset(principal, DEFAULT_PROJECT, dataset_id)
    if dataset is None or dataset.digest != original.dataset_digest:
        raise HTTPException(422, "The run's dataset version is no longer in the store")
    suite = None
    if original.suite_id is not None and original.suite_version is not None:
        suite = await records.get_suite(principal, DEFAULT_PROJECT, original.suite_id, version=original.suite_version)
        if suite is None:
            raise HTTPException(422, "The run's suite version is no longer saved")
        _runnable(suite, worker)
    run = await admit_run(
        store,
        records,
        worker,
        principal,
        run_id=again_id,
        project=project,
        target=target,
        pin=pin,
        dataset=dataset,
        max_cases=original.max_cases,
        suite=suite,
        provenance={"repeat_of": original.id},
    )
    return run_out(run)


__all__ = [
    "TARGET_TIMEOUT_SECONDS",
    "LocalRunRequest",
    "LocalWorker",
    "RunAgainRequest",
    "router",
    "worker_services",
]
