"""Scoring a finished run, and Monitoring: a port of the scoring routes of the server's
``app/api/evals_workbench.py`` and ``GET /monitoring`` and ``scoring_config`` of
``app/api/evals_quality.py`` (MorningHQ/MartianBackend).

A scoring request admits a ``queued`` scoring job and submits it to the worker, which runs the code
scorers and, through its judge, the model judges. The request calls no model.

- The job's id is derived from ``request_id``; the same request again returns the same job.
- The scorers are the console's built-in scorers by name, registered judges pinned by name and
  version, or a saved suite version's scorers and policies.
- Judge scorers need a worker with a judge (``Worker.judges``), as the server needs a configured
  judge.
- Each judge scorer is pinned to its model when the job is admitted: the judge's own, or the
  default judge model when it names none. Changing the default later does not change the job.
- One active scoring job per run, and at most the edition's ``concurrent_scoring_jobs`` queued or
  running in the organisation at once, which bounds its model use as the server's one per
  organisation does (:mod:`martian.evals.server.admission`).

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from martian.evals.judge_settings import JudgeSettings
from martian.evals.model import Policy, Run, ScorerVersion, ScoringJob
from martian.evals.outbox import save_score
from martian.evals.records import RecordStore
from martian.evals.server.admission import ACTIVE, ensure_scoring_place
from martian.evals.server.dependencies import (
    EntitlementsDep,
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
    public_job,
    score_id,
    suite_version_id,
)
from martian.evals.server.quality import definition_digest, scorecard
from martian.evals.server.runs import find_run, project_runs, run_results, writable_key
from martian.evals.server.write_schemas import ScoreRequest
from martian.evals.store import EvalStore, StoreError

# `execute` as a module: the worker package imports these routes while it is importing it.
from martian.evals.worker import Work, Worker, execute
from martian.evals.worker.scorers import BUILTIN_SCORERS, CODE_SCORERS, JUDGE_SCORERS, builtin_scorer

router = APIRouter(prefix="/v1/evals", tags=["evals"])

LABELS = {
    "exact_match": "Exact match",
    "contains_expected": "Contains expected text",
    "valid_json": "Valid JSON",
    "response_length": "Response length (characters)",
    "correctness": "Correctness judge",
    "safety": "Safety judge",
    "policy": "Policy compliance judge",
}
MONITORING_NOTE = (
    "Saved-output monitoring only. Judge alignment and live model scheduling require separately approved "
    "model budget and instrumentation."
)


def _now() -> datetime:
    return datetime.now(UTC)


def _worker(request: Request) -> Worker | None:
    worker: Worker | None = request.app.state.martian_evals_worker
    return worker


async def find_suite_version(records: RecordStore, principal: Principal, version_id: uuid.UUID) -> Any:
    """The saved suite version with this API id, or ``None``."""
    for latest in await records.list_suites(principal, DEFAULT_PROJECT):
        for version in range(latest.version, 0, -1):
            if suite_version_id(latest.id, version) == version_id:
                return await records.get_suite(principal, DEFAULT_PROJECT, latest.id, version=version)
    return None


async def _pinned_judges(
    store: EvalStore, principal: Principal, project: str, body: ScoreRequest
) -> list[ScorerVersion]:
    pinned = []
    for reference in body.judges:
        versions = await store.scorer_versions(principal, project, reference.name)
        found = next((v for v in versions if v.version == str(reference.version) and v.kind == "llm_judge"), None)
        if found is None:
            raise HTTPException(404, "Judge version not found")
        pinned.append(found)
    return pinned


async def with_models(
    settings: JudgeSettings | None, principal: Principal, scorers: list[ScorerVersion]
) -> list[ScorerVersion]:
    """The scorers with each judge that names no model pinned to the default judge model. With no
    default either, the judge stays without one, and the worker's judge uses its own."""
    if not any(s.kind == "llm_judge" and s.model is None for s in scorers) or settings is None:
        return scorers
    default = await settings.default_model(principal)
    return [
        s.model_copy(update={"model": default.model, "endpoint": default.endpoint})
        if s.kind == "llm_judge" and s.model is None
        else s
        for s in scorers
    ]


