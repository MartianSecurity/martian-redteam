"""A person's answer never waits on the store: the outbox keeps its write until the store takes it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from martian.evals.model import Score
from martian.evals.outbox import MARKER, deliver_due, pause, queue_item_delivery, save_queue_item, save_score
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, ENTERPRISE, organisation_principal
from martian.evals.store import MemoryStore, StoreError
from tests.evals.write_fixture import Served, request_id, serve

ADMIN = organisation_principal("admin-1", "org-a", frozenset({"admin"}))
LATER = datetime.now(UTC) + timedelta(days=1)


class Flaky(MemoryStore):
    """A store whose score and queue-item writes fail while ``down``; ``lost`` makes a write that
    reaches the store fail afterwards, as a lost response would."""

    def __init__(self) -> None:
        super().__init__()
        self.down, self.lost, self.writes = False, False, 0

    async def log_score(self, *args: Any, **kwargs: Any) -> Score:
        if self.down:
            raise ConnectionError("store unreachable")
        self.writes += 1
        logged = await super().log_score(*args, **kwargs)
        if self.lost:
            raise TimeoutError("response lost")
        return logged

    async def set_queue_item(self, *args: Any, **kwargs: Any) -> Any:
        if self.down:
            raise ConnectionError("store unreachable")
        return await super().set_queue_item(*args, **kwargs)


@pytest.fixture
def flaky(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    store = Flaky()
    client, served = serve(store, records, entitlements=lambda _p: ENTERPRISE, principal=ADMIN)
    with client:
        yield served, store
    records.close()


def answer(served: Served, run: dict[str, Any]) -> dict[str, Any]:
    item = next(i for i in run["items"] if i["evals_trace_id"])
    body = {"request_id": request_id(), "state": "complete", "value": True, "comment": "Matches the policy."}
    return served.call("POST", f"/v1/evals/{run['id']}/items/{item['id']}/reviews", 201, json=body) | {"item": item}


def human_scores(served: Served, run: dict[str, Any]) -> list[Score]:
    return [
        s for s in asyncio.run(served.records.list_scores(ADMIN, DEFAULT_PROJECT, run["id"])) if s.source == "human"
    ]


def test_an_answer_is_saved_while_the_store_is_down_and_delivered_when_it_is_back(flaky):
    served, store = flaky
    run = served.completed_run()
    store.down = True
    review = answer(served, run)  # 201: the request does not wait on the store
    (kept,) = human_scores(served, run)
    assert (kept.value, kept.store.assessment_id) == (True, None)
    trace = review["item"]["evals_trace_id"]
    assert not [s for s in asyncio.run(store.list_scores(ADMIN, DEFAULT_PROJECT, trace)) if s.source == "human"]
    (waiting,) = asyncio.run(served.records.due_deliveries(ADMIN, now=LATER))
    assert (waiting.state, waiting.attempts, waiting.error) == ("pending", 1, "ConnectionError")
    assert asyncio.run(deliver_due(store, served.records, ADMIN, now=datetime.now(UTC))) == 0  # not due yet
    store.down = False
    assert asyncio.run(deliver_due(store, served.records, ADMIN, now=LATER)) == 1
    (delivered,) = [s for s in asyncio.run(store.list_scores(ADMIN, DEFAULT_PROJECT, trace)) if s.source == "human"]
    assert delivered.metadata[MARKER] == waiting.id
    (kept,) = human_scores(served, run)
    assert kept.store.assessment_id == delivered.store.assessment_id is not None
    assert asyncio.run(served.records.get_delivery(ADMIN, DEFAULT_PROJECT, waiting.id)).state == "delivered"
    assert asyncio.run(served.records.due_deliveries(ADMIN, now=LATER)) == []


def test_a_write_that_reached_the_store_is_not_made_twice(flaky):
    served, store = flaky
    run = served.completed_run()
    store.lost, before = True, store.writes
    review = answer(served, run)
    assert store.writes == before + 1
    store.lost = False
    assert asyncio.run(deliver_due(store, served.records, ADMIN, now=LATER)) == 1
    assert store.writes == before + 1  # read back, not written again
    trace = review["item"]["evals_trace_id"]
    assert len([s for s in asyncio.run(store.list_scores(ADMIN, DEFAULT_PROJECT, trace)) if s.source == "human"]) == 1


def test_the_newest_queue_status_is_the_one_delivered(flaky):
    served, store = flaky
    run = served.completed_run()
    item = next(i for i in run["items"] if i["evals_trace_id"])
    queue = served.call(
        "POST",
        "/v1/evals/review-queues",
        201,
        json={
            "request_id": request_id(),
            "run_id": run["id"],
            "name": "Check",
            "item_ids": [item["id"]],
            "questions": [{"name": "correct", "kind": "boolean", "instruction": "Correct?"}],
        },
    )
    trace = item["evals_trace_id"]
    store.down = True
    for status in ("completed", "pending", "completed"):
        asyncio.run(save_queue_item(store, served.records, ADMIN, DEFAULT_PROJECT, queue["id"], trace, status))
    assert len(asyncio.run(served.records.due_deliveries(ADMIN, now=LATER))) == 1
    store.down = False
    asyncio.run(deliver_due(store, served.records, ADMIN, now=LATER))
    (found,) = [i for i in asyncio.run(store.queue_items(ADMIN, DEFAULT_PROJECT, queue["id"])) if i.trace_id == trace]
    assert found.status == "completed"
    assert (
        queue_item_delivery(DEFAULT_PROJECT, queue["id"], trace, "pending").id
        == queue_item_delivery(DEFAULT_PROJECT, queue["id"], trace, "completed").id
    )


def test_a_write_the_store_refuses_for_good_ends_failed(flaky):
    served, store = flaky

    async def refuse(*_args: Any, **_kwargs: Any) -> Score:
        raise StoreError("invalid", "the trace takes no scores")

    run = served.completed_run()
    store.log_score = refuse  # type: ignore[method-assign]
    item = next(i for i in run["items"] if i["evals_trace_id"])
    score = Score(
        run_id=run["id"],
        case_id=item["dataset_item_id"],
        trace_id=item["evals_trace_id"],
        scorer="human_review",
        source="human",
        value=True,
        created_at=datetime.now(UTC),
    )
    _kept, delivery = asyncio.run(save_score(store, served.records, ADMIN, DEFAULT_PROJECT, score))
    assert delivery is not None and (delivery.state, delivery.error) == ("failed", "the trace takes no scores")
    assert asyncio.run(served.records.due_deliveries(ADMIN, now=LATER)) == []


def test_retries_wait_longer_each_time_up_to_fifteen_minutes():
    assert [pause(n).total_seconds() for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert pause(20) == timedelta(minutes=15)
