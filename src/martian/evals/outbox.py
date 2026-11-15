"""The outbox: a person's answer never waits on the store.

A score a person gives (a review answer, or keeping a stored score on its trace) and a review
queue item's status are saved in the record store first. The write to the store is then a
:class:`~martian.evals.records.Delivery` kept in the record store's outbox: it is attempted at once,
in the request, and when the store cannot take it (MLflow is down, say) the request still succeeds
and the delivery waits. :func:`deliver_due` retries what is due, with a growing pause between
attempts, and :func:`run_deliveries` does that in a loop for the principals an edition serves.

Every attempt reads back first, so a write that reached the store before an attempt failed is not
made twice: a score carries its delivery's id in its metadata (``martian.delivery``), and a queue
item is set only when its status differs. A delivery of the same queue item replaces the pending
one, so the newest status is the one delivered. A store refusal of kind ``invalid`` ends the
delivery as ``failed``; anything else is retried.

The outbox is one table and a loop, not a job queue. The durable job queue
(MorningHQ/martian-notes#139) can later take ``deliver_due`` as one of its kinds of work.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from martian.evals.model import Score, digest
from martian.evals.records import Delivery, RecordStore
from martian.evals.store import EvalStore, StoreError

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

log = logging.getLogger(__name__)

MARKER = "martian.delivery"
FIRST_PAUSE = timedelta(seconds=5)
LONGEST_PAUSE = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(UTC)


def pause(attempts: int) -> timedelta:
    """How long to wait after ``attempts`` failed attempts: 5 s, doubling, at most 15 minutes."""
    longer: timedelta = FIRST_PAUSE * (2 ** max(attempts - 1, 0))
    return min(longer, LONGEST_PAUSE)


def score_delivery(project_id: str, score: Score) -> Delivery:
    identity = [score.run_id, score.case_id, score.trace_id, score.scorer, score.scoring_job_id, score.created_at]
    return Delivery(
        id="score-" + digest([str(part) for part in identity])[:40],
        project_id=project_id,
        kind="score",
        payload={"score": score.model_dump(mode="json")},
    )


def queue_item_delivery(
    project_id: str, queue_id: str, trace_id: str, status: Literal["pending", "completed"]
) -> Delivery:
    return Delivery(
        id="queue-item-" + digest([queue_id, trace_id])[:40],
        project_id=project_id,
        kind="queue_item",
        payload={"queue_id": queue_id, "trace_id": trace_id, "status": status},
    )


async def _deliver_score(store: EvalStore, records: RecordStore, principal: Principal, delivery: Delivery) -> None:
    score = Score.model_validate(delivery.payload["score"])
    project, trace_id = delivery.project_id, score.trace_id
    if trace_id is None:
        raise StoreError("invalid", "a delivered score names its trace")
    found = next(
        (s for s in await store.list_scores(principal, project, trace_id) if s.metadata.get(MARKER) == delivery.id),
        None,
    )
    if found is None:
        marked = score.model_copy(update={"metadata": {**score.metadata, MARKER: delivery.id}})
        found = await store.log_score(principal, project, trace_id, marked)
    if score.run_id is None or score.case_id is None:
        return
    # Keep the store's id with the record store's score, unless a newer answer has replaced it.
    for kept in await records.list_scores(principal, project, score.run_id, case_id=score.case_id):
        same = (kept.scorer, kept.scoring_job_id, kept.created_at, kept.value) == (
            score.scorer,
            score.scoring_job_id,
            score.created_at,
            score.value,
        )
        if same and kept.store.assessment_id != found.store.assessment_id:
            await records.put_scores(principal, project, [kept.model_copy(update={"store": found.store})])


async def _deliver_queue_item(store: EvalStore, principal: Principal, delivery: Delivery) -> None:
    queue_id, trace_id, status = (delivery.payload[k] for k in ("queue_id", "trace_id", "status"))
    items = await store.queue_items(principal, delivery.project_id, queue_id)
    if not any(item.trace_id == trace_id and item.status == status for item in items):
        await store.set_queue_item(principal, delivery.project_id, queue_id, trace_id, status)


async def attempt(store: EvalStore, records: RecordStore, principal: Principal, delivery: Delivery) -> Delivery:
    """One attempt at a delivery, settled in the record store: delivered, failed for good, or due
    again after a pause."""
    now = _now()
    try:
        if delivery.kind == "score":
            await _deliver_score(store, records, principal, delivery)
        else:
            await _deliver_queue_item(store, principal, delivery)
    except Exception as error:  # noqa: BLE001 - every failure is recorded and retried or ended
        attempts = delivery.attempts + 1
        permanent = isinstance(error, StoreError) and error.kind == "invalid"
        reason = error.detail if isinstance(error, StoreError) else type(error).__name__
        if not isinstance(error, StoreError):
            log.warning("delivery %s failed (%s); retrying", delivery.id, type(error).__name__)
        settled = delivery.model_copy(
            update={
                "state": "failed" if permanent else "pending",
                "attempts": attempts,
                "error": reason[:300],
                "next_attempt_at": now + pause(attempts),
            }
        )
    else:
        settled = delivery.model_copy(
            update={"state": "delivered", "attempts": delivery.attempts + 1, "error": "", "delivered_at": now}
        )
    return await records.settle_delivery(principal, delivery.project_id, settled)


async def save_score(
    store: EvalStore, records: RecordStore, principal: Principal, project_id: str, score: Score
) -> tuple[Score, Delivery | None]:
    """Save a person's score in the record store, then deliver it to its trace: at once when the store
    takes it, otherwise later. Returns the score as kept, with the store's id once delivered, and the
    delivery (``None`` for a score with no trace)."""
    await records.put_scores(principal, project_id, [score])
    if score.trace_id is None:
        return score, None
    waiting = await records.put_delivery(principal, project_id, score_delivery(project_id, score))
    delivery = await attempt(store, records, principal, waiting)
    if delivery.state == "delivered" and score.run_id is not None and score.case_id is not None:
        kept = await records.list_scores(principal, project_id, score.run_id, case_id=score.case_id)
        score = next((s for s in kept if (s.scorer, s.scoring_job_id) == (score.scorer, score.scoring_job_id)), score)
    return score, delivery


async def save_queue_item(
    store: EvalStore,
    records: RecordStore,
    principal: Principal,
    project_id: str,
    queue_id: str,
    trace_id: str,
    status: Literal["pending", "completed"],
) -> Delivery:
    """Set a queue item's status in the store, at once when it takes it, otherwise later."""
    delivery = queue_item_delivery(project_id, queue_id, trace_id, status)
    return await attempt(store, records, principal, await records.put_delivery(principal, project_id, delivery))


async def deliver_due(
    store: EvalStore, records: RecordStore, principal: Principal, *, now: datetime | None = None, limit: int = 100
) -> int:
    """Attempt every delivery of the principal's space that is due; the number delivered."""
    delivered = 0
    for delivery in await records.due_deliveries(principal, now=now or _now(), limit=limit):
        delivered += (await attempt(store, records, principal, delivery)).state == "delivered"
    return delivered


async def run_deliveries(
    store: EvalStore,
    records: RecordStore,
    principals: Callable[[], Awaitable[list[Principal]]],
    *,
    interval: float = 5.0,
) -> None:
    """Deliver what is due for each principal ``principals`` names, every ``interval`` seconds, until
    cancelled. The server names its organisations; the local edition its one user."""
    while True:
        for principal in await principals():
            try:
                await deliver_due(store, records, principal)
            except Exception as error:  # noqa: BLE001 - one space's failure must not stop the others
                log.warning("deliveries for a space failed (%s)", type(error).__name__)
        await asyncio.sleep(interval)


__all__ = [
    "MARKER",
    "attempt",
    "deliver_due",
    "pause",
    "queue_item_delivery",
    "run_deliveries",
    "save_queue_item",
    "save_score",
    "score_delivery",
]
