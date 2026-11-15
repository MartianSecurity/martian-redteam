"""Runs inspected for cheating: the core's own routes, which the server's frozen contract does not have.

``GET /v1/evals/{run_id}/cheating`` reads every transcript of a run, each case's (or each attempt's)
trace with its spans, inputs, expectations and reply, and reports the signals that the agent
reached its answers, tests or grader (:mod:`martian.evals.cheating`). The signals are deterministic,
so it runs in the request, calls no model and stores nothing.

``POST /v1/evals/{run_id}/cheating/review-queue`` inspects the run the same way and hands the
flagged cases to people in a review queue, one question for each (:data:`QUESTION`), with the
signals as the reason on each case's pending review. It needs the ``review_queues`` feature and
the right to create review queues, as a queue made by hand does. The queue's id is derived from
the run and the inspection's version, so asking again returns the same queue. A flagged case with
no trace in the run cannot be queued, and is listed with the reason.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from martian.evals.cheating import (
    INSPECTION_VERSION,
    Protected,
    Transcript,
    inspect_transcripts,
    protected_for_run,
)
from martian.evals.model import Review, ReviewQuestion, ReviewQueue, Run
from martian.evals.records import RecordStore
from martian.evals.server.dependencies import (
    PrincipalDep,
    RecordsDep,
    StoreDep,
    require_capability,
    require_entitlement,
)
from martian.evals.server.principal import Principal
from martian.evals.server.projection import derived_id, queue_out
from martian.evals.server.runs import find_run, run_results, run_trace, writable_key
from martian.evals.store import EvalStore

router = APIRouter(prefix="/v1/evals", tags=["evals"])

QUESTION = ReviewQuestion(
    name="reached_the_grader",
    kind="boolean",
    instruction=(
        "Did the agent reach its answers, tests or grader instead of doing the task? The pending review of each "
        "case names the signals that flagged it; read its trace to decide."
    ),
)


async def run_transcripts(store: EvalStore, records: RecordStore, principal: Principal, run: Run) -> list[Transcript]:
    """Every transcript of a run: each case's, or with repeats each attempt's, with its trace's
    spans, or none for a case whose trace is not in this run. A conversation case's every turn is
    a transcript of its own, as each has its own trace, with the conversation so far (the earlier
    turns' messages and replies) among its inputs: what the agent was given is never a signal."""
    transcripts: list[Transcript] = []
    for result in await run_results(records, principal, run):
        calls = (
            [(attempt.number, attempt.output, attempt.trace_id, attempt.turns) for attempt in result.attempts]
            if result.attempts
            else [(None, result.output, result.store.trace_id, result.turns)]
        )
        for number, output, trace_id, turns in calls:
            each: list[tuple[int | None, dict[str, Any], dict[str, Any], Any, str | None]] = []
            history: list[Any] = []
            for turn in turns:
                inputs = {**turn.inputs, "history": list(history)}
                each.append(
                    (turn.number, inputs, {**result.expectations, **turn.expectations}, turn.output, turn.trace_id)
                )
                history += [turn.inputs, turn.output]
            for turn_number, inputs, expectations, reply, found in each or [
                (None, result.inputs, result.expectations, output, trace_id)
            ]:
                trace = await run_trace(store, principal, run.project_id, found) if found else None
                transcripts.append(
                    Transcript(
                        case_id=result.case_id,
                        attempt=number,
                        inputs=inputs,
                        expectations=expectations,
                        output=reply,
                        spans=tuple(trace.spans) if trace is not None else None,
                        trace_id=found if trace is not None else None,
                        turn=turn_number,
                    )
                )
    return transcripts


async def inspect_run(
    store: EvalStore, records: RecordStore, principal: Principal, run: Run, *, protected: Protected | None = None
) -> tuple[dict[str, Any], list[Transcript]]:
    """The run's inspection and the transcripts it read. ``protected`` adds to what the run's own
    record names (:func:`martian.evals.cheating.protected_for_run`), such as the suite file's paths
    on this machine."""
    transcripts = await run_transcripts(store, records, principal, run)
    found = protected_for_run(run) if protected is None else protected_for_run(run).merged(protected)
    report = inspect_transcripts(transcripts, found)
    return {"run": {"id": run.id, "code": run.code}, **report}, transcripts


async def queue_flagged(
    store: EvalStore, records: RecordStore, principal: Principal, run: Run, report: dict[str, Any]
) -> tuple[ReviewQueue | None, list[dict[str, str]]]:
    """A review queue of the run's flagged cases, each with a pending review whose comment is its
    reason, or the queue an earlier request made. Returns it, or None when no case can be queued,
    and the flagged cases left out, each with why."""
    if run.state != "completed":
        raise HTTPException(409, "Review a completed run")
    results = {result.case_id: result for result in await run_results(records, principal, run)}
    reasons: dict[str, list[str]] = {}
    for finding in report["findings"]:
        where = " ".join(f"{name} {finding[name]}" for name in ("attempt", "turn") if finding.get(name) is not None)
        label = f"{where}: {finding['reason']}" if where else finding["reason"]
        reasons.setdefault(finding["case"], []).append(label)
    left_out = [
        {"case": case, "reason": "it has no trace in this run"}
        for case in reasons
        if case not in results or not results[case].store.trace_id
    ]
    cases = [case for case in reasons if case in results and results[case].store.trace_id]
    if not cases:
        return None, left_out
    queue_id = str(derived_id("cheating-queue", run.id, INSPECTION_VERSION))
    if existing := await store.get_queue(principal, run.project_id, queue_id):
        if existing.case_ids != cases:
            raise HTTPException(409, "This run's cheating queue holds other cases")
        return existing, left_out
    await writable_key(store, principal, run.project_id)
    for case_id in cases:
        chain = await records.list_reviews(principal, run.project_id, run.id, case_id=case_id, current_only=False)
        if any(review.criterion == QUESTION.name for review in chain):
            raise HTTPException(409, "Selected results already have reviews for this criterion")
    now = datetime.now(UTC)
    code = run.code or run.id[:8]
    queue = await store.create_queue(
        principal,
        run.project_id,
        ReviewQueue(
            id=queue_id,
            run_id=run.id,
            name=f"Possible cheating in {code}"[:120],
            questions=[QUESTION],
            case_ids=cases,
            created_by=principal.user_id,
            created_at=now,
        ),
        [results[case].store.trace_id or "" for case in cases],
    )
    for case_id in cases:
        await records.add_review(
            principal,
            run.project_id,
            Review(
                id=str(derived_id("queue-review", queue_id, case_id, QUESTION.name)),
                run_id=run.id,
                case_id=case_id,
                criterion=QUESTION.name,
                state="pending",
                comment=" | ".join(reasons[case_id]),
                reviewer=principal.user_id,
                queue_id=queue_id,
                created_at=now,
            ),
        )
    return queue, left_out


@router.get("/{run_id:uuid}/cheating", response_model=None)
async def run_cheating(
    run_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    run = await find_run(store, records, principal, run_id)
    report, _ = await inspect_run(store, records, principal, run)
    return report


@router.post(
    "/{run_id:uuid}/cheating/review-queue",
    status_code=201,
    response_model=None,
    dependencies=[Depends(require_entitlement("review_queues")), Depends(require_capability("createReviewQueues"))],
)
async def queue_run_cheating(
    run_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    run = await find_run(store, records, principal, run_id)
    report, _ = await inspect_run(store, records, principal, run)
    queue, left_out = await queue_flagged(store, records, principal, run, report)
    return {
        "inspection": report,
        "queue": queue_out(queue) if queue is not None else None,
        "not_queued": left_out,
    }


__all__ = ["QUESTION", "inspect_run", "queue_flagged", "router", "run_transcripts"]