async def scoring_plan(
    store: EvalStore,
    records: RecordStore,
    principal: Principal,
    run: Run,
    body: ScoreRequest,
    settings: JudgeSettings | None = None,
) -> tuple[list[ScorerVersion], list[Policy], str | None, int | None]:
    """The scorers, policies and suite version a scoring request asks for, each judge pinned to the
    model it will run on."""
    scorers, policies, suite_id, suite_version = await _planned(store, records, principal, run, body)
    return await with_models(settings, principal, scorers), policies, suite_id, suite_version


async def _planned(
    store: EvalStore, records: RecordStore, principal: Principal, run: Run, body: ScoreRequest
) -> tuple[list[ScorerVersion], list[Policy], str | None, int | None]:
    if body.suite_version_id is not None:
        if body.judges:
            raise HTTPException(422, "This suite pins its own criteria; apply custom judges as a separate scoring job")
        suite = await find_suite_version(records, principal, body.suite_version_id)
        if suite is None:
            raise HTTPException(404, "Not found")
        guidelines = next((str(s.criteria.get("guidelines", "")) for s in suite.scorers if s.name == "policy"), "")
        if sorted(body.scorers) != sorted(s.name for s in suite.scorers) or body.guidelines != guidelines:
            raise HTTPException(422, "Scoring request differs from its pinned suite version")
        return list(suite.scorers), list(suite.policies), suite.id, suite.version
    scorers = [builtin_scorer(name, guidelines=body.guidelines) for name in sorted(body.scorers)]
    scorers += await _pinned_judges(store, principal, run.project_id, body)
    return sorted(scorers, key=lambda s: s.name), [], None, None


@router.get("/scorers", response_model=None)
async def scorers(request: Request, principal: PrincipalDep) -> list[dict[str, Any]]:
    worker = _worker(request)
    judging = worker is not None and worker.judges
    return [
        {
            "name": name,
            "label": LABELS[name],
            "source": "LLM_JUDGE" if name in JUDGE_SCORERS else "CODE",
            "available": judging or name in CODE_SCORERS,
            "requires_guidelines": name == "policy",
        }
        for name in BUILTIN_SCORERS
    ]


async def _job(records: RecordStore, principal: Principal, run: Run, job_id: str) -> ScoringJob | None:
    return next((j for j in await records.list_scoring_jobs(principal, run.project_id, run.id) if j.id == job_id), None)


def _same_job(
    job: ScoringJob, run: Run, scorers: list[ScorerVersion], policies: list[Policy], suite: Any, principal: Principal
) -> bool:
    return (
        job.run_id == run.id
        and job.scorers == scorers
        and job.policies == policies
        and (job.suite_id, job.suite_version) == suite
        and job.requested_by == principal.user_id
    )


@router.post(
    "/{run_id:uuid}/scores", status_code=202, response_model=None, dependencies=[Depends(require_capability("score"))]
)
async def score(
    run_id: uuid.UUID,
    body: ScoreRequest,
    principal: PrincipalDep,
    entitlements: EntitlementsDep,
    store: StoreDep,
    records: RecordsDep,
    worker: WorkerDep,
    settings: JudgeSettingsDep,
) -> dict[str, Any]:
    run = await find_run(store, records, principal, run_id)
    scorers, policies, suite_id, suite_version = await scoring_plan(store, records, principal, run, body, settings)
    job_id = str(derived_id("scoring-job", str(body.request_id)))
    if existing := await _job(records, principal, run, job_id):
        if not _same_job(existing, run, scorers, policies, (suite_id, suite_version), principal):
            raise HTTPException(409, "request_id belongs to a different scoring request")
        return public_job(existing)
    await writable_key(store, principal, run.project_id)
    if run.state != "completed":
        raise HTTPException(409, "Complete the target run before scoring saved outputs")
    if any(s.kind == "llm_judge" for s in scorers) and not worker.judges:
        raise HTTPException(422, "Model judges require a judge configured for this installation's worker")
    if any(j.state in ACTIVE for j in await records.list_scoring_jobs(principal, run.project_id, run.id)):
        raise HTTPException(409, "A scoring job is already active for this run")
    await ensure_scoring_place(store, records, principal, entitlements)
    job = await records.create_scoring_job(
        principal,
        run.project_id,
        ScoringJob(
            id=job_id,
            run_id=run.id,
            scorers=scorers,
            policies=policies,
            suite_id=suite_id,
            suite_version=suite_version,
            state="queued",
            requested_by=principal.user_id,
            created_at=_now(),
        ),
    )
    if job.run_id != run.id:
        raise HTTPException(409, "request_id belongs to a different scoring request")
    await execute.submit_or_fail(
        worker,
        records,
        Work(kind="scoring", principal=principal, project_id=run.project_id, run_id=run.id, job_id=job.id),
    )
    return public_job(job)


