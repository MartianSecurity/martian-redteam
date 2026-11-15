"""Human review: answering a result, review queues, the question library and trace annotations.

A port of ``POST /{run_id}/items/{item_id}/reviews`` and ``GET /reviews`` of the server's
``app/api/evals_workbench.py``, the review queue routes of ``app/api/evals_quality.py`` and the
question and annotation routes of ``app/api/evals_explorer.py`` (MartianSecurity/MartianBackend).

- A review is a new link in its case's chain for one criterion (``human_review``, or a queue
  question). A complete review also stores the person's answer as a human score, on the case's
  trace in the store too. The review's id is derived from ``request_id``.
- A review queue lives in the store with one item per case's trace; creating it adds a pending
  review per case and question, and each answer updates the case's item. Review queues and the
  question library need the ``review_queues`` feature.
- Store writes happen in the request: the store is the core's own, not a telemetry export, so the
  server's background sync tasks have no counterpart here.
- The core knows no organisation membership: an assignee is any user id, and only a role that may
  assign reviews can give another person's work to someone. The principal is compared, and a
  human score's source recorded, by the API's name for them (``projection.user_out``), the id
  the bootstrap gives, so the local user's own id is an assignee the routes accept.
- The store keeps one definition per question name, so the library has version 1 of each question.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from martian.evals.model import CaseResult, Review, ReviewQueue, Run, Score
from martian.evals.model import ReviewQuestion as Question
from martian.evals.records import RecordStore
from martian.evals.server.dependencies import (
    EntitlementsDep,
    PrincipalDep,
    RecordsDep,
    StoreDep,
    allows,
    ensure_entitled,
    require_capability,
    require_entitlement,
)
from martian.evals.server.principal import Principal
from martian.evals.server.projection import (
    DEFAULT_PROJECT,
    ReviewRow,
    derived_id,
    item_out,
    mlflow_assessment,
    project_key,
    queue_out,
    result_id,
    score_id,
    user_out,
)
from martian.evals.server.runs import (
    find_run,
    project_keys,
    project_runs,
    run_queues,
    run_results,
    writable_key,
    writable_project,
)
from martian.evals.server.traces import TRACE_ID
from martian.evals.server.write_schemas import (
    QuestionVersionRequest,
    QueueRequest,
    ReviewRequest,
    TraceAnnotationRequest,
)
from martian.evals.store import EvalStore, StoreError

router = APIRouter(prefix="/v1/evals", tags=["evals"])
explorer = APIRouter(prefix="/v1/evals", tags=["evaluation explorer"])

FINISHED = ("passed", "failed", "error", "skipped")
EPOCH = datetime.min.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def accepts(question: Question, value: Any) -> bool:
    if question.kind == "boolean":
        return type(value) is bool
    if question.kind == "numeric":
        return type(value) in (int, float) and math.isfinite(value) and question.minimum <= value <= question.maximum
    if question.kind == "categorical":
        return type(value) is str and value in question.options
    return type(value) is str and bool(value.strip()) and len(value) <= 4000


def _result(results: list[CaseResult], run: Run, item_id: uuid.UUID) -> CaseResult:
    found = next((r for r in results if result_id(run.id, r.case_id) == item_id), None)
    if found is None:
        raise HTTPException(404, "Not found")
    return found


def _latest(reviews: list[Review]) -> dict[tuple[str, str], Review]:
    """The newest review of each case and criterion."""
    latest: dict[tuple[str, str], Review] = {}
    for review in sorted(reviews, key=lambda r: (r.created_at or EPOCH, r.id)):
        latest[(review.case_id, review.criterion)] = review
    return latest


async def sync_queue_item(
    store: EvalStore, records: RecordStore, principal: Principal, run: Run, queue: ReviewQueue, result: CaseResult
) -> None:
    """Mark the case's queue item completed when every question of the queue has a complete answer
    in it, and pending otherwise."""
    if not result.store.trace_id:
        return
    reviews = await records.list_reviews(principal, run.project_id, run.id, case_id=result.case_id)
    answered = {r.criterion for r in reviews if r.queue_id == queue.id and r.state == "complete"}
    done = {q.name for q in queue.questions} <= answered
    await store.set_queue_item(
        principal, run.project_id, queue.id, result.store.trace_id, "completed" if done else "pending"
    )


# --- answering a result ---------------------------------------------------------------------


def _same_review(review: Review, result: CaseResult, criterion: str, body: ReviewRequest, principal: Principal) -> bool:
    def text(value: uuid.UUID | None) -> str | None:
        return None if value is None else str(value)

    return (
        review.case_id == result.case_id
        and review.criterion == criterion
        and review.reviewer == principal.user_id
        and (review.previous_id, review.assignee, review.queue_id)
        == (text(body.previous_id), text(body.assignee_id), text(body.queue_id))
        and (review.state, review.value, review.comment) == (body.state, body.value, body.comment)
        and type(review.value) is type(body.value)
    )


@router.post(
    "/{run_id:uuid}/items/{item_id}/reviews",
    status_code=201,
    response_model=None,
    dependencies=[Depends(require_capability("review"))],
)
async def review(
    run_id: uuid.UUID,
    item_id: uuid.UUID,
    body: ReviewRequest,
    principal: PrincipalDep,
    entitlements: EntitlementsDep,
    store: StoreDep,
    records: RecordsDep,
) -> dict[str, Any]:
    run = await find_run(store, records, principal, run_id)
    result = _result(await run_results(records, principal, run), run, item_id)
    queue = None
    criterion = "human_review"
    if body.queue_id is not None:
        ensure_entitled(entitlements, "review_queues")
        queue = await store.get_queue(principal, run.project_id, str(body.queue_id))
        if queue is None or queue.run_id != run.id or result.case_id not in queue.case_ids:
            raise HTTPException(404, "Not found")
        if body.question_name:
            question = next((q for q in queue.questions if q.name == body.question_name), None)
        else:
            question = queue.questions[0] if len(queue.questions) == 1 else None
        if question is None:
            raise HTTPException(422, "Choose a required question from this queue")
        criterion = question.name
        if body.state == "complete" and not accepts(question, body.value):
            raise HTTPException(422, "Rating differs from the queue question schema")
    review_id = str(derived_id("review", str(body.request_id)))
    chain = await records.list_reviews(principal, run.project_id, run.id, case_id=result.case_id, current_only=False)
    if old := next((r for r in chain if r.id == review_id), None):
        if not _same_review(old, result, criterion, body, principal):
            raise HTTPException(409, "request_id belongs to a different review")
        return ReviewRow.of(old).public()
    await writable_key(store, principal, run.project_id)
    if run.state != "completed" or result.status not in FINISHED:
        raise HTTPException(409, "Only finished results can be reviewed")
    previous = _latest([r for r in chain if r.criterion == criterion]).get((result.case_id, criterion))
    if previous is not None and previous.queue_id != (str(body.queue_id) if body.queue_id else None):
        raise HTTPException(409, "Keep the existing review question and queue audit chain")
    if (str(body.previous_id) if body.previous_id else None) != (previous.id if previous else None):
        raise HTTPException(409, "Review changed; reload before saving")
    me = user_out(principal.user_id)
    if not allows(principal, "assignReviews") and (
        (body.assignee_id is not None and str(body.assignee_id) != me)
        or (previous is not None and previous.assignee is not None and previous.assignee != me)
    ):
        raise HTTPException(403, "Only administrators can reassign another reviewer's work")
    score = None
    if body.state == "complete":
        metadata: dict[str, Any] = {"martian.review_criterion": criterion}
        if queue is not None:
            metadata["martian.queue_id"] = queue.id
        score = Score(
            run_id=run.id,
            case_id=result.case_id,
            trace_id=result.store.trace_id,
            scorer=criterion,
            source="human",
            source_id=me,
            value=body.value,
            passed=body.value if isinstance(body.value, bool) else None,
            rationale=body.comment,
            metadata=metadata,
            created_at=_now(),
        )
    saved = await records.add_review(
        principal,
        run.project_id,
        Review(
            id=review_id,
            run_id=run.id,
            case_id=result.case_id,
            criterion=criterion,
            state=body.state,
            value=body.value,
            comment=body.comment,
            reviewer=principal.user_id,
            assignee=str(body.assignee_id) if body.assignee_id else None,
            queue_id=str(body.queue_id) if body.queue_id else None,
            previous_id=str(body.previous_id) if body.previous_id else None,
            score_id=str(score_id(score)) if score else None,
            created_at=_now(),
        ),
    )
    if saved.run_id != run.id or saved.case_id != result.case_id:
        raise HTTPException(409, "request_id belongs to a different review")
    if score is not None:
        if score.trace_id:
            logged = await store.log_score(principal, run.project_id, score.trace_id, score)
            score = score.model_copy(update={"store": logged.store})
        await records.put_scores(principal, run.project_id, [score])
    if queue is not None:
        await sync_queue_item(store, records, principal, run, queue, result)
    return ReviewRow.of(saved).public()


@router.get("/reviews", response_model=None)
async def reviews(
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    q: Annotated[str, Query(max_length=200)] = "",
    filter: Annotated[str, Query(pattern="^(all|pending|complete|mine|disagreements)$")] = "all",
    queue_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    key = project_key(project_id)
    if queue_id is not None and await _find_queue(store, principal, str(queue_id)) is None:
        raise HTTPException(404, "Not found")
    me = user_out(principal.user_id)
    needle = q.casefold()
    rows: list[tuple[Review, Run, CaseResult]] = []
    for run in await project_runs(records, principal, key):
        latest = _latest(await records.list_reviews(principal, run.project_id, run.id, current_only=False))
        if not latest:
            continue
        results = {r.case_id: r for r in await run_results(records, principal, run)}
        for review in latest.values():
            result = results.get(review.case_id)
            if result is None:
                continue
            if queue_id is not None and review.queue_id != str(queue_id):
                continue
            if filter in ("pending", "complete") and review.state != filter:
                continue
            if filter == "mine" and review.assignee != me:
                continue
            if filter == "disagreements" and not (
                review.criterion == "human_review" and review.state == "complete" and _disagrees(review, result)
            ):
                continue
            if needle and not any(
                needle in text.casefold() for text in (item_out(result).prompt, review.comment, run.code)
            ):
                continue
            rows.append((review, run, result))
    rows.sort(key=lambda row: (row[0].created_at or EPOCH, row[0].id), reverse=True)
    shown = rows[(page - 1) * limit : page * limit]
    return {
        "truncated": False,
        "total": len(rows),
        "page": page,
        "limit": limit,
        "has_more": page * limit < len(rows),
        "items": [
            {
                **ReviewRow.of(review).public(),
                "run_id": run.id,
                "run_code": run.code,
                "prompt": item_out(result).prompt,
                "canonical_status": result.status,
                "disagreement": review.criterion == "human_review" and _disagrees(review, result),
            }
            for review, run, result in shown
        ],
    }


def _disagrees(review: Review, result: CaseResult) -> bool:
    return (
        type(review.value) is bool
        and result.status in ("passed", "failed")
        and review.value != (result.status == "passed")
    )


# --- review queues --------------------------------------------------------------------------


async def _find_queue(store: EvalStore, principal: Principal, queue_id: str) -> ReviewQueue | None:
    for key in await project_keys(store, principal):
        try:
            if queue := await store.get_queue(principal, key, queue_id):
                return queue
        except StoreError as error:
            if error.kind != "not_found":
                raise
    return None


def _same_queue(queue: ReviewQueue, run: Run, body: QueueRequest, cases: list[str], principal: Principal) -> bool:
    return (
        queue.run_id == run.id
        and queue.name == body.name.strip()
        and queue.case_ids == cases
        and [q.model_dump() for q in queue.questions] == [q.model_dump() for q in body.question_list()]
        and queue.assignee == (str(body.assignee_id) if body.assignee_id else None)
        and queue.created_by == principal.user_id
    )


@router.post(
    "/review-queues",
    status_code=201,
    response_model=None,
    dependencies=[Depends(require_entitlement("review_queues")), Depends(require_capability("createReviewQueues"))],
)
async def create_queue(
    body: QueueRequest, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    run = await find_run(store, records, principal, body.run_id)
    queue_id = str(derived_id("queue", str(body.request_id)))
    results = {result_id(run.id, r.case_id): r for r in await run_results(records, principal, run)}
    cases = [results[i].case_id for i in body.item_ids if i in results]
    if existing := await store.get_queue(principal, run.project_id, queue_id):
        if not _same_queue(existing, run, body, cases, principal):
            raise HTTPException(409, "request_id belongs to a different queue request")
        return queue_out(existing)
    await writable_key(store, principal, run.project_id)
    if run.state != "completed":
        raise HTTPException(409, "Review a completed run")
    me = user_out(principal.user_id)
    if body.assignee_id is not None and str(body.assignee_id) != me and not allows(principal, "assignReviews"):
        raise HTTPException(403, "Only administrators can assign another reviewer")
    if len(cases) != len(body.item_ids):
        raise HTTPException(404, "Not found")
    questions = [Question.model_validate(q.model_dump()) for q in body.question_list()]
    criteria = {q.name for q in questions}
    for case_id in cases:
        chain = await records.list_reviews(principal, run.project_id, run.id, case_id=case_id, current_only=False)
        if any(r.criterion in criteria for r in chain):
            raise HTTPException(409, "Selected results already have reviews for this criterion")
    chosen = [results[i] for i in body.item_ids]
    if any(not r.store.trace_id for r in chosen):
        raise HTTPException(409, "Only results with a saved trace can be reviewed in a queue")
    assignee = str(body.assignee_id) if body.assignee_id else None
    queue = await store.create_queue(
        principal,
        run.project_id,
        ReviewQueue(
            id=queue_id,
            run_id=run.id,
            name=body.name.strip(),
            questions=questions,
            case_ids=cases,
            assignee=assignee,
            created_by=principal.user_id,
            created_at=_now(),
        ),
        [r.store.trace_id or "" for r in chosen],
    )
    for case_id in cases:
        for question in questions:
            await records.add_review(
                principal,
                run.project_id,
                Review(
                    id=str(derived_id("queue-review", queue_id, case_id, question.name)),
                    run_id=run.id,
                    case_id=case_id,
                    criterion=question.name,
                    state="pending",
                    reviewer=principal.user_id,
                    assignee=assignee,
                    queue_id=queue_id,
                    created_at=_now(),
                ),
            )
    return queue_out(queue)


@router.get("/review-queues", response_model=None, dependencies=[Depends(require_entitlement("review_queues"))])
async def queues(
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    project_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    found = [
        (queue, run)
        for run in await project_runs(records, principal, project_key(project_id))
        for queue in await run_queues(store, records, principal, run)
    ]
    found.sort(key=lambda pair: (pair[0].created_at or EPOCH, pair[0].id), reverse=True)
    items = []
    for queue, run in found[(page - 1) * limit : page * limit]:
        required = {q.name for q in queue.questions}
        current = await records.list_reviews(principal, run.project_id, run.id)
        answers = [r for r in current if r.queue_id == queue.id]
        complete = {(r.case_id, r.criterion) for r in answers if r.state == "complete"}
        status = {item.case_id: item.status for item in await store.queue_items(principal, run.project_id, queue.id)}
        answered = len(complete)
        completed = sum(
            {c for case, c in complete if case == case_id} >= required and status.get(case_id) == "completed"
            for case_id in queue.case_ids
        )
        total_answers = len(queue.case_ids) * len(required)
        items.append(
            {
                **queue_out(queue),
                "completed": completed,
                "total": len(queue.case_ids),
                "answered": answered,
                "total_answers": total_answers,
                "pending_answers": 0,
                "status_pending": answered == total_answers and completed < len(queue.case_ids),
            }
        )
    return {
        "items": items,
        "total": len(found),
        "has_more": page * limit < len(found),
        "page": page,
        "limit": limit,
    }


@router.post(
    "/review-queues/{queue_id}/sync",
    status_code=202,
    response_model=None,
    dependencies=[Depends(require_entitlement("review_queues")), Depends(require_capability("review"))],
)
async def reconcile_queue(
    queue_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    # Set every item of the queue from its answers again.
    queue = await _find_queue(store, principal, str(queue_id))
    if queue is None:
        raise HTTPException(404, "Not found")
    run = await find_run(store, records, principal, uuid.UUID(queue.run_id))
    results = {r.case_id: r for r in await run_results(records, principal, run)}
    for case_id in queue.case_ids:
        if case_id in results:
            await sync_queue_item(store, records, principal, run, queue, results[case_id])
    return queue_out(queue)


# --- the question library -------------------------------------------------------------------


def question_out(project: str, question: Question) -> dict[str, Any]:
    return {
        "id": str(derived_id("question", project, question.name)),
        "version": 1,
        "question": question.model_dump(mode="json"),
    }


@explorer.get("/review-questions", response_model=None, dependencies=[Depends(require_entitlement("review_queues"))])
async def question_catalog(
    principal: PrincipalDep, store: StoreDep, project_id: uuid.UUID | None = None
) -> dict[str, Any]:
    key = project_key(project_id)
    if await store.get_project(principal, key) is None:
        if key != DEFAULT_PROJECT:
            raise HTTPException(404, "Not found")
        return {"items": []}
    found = sorted(await store.list_questions(principal, key), key=lambda q: q.name)
    return {"items": [question_out(key, q) for q in found]}


@explorer.post(
    "/review-questions",
    status_code=201,
    response_model=None,
    dependencies=[Depends(require_entitlement("review_queues")), Depends(require_capability("createReviewQueues"))],
)
async def question_version(body: QuestionVersionRequest, principal: PrincipalDep, store: StoreDep) -> dict[str, Any]:
    key = await writable_project(store, principal, body.project_id)
    asked = body.question
    if asked.kind != "numeric":
        asked = asked.model_copy(update={"minimum": 0, "maximum": 1})
    question = Question.model_validate(asked.model_dump())
    current = next((q for q in await store.list_questions(principal, key) if q.name == question.name), None)
    if current is not None and body.expected_version == 0:
        if current != question:
            raise HTTPException(409, "Question version already has a different definition")
        return question_out(key, current)
    if (1 if current else 0) != body.expected_version:
        raise HTTPException(409, "Question changed; reload its latest version")
    if current is not None:
        raise HTTPException(409, "Each question name has one definition; save the new one under another name")
    return question_out(key, await store.save_question(principal, key, question))


# --- trace annotations ----------------------------------------------------------------------


def annotation_out(score: Score, trace_id: str, kind: str) -> dict[str, Any]:
    found = mlflow_assessment(score, trace_id)
    if kind == "expectation":
        found["expectation"] = {"value": found.pop("feedback")["value"]}
    return found


@explorer.post(
    "/traces/{trace_id}/annotations",
    status_code=201,
    response_model=None,
    dependencies=[Depends(require_capability("review"))],
)
async def create_trace_annotation(
    body: TraceAnnotationRequest,
    trace_id: Annotated[str, Path(pattern=TRACE_ID)],
    principal: PrincipalDep,
    store: StoreDep,
) -> dict[str, Any]:
    key = await writable_project(store, principal, body.project_id)
    trace = await store.get_trace(principal, key, trace_id)
    if trace is None:
        raise HTTPException(404, "Trace not found")
    marker, me = str(body.request_id), user_out(principal.user_id)
    found = next(
        (s for s in trace.scores if s.metadata.get("martian.request_id") == marker and s.source_id == me),
        None,
    )
    if found is None:
        found = await store.log_score(
            principal,
            key,
            trace_id,
            Score(
                trace_id=trace_id,
                scorer=body.name,
                source="human",
                source_id=me,
                value=body.value,
                passed=body.value if isinstance(body.value, bool) else None,
                rationale=body.rationale,
                metadata={"martian.request_id": marker, "martian.annotation": body.kind},
                created_at=_now(),
            ),
        )
    if (
        found.scorer != body.name
        or found.value != body.value
        or isinstance(found.value, bool) != isinstance(body.value, bool)
        or found.rationale != body.rationale
        or found.metadata.get("martian.annotation") != body.kind
    ):
        raise HTTPException(409, "This annotation request already has different content")
    return annotation_out(found, trace_id, body.kind)


__all__ = ["accepts", "explorer", "router", "sync_queue_item"]
