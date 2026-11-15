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
  The suite's model judges run on the SDK judge (:func:`~martian.evals.worker.sdk_judge`), each on
  its own model or the default judge model, pinned when the job is recorded.
- ``POST /v1/local/runs/{run_id}/again``: a new run of a finished run with its saved settings: the
  same target, the same dataset version, the same suite version and the same case limit. The new
  run names the one it repeats in ``provenance.repeat_of``.

Both take a ``request_id`` and are idempotent by it, as the launch is. A suite with a model judge
needs ``acknowledge_model_cost``, in both, as a scoring request does: each judged case is a model
call on the user's own key.

The worker calls a local target itself, as an HTTP target of the worker
(:meth:`~martian.evals.local_targets.LocalTargets.http_targets`, :mod:`martian.evals.worker.targets`),
in each run's process, and fills in ``${MARTIAN_TARGET_KEY}`` there from the worker's
``Credentials``, :meth:`~martian.evals.local_targets.LocalTargets.credential`, each time it calls
the target. The server's own process never reads a key, and no request, answer, log line, trace or
record holds one.

A run the UI starts, and a run ``martian evals run`` records, is held by the processes working on
it (:class:`RunLock`): an operating-system lock on a file in the data folder's ``locks/``, which
ends with the process however the process ends. A queued or running run that no process holds any
more, such as one whose ``martian ui`` was stopped before it finished, is finished as interrupted
when ``martian ui`` starts (:func:`finish_interrupted`) or when someone cancels it
(:meth:`LocalWorker.cancel_interrupted`), so that the next launch is admitted. A run another live
process holds is left alone.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from martian.evals.judge_settings import JudgeSettings
from martian.evals.local_targets import LocalTargets
from martian.evals.model import Run, RunState, ScoringJob, Suite, Target
from martian.evals.records import RecordStore
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, StoreDep, WorkerDep, require_capability
from martian.evals.server.launch import ACTIVE, admit_run, run_id_for
from martian.evals.server.principal import SINGLE_USER, Principal
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id, project_key, run_out
from martian.evals.server.runs import find_run, lookup_run, project_keys, writable_key, writable_project
from martian.evals.server.schemas import EvalRunOut
from martian.evals.server.scoring import find_suite_version, with_models
from martian.evals.server.write_schemas import EvalRunRequest
from martian.evals.store import EvalStore, StoreError
from martian.evals.worker import ProcessWorker, Work, WorkerServices, sdk_judge
from martian.evals.worker.targets import http_pin

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

log = logging.getLogger("martian.evals.worker")

TARGET_TIMEOUT_SECONDS = 120.0
"""How long one call of a local target may take before its case is an error."""

NO_ACKNOWLEDGEMENT = "Model scoring requires cost acknowledgement"
USED = "request_id was already used for a different eval"
NOT_COMPLETED = "The run did not complete, so its suite did not score it"
INTERRUPTED = "Interrupted: the process working on this run stopped before it finished"


# --- the processes working on a run -----------------------------------------------------------

LOCKS = "locks"
"""The data folder's folder of run locks (:class:`RunLock`)."""

SERVER = "server"
"""The lock of the ``martian ui`` that admitted a run, held from submitting it until its process has ended."""

PROCESS = "process"
"""The lock of the process that runs a run: the run's own process, or ``martian evals run``."""

_HOLDERS = (SERVER, PROCESS)


def _lock_path(home: Path, run_id: str, holder: str) -> Path:
    return home / LOCKS / f"{run_id}.{holder}.lock"


def _lock(descriptor: int) -> bool:
    """Take the exclusive lock on the open file ``descriptor`` without waiting: whether it was free.
    The lock belongs to this open file, so a second open of the same file in this process does not
    get it either."""
    if sys.platform == "win32":
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _close(descriptor: int) -> None:
    """Release the lock on ``descriptor``, if this open file holds it, and close the file."""
    if sys.platform == "win32":
        with contextlib.suppress(OSError):  # not locked through this open file
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    os.close(descriptor)  # on POSIX, closing the file releases its lock


class RunLock:
    """That this process is working on the run ``run_id`` of the data folder ``home``, as ``holder``
    (:data:`SERVER` or :data:`PROCESS`): an exclusive lock, which the operating system keeps, on
    ``locks/<run id>.<holder>.lock``. It is held until :meth:`release`, or until the process ends,
    however it ends: a crash, a kill and a reboot release it too, so a lock can never outlive the
    process that holds it. Raises ``RuntimeError`` when another process holds it."""

    def __init__(self, home: Path, run_id: str, holder: str) -> None:
        self.path = _lock_path(home, run_id, holder)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if not _lock(descriptor):
            os.close(descriptor)
            raise RuntimeError(f"another process holds {self.path}")
        self._descriptor: int | None = descriptor

    def release(self) -> None:
        """Release the lock and remove its file. Releasing it again does nothing."""
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        if sys.platform == "win32":
            _close(descriptor)
            with contextlib.suppress(OSError):  # another process has the file open; a free lock holds nothing
                self.path.unlink(missing_ok=True)
            return
        # Removed while still held, so a process that opened the file meanwhile finds it free only
        # once this one is done with the run.
        self.path.unlink(missing_ok=True)
        _close(descriptor)


