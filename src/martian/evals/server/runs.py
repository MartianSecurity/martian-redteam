"""Read routes for projects, runs, their results and comparisons: a port of the server's
``app/api/evals.py``, ``app/api/evals_workbench.py`` and ``GET /projects`` of
``app/api/evals_explorer.py`` (MorningHQ/MartianBackend) onto the store and the record store.

Runs, case results, scores, scoring jobs and reviews come from the record store; projects, traces,
run telemetry and review queues from the store. A run is found by its id in the default project,
then in each of the store's projects. The run routes match only a UUID in place of ``{run_id}``,
so a route an edition adds under ``/v1/evals`` is not taken for a run.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from martian.evals.model import CaseResult, Project, ReviewQueue, Run, ScoringJob, Trace
from martian.evals.records import RecordStore
from martian.evals.server.capabilities import highest_role
from martian.evals.server.dependencies import AgentNames, AgentNamesDep, PrincipalDep, RecordsDep, StoreDep
from martian.evals.server.principal import Principal
from martian.evals.server.projection import (
    DEFAULT_PROJECT,
    RUN_STATUS,
    Assessment,
    ReviewRow,
    agent_of,
    item_out,
    job_config,
    mlflow_assessment,
    project_key,
    public_job,
    queue_out,
    result_id,
    run_detail,
    run_out,
    trace_view,
)
from martian.evals.server.quality import calibration, compare_assessments, compare_runs, scorecard
from martian.evals.server.schemas import EvalRunDetailOut, EvalRunOut
from martian.evals.store import EvalStore, StoreError

router = APIRouter(prefix="/v1/evals", tags=["evals"])
explorer = APIRouter(prefix="/v1/evals", tags=["evaluation explorer"])

EPOCH = datetime.min.replace(tzinfo=UTC)
NOT_PERSISTED = "A native run has not been persisted for this evaluation."
PROVENANCE_NOTE = (
    "Dataset and scorer versions identify evaluation inputs. Customer-declared artifact revisions and reported "
    "model/prompt versions retain their source; missing versions remain unavailable."
)


# --- lookups --------------------------------------------------------------------------------


async def agent_names(names: AgentNames | None, principal: Principal, runs: Sequence[Run]) -> dict[str, str]:
    """The display names of the runs' agents, by agent id, as far as the edition can name them."""
    ids = sorted({agent for run in runs if (agent := agent_of(run))})
    return await names(principal, ids) if names is not None and ids else {}


def name_of(found: dict[str, str], run: Run) -> str | None:
    return found.get(agent_of(run) or "")


async def project_keys(store: EvalStore, principal: Principal) -> list[str]:
    """The default project, then every project of the store."""
    return [DEFAULT_PROJECT, *(p.id for p in await store.list_projects(principal) if p.id != DEFAULT_PROJECT)]


async def lookup_run(store: EvalStore, records: RecordStore, principal: Principal, run_id: str) -> Run | None:
    for key in await project_keys(store, principal):
        if run := await records.get_run(principal, key, run_id):
            return run
    return None


async def find_run(store: EvalStore, records: RecordStore, principal: Principal, run_id: uuid.UUID) -> Run:
    if run := await lookup_run(store, records, principal, str(run_id)):
        return run
    raise HTTPException(404, "Not found")


async def writable_project(store: EvalStore, principal: Principal, project_id: uuid.UUID | None) -> str:
    """The store's project a write goes to. The default project is created on its first write;
    another project must exist, and an archived project takes no more work."""
    return await writable_key(store, principal, project_key(project_id))


async def writable_key(store: EvalStore, principal: Principal, key: str) -> str:
    """:func:`writable_project` for a project named by its store id."""
    project = await store.get_project(principal, key)
    if project is None:
        if key != DEFAULT_PROJECT:
            raise HTTPException(404, "Not found")
        project = await store.create_project(
            principal, Project(id=DEFAULT_PROJECT, name="Default", description="Organization evaluation project")
        )
    if project.archived_at is not None:
        raise HTTPException(409, "This evaluation project is archived or unavailable")
    return key


