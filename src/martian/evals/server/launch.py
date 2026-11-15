"""Launching and cancelling a run: a port of ``POST /v1/evals`` and ``POST /v1/evals/{run_id}/cancel``
in the server's ``app/api/evals.py`` (MartianSecurity/MartianBackend).

A launch admits the run and returns at once. It checks the request, asks the worker whether it can
call the run's target, records the run as ``queued`` with the published dataset version it will
read, and submits it to the worker. The worker calls the target later; the request never does.

- ``request_id`` identifies the request: the run's id is derived from it, so sending the same
  request again returns the same run, and the same ``request_id`` with other parameters is ``409``.
- An organisation has at most its edition's ``concurrent_runs`` queued or running at once: one
  more is ``409`` (:mod:`martian.evals.server.admission`).
- Run codes count up from ``EV-1001``, as on the server.
- Cancelling a queued run finishes it at once. A running one gets ``cancel_requested_at``, which its
  worker reads before the next case, or is cancelled at once when its worker says that nothing is
  working on it any more (:class:`~martian.evals.worker.InterruptedRuns`).

:func:`admit_run` is the admission itself, which the local edition's launch and run again share
(:mod:`martian.evals.local_runs`). An edition's own launch may also name checks its worker installs,
which the run grades each case with (:mod:`martian.evals.worker.hooks`); the contract's launch
names none.

A run, or a scoring job, that stopped at one of its limits (``stopped``) is resumed by
:func:`resume_run` or :func:`resume_scoring`: queued again, optionally under new limits, so that
the worker goes on after the cases it finished (``execute_run``'s checkpoints). The contract's
launch body takes optional ``limits`` (``RunLimits``, kept in ``provenance.limits``, without them
the worker's apply) and ``POST /{run_id}/resume`` resumes a stopped run, optionally under new
limits (version 4); neither route exists for a scoring job, and the local edition's own routes
take both (:mod:`martian.evals.local_runs`).

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException

from martian.evals.manifests import PAUSED, publishing
from martian.evals.model import DatasetVersion, Run, RunLimits, ScorerVersion, ScoringJob, StoreRef, Suite, Target
from martian.evals.records import RecordStore
from martian.evals.server.admission import ACTIVE, ensure_run_place, ensure_scoring_place
from martian.evals.server.dependencies import (
    AgentNamesDep,
    EntitlementsDep,
    OptionalWorkerDep,
    PrincipalDep,
    RecordsDep,
    StoreDep,
    WorkerDep,
    require_capability,
)
from martian.evals.server.entitlements import Entitlements
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id, project_key, run_out, suite_version_id
from martian.evals.server.runs import (
    agent_names,
    find_run,
    lookup_run,
    name_of,
    project_keys,
    project_runs,
    writable_key,
    writable_project,
)
from martian.evals.server.schemas import EvalRunOut
from martian.evals.server.write_schemas import EvalRunRequest, ResumeRequest
from martian.evals.store import EvalStore, StoreError

# `execute` as a module: the worker package imports these routes while it is importing it.
from martian.evals.worker import InterruptedRuns, Work, Worker, execute
from martian.evals.worker.hooks import is_installed
from martian.evals.worker.scorers import builtin_scorer
from martian.evals.worker.targets import TargetRefused

router = APIRouter(prefix="/v1/evals", tags=["evals"])

RUN_CODE = re.compile(r"^EV-(\d+)$")
FIRST_CODE = 1001


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


def limits_provenance(limits: RunLimits | None) -> dict[str, Any]:
    """The provenance that keeps a run's own limits, when it is given some."""
    return {} if limits is None else {execute.LIMITS_KEY: limits.model_dump(mode="json")}


def same_limits(run: Run, limits: RunLimits | None) -> bool:
    """Whether a repeated request's ``limits`` are the run's. Without limits of its own the run has
    its suite's or the worker's, which the request did not name, so a request without any matches."""
    return limits is None or run.provenance.get(execute.LIMITS_KEY) == limits_provenance(limits)[execute.LIMITS_KEY]


def _same_request(run: Run, body: EvalRunRequest, principal: Principal) -> bool:
    return (
        run.target is not None
        and run.target.agent_id == str(body.agent_id)
        and run.dataset_name == body.dataset_name
        and run.project_id == project_key(body.project_id)
        and run.max_cases == body.max_items
        and same_limits(run, body.limits)
        and run.requested_by == principal.user_id
    )