def held(home: Path, run_id: str) -> bool:
    """Whether a process holds a lock on the run ``run_id`` of the data folder ``home``: whether
    something is still working on it. A run with no lock file, such as one an earlier release
    recorded, is held by nothing."""
    for holder in _HOLDERS:
        try:
            descriptor = os.open(_lock_path(home, run_id, holder), os.O_RDWR)
        except FileNotFoundError:
            continue
        free = _lock(descriptor)
        _close(descriptor)
        if not free:
            return True
    return False


async def _interrupt(records: RecordStore, principal: Principal, run: Run, state: RunState) -> Run:
    """Finish ``run``, which no process is working on any more, as ``state`` (``failed`` or
    ``cancelled``) with :data:`INTERRUPTED` as its error, and cancel its suite's scoring job, which
    nothing will start now. Raises ``StoreError`` (``conflict``) when the run changed meanwhile."""
    now = datetime.now(UTC)
    changes: dict[str, Any] = {"state": state, "error": INTERRUPTED, "finished_at": now}
    if state == "cancelled":
        changes["cancel_requested_at"] = run.cancel_requested_at or now
    ended = await records.update_run(principal, run.model_copy(update=changes), expected_state=run.state)
    if ended.suite_id is not None:
        await _cancel_job(records, principal, ended.project_id, ended.id, _suite_job_id(ended.id))
    return ended


async def finish_interrupted(home: Path, store: EvalStore, records: RecordStore) -> list[Run]:
    """Finish every queued or running run of the data folder ``home`` that no process holds any
    more (:func:`held`), such as one whose ``martian ui`` stopped before it finished: ``failed``,
    or ``cancelled`` when someone had asked to stop it, with :data:`INTERRUPTED` as its error.
    A run another live process holds, such as one ``martian evals run`` is recording, is left
    alone. ``martian ui`` calls it when it starts, before it admits a run; the finished runs."""
    principal = SINGLE_USER
    active: list[Run] = []
    for key in await project_keys(store, principal):
        cursor = None
        while True:
            page = await records.list_runs(principal, key, states=list(ACTIVE), limit=200, cursor=cursor)
            active.extend(page.items)
            if (cursor := page.next_cursor) is None:
                break
    ended: list[Run] = []
    for run in active:
        if held(home, run.id):
            continue
        try:
            ended.append(
                await _interrupt(records, principal, run, "cancelled" if run.cancel_requested_at else "failed")
            )
        except StoreError as error:
            if error.kind != "conflict":
                raise
            continue  # it moved on meanwhile, so something is working on it
        log.warning("Run %s was interrupted: no process was running it any more", run.code or run.id)
    return ended


# --- the worker -------------------------------------------------------------------------------


def worker_services(home: str, run_id: str | None = None) -> WorkerServices:
    """:class:`LocalWorker`'s setup, called in each run's process: the stores in the data folder
    ``home``, the local targets' keys as the worker's ``Credentials``, read in that process, and
    the SDK judge. The local targets themselves arrive with the work, as written, with their keys
    as references. The judge has no model of its own: each judge scorer names the model it was
    pinned to, and reads the provider's key from this process's environment.

    With ``run_id``, the run's process holds the run (:class:`RunLock`, :data:`PROCESS`) until it
    exits, so that the run stays held while it runs even if ``martian ui`` stops first."""
    from martian.evals.local import open_stores

    folder = Path(home)
    if run_id is not None:
        atexit.register(RunLock(folder, run_id, PROCESS).release)
    store, records = open_stores(folder)
    return WorkerServices(
        store,
        records,
        judge=sdk_judge(),
        target_timeout_seconds=TARGET_TIMEOUT_SECONDS,
        credentials=LocalTargets(folder).credential,
    )


