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
  same target, the same dataset version, the same suite version, the same case limit and the same
  limits, unless the request gives others. The new run names the one it repeats in
  ``provenance.repeat_of``.
- ``POST /v1/local/runs/{run_id}/resume``: go on with a run that stopped at one of its limits, or
  whose suite's scoring job did, after the cases it finished, optionally under new ``limits``
  (:func:`~martian.evals.server.launch.resume_run`, :func:`~martian.evals.server.launch.resume_scoring`).

The first two take a ``request_id`` and are idempotent by it, as the launch is, and ``limits``
(:class:`~martian.evals.model.RunLimits`: ``max_seconds`` and ``max_judge_tokens``), which the run
keeps in ``provenance.limits`` and its suite's scoring job runs under too; the frozen launch has no
such field. A suite with a model judge needs ``acknowledge_model_cost``, in both, as a scoring
request does: each judged case is a model call on the user's own key.

The worker calls a local target itself, as an HTTP target of the worker
(:meth:`~martian.evals.local_targets.LocalTargets.http_targets`, :mod:`martian.evals.worker.targets`),
in each run's process, and fills in ``${MARTIAN_TARGET_KEY}`` there from the worker's
``Credentials``, :meth:`~martian.evals.local_targets.LocalTargets.credential`, each time it calls
the target. The server's own process never reads a key, and no request, answer, log line, trace or
record holds one.

Runs and scoring jobs are queued durably in the data folder's ``martian.db``
(:class:`~martian.evals.records.queue.SqlWorkQueue`), and ``martian ui`` serves the queue beside the
app (:class:`~martian.evals.worker.QueueWorker`). Each run is held by whatever works on it: a
claim on the run's entry in the queue, whose lease that process renews. A run whose ``martian ui``
stopped is taken again once its lease runs out, by the next ``martian ui`` on the folder, and
resumes after the cases it had finished, on the dataset and suite versions it was admitted with;
its own process ended with the ``martian ui`` that started it. ``martian evals
run`` holds the run it records in the same queue (:class:`RunHold`), and a run whose recording
process stopped is finished as interrupted once its lease runs out, or at once when someone
cancels it (:meth:`LocalWorker.cancel_interrupted`). A queued or running run with no entry in the
queue at all, such as one an earlier release left running, is finished as interrupted when
``martian ui`` starts (:func:`finish_interrupted`), so that the next launch is admitted.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from martian.evals.judge_settings import JudgeSettings
from martian.evals.local_targets import LocalTargets
from martian.evals.model import Run, RunLimits, RunState, ScoringJob, Suite, Target
from martian.evals.records import RecordStore
from martian.evals.records.queue import RECORDING, Claim, SqlWorkQueue
from martian.evals.server.dependencies import (
    EntitlementsDep,
    PrincipalDep,
    RecordsDep,
    StoreDep,
    WorkerDep,
    require_capability,
)
from martian.evals.server.launch import (
    ACTIVE,
    admit_run,
    limits_provenance,
    pin_target,
    resume_run,
    resume_scoring,
    run_id_for,
    same_limits,
)
from martian.evals.server.principal import SINGLE_USER, Principal
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id, project_key, run_out
from martian.evals.server.runs import find_run, lookup_run, project_keys, writable_key, writable_project
from martian.evals.server.schemas import EvalRunOut
from martian.evals.server.scoring import find_suite_version, with_models
from martian.evals.server.write_schemas import EvalRunRequest, ResumeRequest
from martian.evals.store import EvalStore, StoreError
from martian.evals.worker import ProcessWorker, QueueWorker, Work, WorkerServices, dump_work, sdk_judge
from martian.evals.worker.execute import INTERRUPTED, LIMITS_KEY, interrupt
from martian.evals.worker.queue import entry_id
from martian.evals.worker.targets import TargetRefused, http_pin

log = logging.getLogger("martian.evals.worker")

TARGET_TIMEOUT_SECONDS = 120.0
"""How long one call of a local target may take before its case is an error."""

LEASE_SECONDS = 10.0
"""How long a claim in the local queue holds a run or a scoring job unless it is renewed: how long
after a ``martian ui`` stopped the next one takes its run again, and how long after a
``martian evals run`` stopped its run is finished as interrupted."""

POLL_SECONDS = 1.0
"""How often ``martian ui``'s worker looks for work it was not handed itself, such as a run whose
lease ran out."""

NO_ACKNOWLEDGEMENT = "Model scoring requires cost acknowledgement"
USED = "request_id was already used for a different eval"
NOT_COMPLETED = "The run did not complete, so its suite did not score it"


# --- the processes working on a run -----------------------------------------------------------


def open_queue(home: Path) -> SqlWorkQueue:
    """The queue in the data folder ``home``: the record store's database, ``martian.db``."""
    return SqlWorkQueue(f"sqlite:///{home / 'martian.db'}")