async def admit_run(
    store: EvalStore,
    records: RecordStore,
    worker: Worker,
    principal: Principal,
    *,
    entitlements: Entitlements,
    run_id: str,
    project: str,
    target: Target,
    pin: dict[str, Any],
    dataset: DatasetVersion,
    max_cases: int,
    suite: Suite | None = None,
    provenance: Mapping[str, Any] | None = None,
    checks: Sequence[ScorerVersion] = (),
) -> Run:
    """Record a queued run of ``target`` over the published ``dataset`` version and submit it to
    the worker, once the request is checked and the worker has pinned the target (``pin``). The
    organisation may have its edition's ``concurrent_runs`` (``entitlements``) queued or running at
    once: one more is ``409``.

    A launch admits every run this way. The local edition's launch also names a saved ``suite``
    version, which the run keeps (``suite_id``, ``suite_version``, ``suite_digest`` and
    ``provenance.suite``, which says whether the suite has a model judge), and adds its own
    ``provenance``. The run's limits, ``provenance.limits``, are the ones ``provenance`` gives,
    else the suite's (``Suite.settings.limits``); without either, the worker's apply. While a
    dataset of the project is being published, no run is admitted (``409``, ``PAUSED``).

    ``checks`` are checks the worker installs (:func:`~martian.evals.worker.hooks.installed_check`),
    which the run keeps beside exact match in its ``scorers`` and grades each answered case with,
    with the case's state from the worker's hooks. No route of the contract names one."""
    if not all(is_installed(check) for check in checks):
        raise ValueError("a run's checks are installed checks: code scorers of type installed")
    await ensure_run_place(store, records, principal, entitlements)
    if await publishing(records, principal, DEFAULT_PROJECT, _now()):
        raise HTTPException(409, PAUSED)
    provenance = dict(provenance or {})
    if suite is not None and suite.settings.limits is not None and execute.LIMITS_KEY not in provenance:
        provenance[execute.LIMITS_KEY] = suite.settings.limits.model_dump(mode="json")
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
            "model_judges": any(scorer.kind == "llm_judge" for scorer in suite.scorers),
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
        scorers=[builtin_scorer("exact_match"), *checks],
        max_cases=max_cases,
        provenance={**admitted, **provenance},
        requested_by=principal.user_id,
        created_at=_now(),
        store=StoreRef(dataset_id=dataset.id),
    )
    run = await records.create_run(principal, run)
    await execute.submit_or_fail(
        worker, records, Work(kind="run", principal=principal, project_id=project, run_id=run.id)
    )
    return run


@router.post("", response_model=EvalRunOut, status_code=202, dependencies=[Depends(require_capability("run"))])
async def request_run(
    body: EvalRunRequest,
    principal: PrincipalDep,
    entitlements: EntitlementsDep,
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
    try:
        pin = await worker.check_target(principal, target)
    except TargetRefused as error:
        raise HTTPException(422, str(error)) from None
    dataset = await store.find_dataset(principal, DEFAULT_PROJECT, body.dataset_name)
    if dataset is None:
        raise HTTPException(422, "Unknown dataset")
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
        provenance=limits_provenance(body.limits),
    )
    return run_out(run, name_of(await agent_names(names, principal, [run]), run))


NOT_STOPPED = "Only a run stopped at one of its limits can be resumed"
JOB_NOT_STOPPED = "Only a scoring job stopped at one of its limits can be resumed"
NOT_THE_WORKERS = (
    "This run was not run by the worker, so the worker cannot resume it; a martian evals run resumes when the "
    "same command runs again"
)


async def resume_run(
    store: EvalStore,
    records: RecordStore,
    worker: Worker,
    principal: Principal,
    run: Run,
    *,
    entitlements: Entitlements,
    limits: RunLimits | None,
) -> Run:
    """Queue ``run``, which stopped at one of its limits, again and submit it to the worker, which
    resumes it after the cases it finished: their target is not called again. ``limits`` replace
    the run's own when given, and the run keeps them. The organisation's ``concurrent_runs`` apply,
    as for a launch (``409``), and so does a launch's pause while a dataset is being published
    (``409``, ``PAUSED``); a run that is not ``stopped`` is ``409`` too, and one the worker did not
    run, such as a ``martian evals run``, ``422``."""
    if run.state != "stopped":
        raise HTTPException(409, NOT_STOPPED)
    if run.source != "console":  # recorded by the process that ran it, such as martian evals run
        raise HTTPException(422, NOT_THE_WORKERS)
    await ensure_run_place(store, records, principal, entitlements)
    if await publishing(records, principal, DEFAULT_PROJECT, _now()):
        raise HTTPException(409, PAUSED)
    provenance = {key: value for key, value in run.provenance.items() if key != execute.STOPPED_KEY}
    if limits is not None:
        provenance[execute.LIMITS_KEY] = limits.model_dump(mode="json")
    changes = {"state": "queued", "error": "", "finished_at": None, "cancel_requested_at": None}
    try:
        queued = await records.update_run(
            principal, run.model_copy(update={**changes, "provenance": provenance}), expected_state="stopped"
        )
    except StoreError as error:
        if error.kind != "conflict":
            raise
        raise HTTPException(409, NOT_STOPPED) from None
    work = Work(kind="run", principal=principal, project_id=run.project_id, run_id=run.id)
    await execute.submit_or_fail(worker, records, work)
    return queued