async def project_runs(records: RecordStore, principal: Principal, project_id: str) -> list[Run]:
    runs: list[Run] = []
    cursor = None
    while True:
        page = await records.list_runs(principal, project_id, limit=500, cursor=cursor)
        runs.extend(page.items)
        if (cursor := page.next_cursor) is None:
            return runs


async def run_results(records: RecordStore, principal: Principal, run: Run) -> list[CaseResult]:
    results: list[CaseResult] = []
    while True:
        page = await records.list_results(principal, run.project_id, run.id, offset=len(results), limit=1000)
        results.extend(page.items)
        if page.next_cursor is None:
            return results


async def run_trace(store: EvalStore, principal: Principal, project_id: str, trace_id: str) -> Trace | None:
    try:
        return await store.get_trace(principal, project_id, trace_id)
    except StoreError as error:
        if error.kind == "not_found":
            return None
        raise


async def run_queues(store: EvalStore, records: RecordStore, principal: Principal, run: Run) -> list[ReviewQueue]:
    """The run's review queues, newest first. The store cannot list queues; creating one adds a
    pending review per case and question, so the run's reviews name every queue it has."""
    reviews = await records.list_reviews(principal, run.project_id, run.id, current_only=False)
    queues = []
    for queue_id in dict.fromkeys(r.queue_id for r in reviews if r.queue_id):
        if (queue := await store.get_queue(principal, run.project_id, queue_id)) is not None:
            queues.append(queue)
    return _newest(queues, lambda q: (q.created_at or EPOCH, q.id))


def _newest[T](rows: Sequence[T], key: Callable[[T], Any]) -> list[T]:
    return sorted(rows, key=key, reverse=True)


def _page[T](rows: Sequence[T], number: int, limit: int) -> tuple[list[T], dict[str, Any]]:
    return list(rows[(number - 1) * limit : number * limit]), {
        "page": number,
        "limit": limit,
        "total": len(rows),
        "has_more": number * limit < len(rows),
    }


def project_row(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "state": "archived" if project.archived_at else "ready",
        "created_at": project.created_at,
        "archived_at": project.archived_at,
    }


# --- projects -------------------------------------------------------------------------------


@explorer.get("/projects", response_model=None)
async def projects(principal: PrincipalDep, store: StoreDep) -> dict[str, Any]:
    found = [p for p in await store.list_projects(principal) if p.id != DEFAULT_PROJECT]
    found.sort(key=lambda p: (p.created_at or EPOCH, p.id))
    return {
        "items": [
            {"id": None, "name": "Default", "description": "Organization evaluation project", "state": "ready"},
            *(project_row(p) for p in found),
        ],
        "datasets_scope": "organization",
    }


# --- comparison (before /{run_id}) ----------------------------------------------------------


async def _job(records: RecordStore, principal: Principal, run: Run, job_id: uuid.UUID) -> ScoringJob:
    job = next(
        (j for j in await records.list_scoring_jobs(principal, run.project_id, run.id) if j.id == str(job_id)), None
    )
    if job is None:
        raise HTTPException(404, "Not found")
    return job


async def _job_scores(records: RecordStore, principal: Principal, run: Run, job: ScoringJob) -> list[Assessment]:
    scores = await records.list_scores(principal, run.project_id, run.id)
    return [Assessment.of(s) for s in scores if s.scoring_job_id == job.id]