@router.post(
    "/{run_id:uuid}/scores/{job_id}/cancel",
    status_code=202,
    response_model=None,
    dependencies=[Depends(require_capability("score"))],
)
async def cancel_scoring(
    run_id: uuid.UUID, job_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    # A queued job is cancelled at once; a running one when its worker reaches the next case.
    run = await find_run(store, records, principal, run_id)
    for _ in range(3):
        job = await _job(records, principal, run, str(job_id))
        if job is None:
            raise HTTPException(404, "Not found")
        if job.cancel_requested_at is not None:
            return public_job(job)
        if job.state not in ACTIVE:
            raise HTTPException(409, "This scoring job has already finished")
        now = _now()
        changes: dict[str, object] = {"cancel_requested_at": now}
        if job.state == "queued":
            changes |= {"state": "cancelled", "finished_at": now}
        try:
            updated = await records.update_scoring_job(
                principal, run.project_id, job.model_copy(update=changes), expected_state=job.state
            )
            return public_job(updated)
        except StoreError as error:
            if error.kind != "conflict":
                raise
    raise HTTPException(409, "The scoring job changed while stopping it; try again")


@router.post(
    "/{run_id:uuid}/assessments/{assessment_id}/sync",
    status_code=202,
    response_model=None,
    dependencies=[Depends(require_capability("score"))],
)
async def sync(
    run_id: uuid.UUID, assessment_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    # Keep a stored score on its case's trace in the store, if it is not there yet.
    run = await find_run(store, records, principal, run_id)
    found = next(
        (s for s in await records.list_scores(principal, run.project_id, run.id) if score_id(s) == assessment_id), None
    )
    if found is None:
        raise HTTPException(404, "Not found")
    state = "unavailable"
    if found.store.assessment_id is None and found.value is not None and found.trace_id:
        found, delivery = await save_score(store, records, principal, run.project_id, found)
        state = "pending" if delivery is not None and delivery.state == "pending" else state
    return {"id": assessment_id, "telemetry_state": "persisted" if found.store.assessment_id else state}


@router.get("/monitoring", response_model=None, dependencies=[Depends(require_entitlement("monitoring"))])
async def monitoring(
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    project_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    pairs = [
        (job, run)
        for run in await project_runs(records, principal, project_key(project_id))
        for job in await records.list_scoring_jobs(principal, run.project_id, run.id)
    ]
    pairs.sort(key=lambda pair: (pair[0].created_at or datetime.min.replace(tzinfo=UTC), pair[0].id), reverse=True)
    items = []
    for job, run in pairs[(page - 1) * limit : page * limit]:
        results = [item_out(r) for r in await run_results(records, principal, run)]
        scores = [
            Assessment.of(s)
            for s in await records.list_scores(principal, run.project_id, run.id)
            if s.scoring_job_id == job.id
        ]
        config = job_config(job)
        suite = (
            await records.get_suite(principal, DEFAULT_PROJECT, job.suite_id, version=job.suite_version)
            if job.suite_id
            else None
        )
        items.append(
            {
                "job_id": job.id,
                "run_id": run.id,
                "run_code": run.code,
                "dataset_id": run.store.dataset_id or run.dataset_digest or "",
                "created_at": job.created_at,
                "state": job.state,
                "definition_digest": definition_digest(config),
                "suite_name": suite.name if suite else None,
                "suite_version": job.suite_version,
                "scorecard": scorecard(results, scores, config),
            }
        )
    return {
        "items": items,
        "total": len(pairs),
        "page": page,
        "has_more": page * limit < len(pairs),
        "note": MONITORING_NOTE,
    }


__all__ = ["find_suite_version", "router", "scoring_plan", "with_models"]