@router.post(
    "/{run_id:uuid}/resume",
    response_model=EvalRunOut,
    status_code=202,
    dependencies=[Depends(require_capability("run"))],
)
async def resume_eval(
    run_id: uuid.UUID,
    principal: PrincipalDep,
    entitlements: EntitlementsDep,
    store: StoreDep,
    records: RecordsDep,
    worker: WorkerDep,
    names: AgentNamesDep,
    body: Annotated[ResumeRequest, Body(default_factory=ResumeRequest)],
) -> EvalRunOut:
    run = await find_run(store, records, principal, run_id)
    await writable_key(store, principal, run.project_id)
    queued = await resume_run(store, records, worker, principal, run, entitlements=entitlements, limits=body.limits)
    return run_out(queued, name_of(await agent_names(names, principal, [queued]), queued))


async def resume_scoring(
    store: EvalStore,
    records: RecordStore,
    worker: Worker,
    principal: Principal,
    run: Run,
    job: ScoringJob,
    *,
    entitlements: Entitlements,
    limits: RunLimits | None,
) -> ScoringJob:
    """Queue ``job``, a scoring job of ``run`` that stopped at one of its limits, again and submit
    it to the worker, which resumes it after the cases it scored: their judges are not asked
    again. A job runs under its run's limits, so ``limits``, when given, replace the run's. One
    active scoring job per run, and the organisation's ``concurrent_scoring_jobs``, as for a
    scoring request (``409``); a job that is not ``stopped`` is ``409`` too."""
    if job.state != "stopped":
        raise HTTPException(409, JOB_NOT_STOPPED)
    if any(other.state in ACTIVE for other in await records.list_scoring_jobs(principal, run.project_id, run.id)):
        raise HTTPException(409, "A scoring job is already active for this run")
    await ensure_scoring_place(store, records, principal, entitlements)
    if limits is not None:
        provenance = {**run.provenance, execute.LIMITS_KEY: limits.model_dump(mode="json")}
        await records.update_run(principal, run.model_copy(update={"provenance": provenance}), expected_state=run.state)
    changes = {"state": "queued", "error": "", "finished_at": None, "cancel_requested_at": None}
    try:
        queued = await records.update_scoring_job(
            principal, run.project_id, job.model_copy(update=changes), expected_state="stopped"
        )
    except StoreError as error:
        if error.kind != "conflict":
            raise
        raise HTTPException(409, JOB_NOT_STOPPED) from None
    work = Work(kind="scoring", principal=principal, project_id=run.project_id, run_id=run.id, job_id=job.id)
    await execute.submit_or_fail(worker, records, work)
    return queued


async def _cancel_interrupted(worker: Worker | None, principal: Principal, run: Run) -> Run | None:
    """The running ``run`` cancelled at once, when its worker says nothing is working on it any
    more (``InterruptedRuns``), or None."""
    if run.state != "running" or run.finished_at is not None or not isinstance(worker, InterruptedRuns):
        return None
    return await worker.cancel_interrupted(principal, run)


@router.post(
    "/{run_id:uuid}/cancel",
    response_model=EvalRunOut,
    status_code=202,
    dependencies=[Depends(require_capability("run"))],
)
async def cancel_eval(
    run_id: uuid.UUID,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    worker: OptionalWorkerDep,
    names: AgentNamesDep,
) -> EvalRunOut:
    # A queued run is cancelled at once; a running one when its worker reaches the next case, or at
    # once when its worker says nothing is working on it any more, even if a stop was asked before.
    run = await find_run(store, records, principal, run_id)
    named = await agent_names(names, principal, [run])
    for _ in range(3):
        try:
            if (stopped := await _cancel_interrupted(worker, principal, run)) is not None:
                return run_out(stopped, name_of(named, stopped))
            if run.cancel_requested_at is not None:
                return run_out(run, name_of(named, run))
            if run.state not in ACTIVE or run.finished_at is not None:
                raise HTTPException(409, "Execution has finished; its recorded results remain available")
            now = _now()
            changes: dict[str, object] = {"cancel_requested_at": now}
            if run.state == "queued":
                changes |= {"state": "cancelled", "finished_at": now}
            moved = await records.update_run(principal, run.model_copy(update=changes), expected_state=run.state)
            return run_out(moved, name_of(named, moved))
        except StoreError as error:
            if error.kind != "conflict":
                raise
        run = await find_run(store, records, principal, run_id)
    raise HTTPException(409, "The run changed while stopping it; try again")


__all__ = [
    "ACTIVE",
    "admit_run",
    "limits_provenance",
    "resume_run",
    "resume_scoring",
    "router",
    "run_id_for",
    "same_limits",
]