@router.get("/compare", response_model=None)
async def compare(
    baseline_id: uuid.UUID,
    candidate_id: uuid.UUID,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    baseline_job_id: uuid.UUID | None = None,
    candidate_job_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    baseline = await find_run(store, records, principal, baseline_id)
    candidate = await find_run(store, records, principal, candidate_id)
    left_items = [item_out(r) for r in await run_results(records, principal, baseline)]
    right_items = [item_out(r) for r in await run_results(records, principal, candidate)]
    result = compare_runs(baseline, left_items, candidate, right_items)
    if bool(baseline_job_id) != bool(candidate_job_id):
        raise HTTPException(422, "Choose both scoring jobs")
    if baseline_job_id is None or candidate_job_id is None:
        return result
    left = await _job(records, principal, baseline, baseline_job_id)
    right = await _job(records, principal, candidate, candidate_job_id)
    if left.state in ("queued", "running") or right.state in ("queued", "running"):
        raise HTTPException(409, "Wait for scoring to finish before comparing")
    return compare_assessments(
        result,
        left_items,
        right_items,
        public_job(left),
        public_job(right),
        await _job_scores(records, principal, baseline, left),
        await _job_scores(records, principal, candidate, right),
    )


# --- runs -----------------------------------------------------------------------------------


@router.get("", response_model=list[EvalRunOut])
async def list_runs(
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    names: AgentNamesDep,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    project_id: uuid.UUID | None = None,
    default_only: bool = False,
    q: Annotated[str, Query(max_length=200)] = "",
) -> list[EvalRunOut]:
    if project_id is not None:
        if await store.get_project(principal, str(project_id)) is None:
            raise HTTPException(404, "Not found")
        keys = [str(project_id)]
    else:
        keys = [DEFAULT_PROJECT] if default_only else await project_keys(store, principal)
    needle = q.casefold()
    runs = [
        run
        for key in keys
        for run in await project_runs(records, principal, key)
        if needle in run.code.casefold() or needle in run.dataset_name.casefold()
    ]
    runs = _newest(runs, lambda r: (r.created_at or EPOCH, r.id))[(page - 1) * limit : page * limit]
    found = await agent_names(names, principal, runs)
    return [run_out(r, name_of(found, r)) for r in runs]


@router.get("/{run_id:uuid}", response_model=EvalRunDetailOut)
async def get_run(
    run_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep, names: AgentNamesDep
) -> EvalRunDetailOut:
    run = await find_run(store, records, principal, run_id)
    found = await agent_names(names, principal, [run])
    return run_detail(run, await run_results(records, principal, run), name_of(found, run))


@router.get("/{run_id:uuid}/workbench", response_model=None)
async def workbench(
    run_id: uuid.UUID,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    job_id: uuid.UUID | None = None,
    jobs_page: Annotated[int, Query(ge=1)] = 1,
    assessments_page: Annotated[int, Query(ge=1)] = 1,
    reviews_page: Annotated[int, Query(ge=1)] = 1,
    item_id: uuid.UUID | None = None,
    queue_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    # Scoring jobs, scores and reviews of a run's results, newest first, with the selected job's
    # scorecard and the judges' agreement with human reviews.
    run = await find_run(store, records, principal, run_id)
    items = [item_out(r) for r in await run_results(records, principal, run)]
    jobs = _newest(
        await records.list_scoring_jobs(principal, run.project_id, run.id), lambda j: (j.created_at or EPOCH, j.id)
    )
    job_rows, jobs_pagination = _page(jobs, jobs_page, 25)
    selected = None
    if job_id is not None:
        selected = next((j for j in jobs if j.id == str(job_id)), None)
        if selected is None:
            raise HTTPException(404, "Not found")
    elif job_rows:
        selected = job_rows[0]
    config = job_config(selected) if selected else None

    scores = [Assessment.of(s) for s in await records.list_scores(principal, run.project_id, run.id)]
    if item_id is not None:
        shown = [s for s in scores if s.item_id == item_id]
    else:
        shown = [s for s in scores if s.job_id == (selected.id if selected else None)]
    score_rows, scores_pagination = _page(
        _newest(shown, lambda s: (s.created_at or EPOCH, str(s.id))), assessments_page, 100
    )
    selected_scores = [s for s in scores if selected and s.job_id == selected.id]

    reviews = _newest(
        [ReviewRow.of(r) for r in await records.list_reviews(principal, run.project_id, run.id, current_only=False)],
        lambda r: (r.created_at or EPOCH, r.id),
    )
    history = [r for r in reviews if item_id is None or r.item_id == item_id]
    review_rows, reviews_pagination = _page(history, reviews_page, 100)
    criteria = config["scorers"] if config else ["human_review"]
    latest: dict[tuple[uuid.UUID, str], ReviewRow] = {}
    for review in reviews:
        if (review.item_id == item_id) if item_id is not None else review.criterion in criteria:
            latest.setdefault((review.item_id, review.criterion), review)
    latest_reviews = [latest[key] for key in sorted(latest, key=lambda key: (str(key[0]), key[1]))][:801]

    queues = (await run_queues(store, records, principal, run))[:100]
    if queue_id is not None:
        queue = await store.get_queue(principal, run.project_id, str(queue_id))
        case_ids = {item.id: item.dataset_item_id for item in items}
        if (
            queue is None
            or queue.run_id != run.id
            or (item_id is not None and case_ids.get(item_id) not in queue.case_ids)
        ):
            raise HTTPException(404, "Not found")
        if all(q.id != queue.id for q in queues):
            queues.insert(0, queue)
    return {
        "jobs": [public_job(j) for j in job_rows],
        "assessments": [s.public() for s in score_rows],
        "reviews": [r.public() for r in review_rows],
        "latest_reviews": [r.public() for r in latest_reviews[:800]],
        "latest_reviews_truncated": len(latest_reviews) > 800,
        # The core knows only the principal of the request, not the organisation's other members.
        "members": [
            {
                "id": principal.user_id,
                "name": principal.display_name or principal.email or principal.user_id,
                "role": highest_role(principal.roles),
            }
        ],
        "queues": [queue_out(q) for q in queues],
        "selected_job": public_job(selected) if selected else None,
        "pagination": {"jobs": jobs_pagination, "assessments": scores_pagination, "reviews": reviews_pagination},
        "truncated": False,
        "scorecard": scorecard(items, selected_scores, config) if config else None,
        "calibration": calibration(selected_scores, latest_reviews),
    }


@router.get(
    "/{run_id:uuid}/provenance",
    response_model=None,
    openapi_extra={"x-mlflow-shaped": "`native` carries MLflow's run status and metric objects"},
)
async def provenance(
    run_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    # The run as it was admitted, and its run in the store.
    run = await find_run(store, records, principal, run_id)
    try:
        native = await store.get_run(principal, run.project_id, run.id)
    except StoreError as error:
        if error.kind != "not_found":
            raise
        native = None
    if native is None:
        return {"admission": run.provenance, "native": None, "note": NOT_PERSISTED}
    if native.id != run.id or native.dataset_digest != run.dataset_digest:
        raise HTTPException(409, "Native run provenance differs from its admission")
    return {
        "admission": run.provenance,
        "native": {
            "id": native.store.run_id or native.id,
            "status": RUN_STATUS[native.state],
            "metrics": [{"key": key, "value": float(value)} for key, value in native.counts.model_dump().items()],
            "datasets": [{"name": native.dataset_name, "digest": native.dataset_digest}],
        },
        "note": PROVENANCE_NOTE,
    }


@router.get(
    "/{run_id:uuid}/items/{item_id}/trace",
    response_model=None,
    openapi_extra={"x-mlflow-shaped": "`annotations` is MLflow's list of assessments"},
)
async def trace(
    run_id: uuid.UUID, item_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    run = await find_run(store, records, principal, run_id)
    result = next(
        (r for r in await run_results(records, principal, run) if result_id(run.id, r.case_id) == item_id), None
    )
    if result is None:
        raise HTTPException(404, "Not found")
    if not result.store.trace_id:
        raise HTTPException(409, "Trace is not available; inspect telemetry status")
    found = await run_trace(store, principal, run.project_id, result.store.trace_id)
    if found is None:
        raise HTTPException(404, "Trace not found")
    if found.run_id != run.id:
        raise HTTPException(409, "Trace does not belong to this result")
    return {
        **trace_view(found, result.store.trace_id),
        "annotations": [mlflow_assessment(score, found.id) for score in found.scores],
    }


__all__ = [
    "explorer",
    "find_run",
    "lookup_run",
    "project_keys",
    "project_row",
    "project_runs",
    "router",
    "run_queues",
    "run_results",
    "writable_key",
    "writable_project",
]