def _run_work(run_id: str) -> Work:
    return Work(kind="run", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=run_id)


class RunHold:
    """That this process is working on the run ``run_id`` of the data folder ``home``, which it
    records itself, outside the queue, as ``martian evals run`` does: a ``recording`` claim on the
    run's entry in the queue, which a thread renews every third of the lease until :meth:`release`.
    A process that ends without releasing it, however it ends, stops renewing it; once its lease
    runs out, the worker of a ``martian ui`` on the folder finishes the run as interrupted. Take
    one with :meth:`take`, before the run is recorded, so no process ever finds the run running and
    not held."""

    def __init__(self, queue: SqlWorkQueue, claim: Claim, lease_seconds: float) -> None:
        self._queue = queue
        self._claim = claim
        self._lease = lease_seconds
        self._stopped = threading.Event()
        self._renewing = threading.Thread(target=self._renew, name="martian-run-hold", daemon=True)
        self._renewing.start()

    @classmethod
    async def take(cls, home: Path, run_id: str, lease_seconds: float | None = None) -> RunHold:
        queue, lease = open_queue(home), lease_seconds or LEASE_SECONDS
        work = _run_work(run_id)
        try:
            claim = await queue.hold(SINGLE_USER, DEFAULT_PROJECT, entry_id(work), RECORDING, dump_work(work), lease)
        except BaseException:
            queue.close()
            raise
        return cls(queue, claim, lease)

    def _renew(self) -> None:
        while not self._stopped.wait(self._lease / 3):
            try:
                if not asyncio.run(self._queue.renew(self._claim, self._lease)):
                    return  # its lease ran out, and a worker finished the run as interrupted
            except Exception:  # noqa: BLE001 - the database is busy; try again at the next renewal
                log.exception("Could not renew the hold on run %s", self._claim.id)

    def release(self) -> None:
        """Release the run and remove its entry from the queue. Releasing it again does nothing."""
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._renewing.join()
        try:
            asyncio.run(self._queue.done(self._claim))
        finally:
            self._queue.close()


def held(home: Path, run_id: str) -> bool:
    """Whether something is working on the run ``run_id`` of the data folder ``home``: whether a
    claim in the queue whose lease has not run out holds it."""
    queue = open_queue(home)
    try:
        return asyncio.run(queue.held(SINGLE_USER, DEFAULT_PROJECT, entry_id(_run_work(run_id))))
    finally:
        queue.close()


async def _interrupt(records: RecordStore, principal: Principal, run: Run, state: RunState) -> Run:
    """Finish ``run``, which nothing is working on any more, as interrupted (``interrupt``), and
    cancel its suite's scoring job, which nothing will start now. Raises ``StoreError``
    (``conflict``) when the run changed meanwhile."""
    ended = await interrupt(records, principal, run, state)
    if ended.suite_id is not None:
        await _cancel_job(records, principal, ended.project_id, ended.id, _suite_job_id(ended.id))
    return ended


async def finish_interrupted(home: Path, store: EvalStore, records: RecordStore) -> list[Run]:
    """Finish every queued or running run of the data folder ``home`` that is not in its queue at
    all, such as one an earlier release left running, or one recorded before its work was queued:
    ``failed``, or ``cancelled`` when someone had asked to stop it, with :data:`INTERRUPTED` as its
    error. A run in the queue is left to it: the queue's worker takes it again, or, for a run
    another process is recording, finishes it as interrupted once that process's lease runs out.
    ``martian ui`` calls it when it starts, before it admits a run; the finished runs."""
    principal = SINGLE_USER
    active: list[Run] = []
    for key in await project_keys(store, principal):
        cursor = None
        while True:
            page = await records.list_runs(principal, key, states=list(ACTIVE), limit=200, cursor=cursor)
            active.extend(page.items)
            if (cursor := page.next_cursor) is None:
                break
    queue = open_queue(home)
    ended: list[Run] = []
    try:
        for run in active:
            work = Work(kind="run", principal=principal, project_id=run.project_id, run_id=run.id)
            if await queue.contains(principal, run.project_id, entry_id(work)):
                continue
            try:
                ended.append(
                    await _interrupt(records, principal, run, "cancelled" if run.cancel_requested_at else "failed")
                )
            except StoreError as error:
                if error.kind != "conflict":
                    raise
                continue  # it moved on meanwhile, so something is working on it
            log.warning("Run %s was interrupted: nothing was working on it any more", run.code or run.id)
    finally:
        queue.close()
    return ended


# --- the worker -------------------------------------------------------------------------------