class LocalWorker:
    """The local edition's worker. Each piece of work runs in a new process of its own, started by
    a :class:`~martian.evals.worker.ProcessWorker` whose setup is :func:`worker_services`, never in
    the server's process: MLflow keeps its state for the whole process. Its HTTP targets are the
    local targets (``LocalTargets.http_targets``), read at each use, and its judge is the SDK
    judge.

    A run admitted with a suite gets a scoring job of that suite version, recorded ``queued`` when
    the run is submitted, so the UI shows it from the start, with each judge that names no model
    pinned to the default judge model then (``judge_settings``). Once the run's process ends, the
    job runs in a process of its own if the run completed, and is cancelled otherwise.

    The server holds each run it submits (:class:`RunLock`, :data:`SERVER`) until the run's process
    has ended, and that process holds it too (:data:`PROCESS`) while it runs. Work is not queued
    durably (MorningHQ/martian-notes#139): a run that no process holds any more is finished as
    interrupted when ``martian ui`` starts again (:func:`finish_interrupted`), or at once when it
    is cancelled (:meth:`cancel_interrupted`).
    """

    def __init__(
        self, home: Path, records: RecordStore, targets: LocalTargets, judge_settings: JudgeSettings | None = None
    ) -> None:
        self._home = home
        self._records = records
        self._targets = targets
        self._http = targets.http_targets()
        self._settings = judge_settings
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def judges(self) -> bool:
        return True

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        """A local target by its id (``agent_id``), pinned as the worker pins an HTTP target, by
        its id and a digest of it as written (``http_pin``), and by its name, which the run shows."""
        found = self._targets.get(target.agent_id) if target.kind == "agent" and target.agent_id else None
        if found is None:
            raise StoreError("not_found", "Target not found")
        return {**http_pin(found.id, self._targets.model_target(found)), "name": found.name}

    async def submit(self, work: Work) -> None:
        """Hold a run, record its suite's scoring job, start the work's process and return; a task
        waits for the process, releases the run, then runs or cancels the scoring job."""
        run_id = work.run_id if work.kind == "run" else None
        lock = RunLock(self._home, work.run_id, SERVER) if run_id is not None else None
        try:
            scoring = await self._suite_job(work) if run_id is not None else None
            processes = self._processes(run_id)
            await processes.submit(work)
        except BaseException:
            if lock is not None:
                lock.release()
            raise
        task = asyncio.get_running_loop().create_task(self._then(processes, work, scoring, lock))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait until every piece of work submitted so far, and the scoring it led to, has finished."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def cancel_interrupted(self, principal: Principal, run: Run) -> Run | None:
        """The cancel route's question (``InterruptedRuns``): cancel the running ``run`` at once when
        no process holds it any more (:func:`held`), with :data:`INTERRUPTED` as its error; None
        while one does, which then stops it before its next case."""
        if held(self._home, run.id):
            return None
        return await _interrupt(self._records, principal, run, "cancelled")

    def _processes(self, run_id: str | None) -> ProcessWorker:
        """A worker that starts one process per piece of work; with ``run_id``, that process holds
        the run while it runs."""
        options: dict[str, Any] = {"home": str(self._home)}
        if run_id is not None:
            options["run_id"] = run_id
        return ProcessWorker(worker_services, self._records, judges=True, options=options, targets=self._http)

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
                id=_suite_job_id(run.id),
                run_id=run.id,
                scorers=await with_models(self._settings, principal, list(suite.scorers)),
                policies=list(suite.policies),
                suite_id=suite.id,
                suite_version=suite.version,
                state="queued",
                requested_by=run.requested_by,
                created_at=datetime.now(UTC),
            ),
        )
        return Work(kind="scoring", principal=principal, project_id=run.project_id, run_id=run.id, job_id=job.id)

    async def _then(self, processes: ProcessWorker, work: Work, scoring: Work | None, lock: RunLock | None) -> None:
        try:
            await processes.drain()
        finally:
            if lock is not None:  # the run's process has ended, and with it the run
                lock.release()
        if scoring is None:
            return
        scorer = self._processes(None)
        try:
            run = await self._records.get_run(work.principal, work.project_id, work.run_id)
            if run is not None and run.state == "completed":
                await scorer.submit(scoring)
            else:
                await _cancel_job(self._records, scoring.principal, scoring.project_id, scoring.run_id, scoring.job_id)
        except Exception:  # noqa: BLE001 - the run stays as it finished; only its suite's scores are missing
            log.exception("The suite of run %s could not be started", work.run_id)
        await scorer.drain()


def _suite_job_id(run_id: str) -> str:
    """The id of the suite's scoring job of the run ``run_id``."""
    return str(derived_id("suite-scoring", run_id))


async def _cancel_job(
    records: RecordStore, principal: Principal, project: str, run_id: str, job_id: str | None
) -> None:
    """Cancel the suite's scoring job of a run that did not complete, if it is still queued."""
    jobs = await records.list_scoring_jobs(principal, project, run_id)
    job = next((job for job in jobs if job.id == job_id), None)
    if job is None or job.state != "queued":
        return
    changes = {"state": "cancelled", "error": NOT_COMPLETED, "finished_at": datetime.now(UTC)}
    try:
        await records.update_scoring_job(principal, project, job.model_copy(update=changes), expected_state="queued")
    except StoreError as error:
        if error.kind != "conflict":
            raise


# --- the routes -------------------------------------------------------------------------------


class LocalRunRequest(EvalRunRequest):
    suite_version_id: uuid.UUID | None = None
    acknowledge_model_cost: bool = False


class RunAgainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    acknowledge_model_cost: bool = False


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


def _acknowledged(suite: Suite, acknowledged: bool) -> None:
    """A suite with a model judge calls a model for each judged case: refuse it unless the request
    acknowledges the cost, as a scoring request must."""
    if any(scorer.kind == "llm_judge" for scorer in suite.scorers) and not acknowledged:
        raise HTTPException(422, NO_ACKNOWLEDGEMENT)


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
        _acknowledged(suite, body.acknowledge_model_cost)
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
        _acknowledged(suite, body.acknowledge_model_cost)
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
    "INTERRUPTED",
    "PROCESS",
    "SERVER",
    "TARGET_TIMEOUT_SECONDS",
    "LocalRunRequest",
    "LocalWorker",
    "RunAgainRequest",
    "RunLock",
    "finish_interrupted",
    "held",
    "router",
    "worker_services",
]