def worker_services(home: str) -> WorkerServices:
    """:class:`LocalWorker`'s setup, called in each run's process: the stores in the data folder
    ``home``, the local targets' keys as the worker's ``Credentials``, read in that process, and
    the SDK judge. The local targets themselves arrive with the work, as written, with their keys
    as references. The judge has no model of its own: each judge scorer names the model it was
    pinned to, and reads the provider's key from this process's environment."""
    from martian.evals.local import open_stores

    folder = Path(home)
    store, records = open_stores(folder)
    return WorkerServices(
        store,
        records,
        judge=sdk_judge(),
        target_timeout_seconds=TARGET_TIMEOUT_SECONDS,
        credentials=LocalTargets(folder).credential,
    )


class LocalWorker:
    """The local edition's worker: a :class:`~martian.evals.worker.QueueWorker` on the data folder's
    queue, which ``martian ui`` serves beside the app, and which runs each piece of work in a new
    process of its own, started by a :class:`~martian.evals.worker.ProcessWorker` whose setup is
    :func:`worker_services`, never in the server's process: MLflow keeps its state for the whole
    process. Its HTTP targets are the local targets (``LocalTargets.http_targets``), read at each
    use, and its judge is the SDK judge. It runs ``concurrency`` pieces of work at once, each in a
    process of its own: ``martian ui --concurrency``, 1 unless set.

    A run admitted with a suite gets a scoring job of that suite version, recorded ``queued`` when
    the run is submitted, so the UI shows it from the start, with each judge that names no model
    pinned to the default judge model then (``judge_settings``). Once the run's process ends, the
    job is queued if the run completed, and cancelled otherwise, before the run leaves the queue,
    so a ``martian ui`` that stops in between does neither twice nor never. A run that stopped at
    one of its limits leaves its job queued, for the run to resume and complete.
    """

    def __init__(
        self,
        home: Path,
        records: RecordStore,
        targets: LocalTargets,
        judge_settings: JudgeSettings | None = None,
        *,
        lease_seconds: float | None = None,
        concurrency: int = 1,
    ) -> None:
        self._home = home
        self._records = records
        self._targets = targets
        self._settings = judge_settings
        self._queue = open_queue(home)
        self._processes = ProcessWorker(
            worker_services, records, judges=True, options={"home": str(home)}, targets=targets.http_targets()
        )
        self._queued = QueueWorker(
            self._queue,
            self,
            records,
            in_process=True,
            lease_seconds=lease_seconds or LEASE_SECONDS,
            poll_seconds=POLL_SECONDS,
            concurrency=concurrency,
        )

    @property
    def judges(self) -> bool:
        return True

    @property
    def concurrency(self) -> int:
        """How many pieces of work it runs at once."""
        return self._queued.concurrency

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        """A local target by its id (``agent_id``), pinned as the worker pins an HTTP target, by
        its id and a digest of it as written (``http_pin``), and by its name, which the run shows.
        One the worker would not call, such as a ``response_path`` it refuses, is
        :class:`~martian.evals.worker.targets.TargetRefused`, naming the target by name and id."""
        found = self._targets.get(target.agent_id) if target.kind == "agent" and target.agent_id else None
        if found is None:
            raise StoreError("not_found", "Target not found")
        try:
            pin = http_pin(found.id, self._targets.model_target(found))
        except ValueError as error:  # the refusal names the setting, never a value
            raise TargetRefused(f'The target "{found.name}" ({found.id}) is refused: {error}') from None
        return {**pin, "name": found.name}

    async def submit(self, work: Work) -> None:
        """Record a run's suite scoring job, then queue the work and return. If the work cannot be
        queued, the suite's job is cancelled, as the route fails the run."""
        if work.kind == "run":
            await self._suite_job(work)
        try:
            await self._queued.submit(work)
        except Exception:
            if work.kind == "run":
                await _cancel_job(
                    self._records, work.principal, work.project_id, work.run_id, _suite_job_id(work.run_id)
                )
            raise

    async def run(self, work: Work) -> None:
        """What the queue's worker runs for each piece of work it takes: the work in a process of
        its own, then, for a run, its suite's scoring job, queued or cancelled."""
        await self._processes.run(work)
        if work.kind == "run":
            await self._then_score(work)

    async def start(self) -> None:
        await self._queued.start()

    async def stop(self) -> None:
        await self._queued.stop()

    async def drain(self) -> None:
        """Wait until the queue holds no work: every run, and the scoring it led to, has finished."""
        await self._queued.drain()

    async def cancel_interrupted(self, principal: Principal, run: Run) -> Run | None:
        """The cancel route's question (``InterruptedRuns``): cancel the running ``run`` at once when
        no live claim in the queue holds it, with :data:`INTERRUPTED` as its error, and its suite's
        job; None while one does, which then stops it before its next case."""
        if await self._queue.held(principal, run.project_id, entry_id(_run_work(run.id))):
            return None
        return await _interrupt(self._records, principal, run, "cancelled")

    async def _suite_job(self, work: Work) -> None:
        """Record the scoring job of the suite the run was admitted with, ``queued``."""
        records, principal = self._records, work.principal
        run = await records.get_run(principal, work.project_id, work.run_id)
        if run is None or run.suite_id is None or run.suite_version is None:
            return
        if any(
            job.id == _suite_job_id(run.id)
            for job in await records.list_scoring_jobs(principal, run.project_id, run.id)
        ):
            return  # a resumed run: its job was recorded when it was first submitted
        suite = await records.get_suite(principal, DEFAULT_PROJECT, run.suite_id, version=run.suite_version)
        if suite is None:
            raise StoreError("not_found", "Suite version not found")
        await records.create_scoring_job(
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

    async def _then_score(self, work: Work) -> None:
        """Once the run's process has ended: queue its suite's scoring job if the run completed, or
        cancel the job if it did not. A job that is no longer queued is left alone."""
        records, principal, job_id = self._records, work.principal, _suite_job_id(work.run_id)
        run = await records.get_run(principal, work.project_id, work.run_id)
        if run is None or run.suite_id is None or run.state == "stopped":
            return  # a stopped run's job waits for the run to resume and complete
        if run.state != "completed":
            await _cancel_job(records, principal, work.project_id, work.run_id, job_id)
            return
        jobs = await records.list_scoring_jobs(principal, work.project_id, work.run_id)
        if any(job.id == job_id and job.state == "queued" for job in jobs):
            scoring = Work(
                kind="scoring", principal=principal, project_id=work.project_id, run_id=run.id, job_id=job_id
            )
            await self._queued.submit(scoring)


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
    limits: RunLimits | None = None


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
        and same_limits(run, body.limits)
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
    body: LocalRunRequest,
    principal: PrincipalDep,
    entitlements: EntitlementsDep,
    store: StoreDep,
    records: RecordsDep,
    worker: WorkerDep,
) -> EvalRunOut:
    run_id = run_id_for(body.request_id)
    if existing := await lookup_run(store, records, principal, run_id):
        if not _same_launch(existing, body, principal):
            raise HTTPException(409, USED)
        return run_out(existing)
    project = await writable_project(store, principal, body.project_id)
    target = Target(kind="agent", agent_id=str(body.agent_id))
    pin = await pin_target(worker, principal, target)
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
        entitlements=entitlements,
        run_id=run_id,
        project=project,
        target=target,
        pin=pin,
        dataset=dataset,
        max_cases=body.max_items,
        suite=suite,
        provenance=limits_provenance(body.limits),
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
    entitlements: EntitlementsDep,
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
    pin = await pin_target(worker, principal, target)
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
        entitlements=entitlements,
        run_id=again_id,
        project=project,
        target=target,
        pin=pin,
        dataset=dataset,
        max_cases=original.max_cases,
        suite=suite,
        provenance={"repeat_of": original.id, **limits_provenance(body.limits or _own_limits(original))},
    )
    return run_out(run)


def _own_limits(run: Run) -> RunLimits | None:
    own = run.provenance.get(LIMITS_KEY)
    return RunLimits.model_validate(own) if isinstance(own, dict) else None


@router.post(
    "/{run_id:uuid}/resume",
    response_model=EvalRunOut,
    status_code=202,
    dependencies=[Depends(require_capability("run"))],
)
async def resume_local_run(
    run_id: uuid.UUID,
    body: ResumeRequest,
    principal: PrincipalDep,
    entitlements: EntitlementsDep,
    store: StoreDep,
    records: RecordsDep,
    worker: WorkerDep,
) -> EvalRunOut:
    run = await find_run(store, records, principal, run_id)
    if run.state == "stopped":
        return run_out(
            await resume_run(store, records, worker, principal, run, entitlements=entitlements, limits=body.limits)
        )
    jobs = await records.list_scoring_jobs(principal, run.project_id, run.id)
    job = next((job for job in jobs if job.id == _suite_job_id(run.id) and job.state == "stopped"), None)
    if run.state != "completed" or job is None:
        raise HTTPException(409, "Only a run, or its suite's scoring, stopped at one of its limits can be resumed")
    await resume_scoring(store, records, worker, principal, run, job, entitlements=entitlements, limits=body.limits)
    found = await lookup_run(store, records, principal, run.id)
    return run_out(found or run)


__all__ = [
    "INTERRUPTED",
    "LEASE_SECONDS",
    "TARGET_TIMEOUT_SECONDS",
    "LocalRunRequest",
    "LocalWorker",
    "ResumeRequest",
    "RunAgainRequest",
    "RunHold",
    "finish_interrupted",
    "held",
    "open_queue",
    "router",
    "worker_services",
]
