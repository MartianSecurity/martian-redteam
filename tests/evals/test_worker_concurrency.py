"""More than one run at a time per organisation (MorningHQ/martian-notes#145).

An organisation may have its edition's ``concurrent_runs`` and ``concurrent_scoring_jobs`` in
progress at once, a queue's worker runs ``concurrency`` pieces of work at once, and each claim takes
the oldest work of the organisation with the fewest pieces running, so one organisation's many
queued pieces never hold back another's single one. On SQLite and on Postgres, there as an ordinary
role, so that row-level security applies.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from martian.evals.model import Case, Target
from martian.evals.records.queue import SqlWorkQueue
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER, Entitlements, Principal, create_app
from martian.evals.server.entitlements import PAID
from martian.evals.store import MemoryStore
from martian.evals.worker import (
    InProcessWorker,
    Judge,
    JudgeRequest,
    JudgeResult,
    QueueWorker,
    TargetCall,
    Work,
    dump_work,
)
from martian.evals.worker.queue import entry_id
from tests.evals import test_records
from tests.evals.record_contract import ORG_A, ORG_B
from tests.evals.write_fixture import AGENT, agent, judge, publish, request_id

needs_postgres = test_records.needs_postgres
schema, owner_url = test_records.schema, test_records.owner_url  # the record tests' fixtures
SECONDS = 60
OVERLAP_SECONDS = 5
"""How long a slow fake target waits for the other one to be called before it gives up."""

OTHER_AGENT = str(uuid.UUID(int=8))
TWO_AT_ONCE = Entitlements(
    edition="enterprise", features=PAID, limits={"concurrent_runs": 2, "concurrent_scoring_jobs": 2}
)
DATABASES = ["sqlite", pytest.param("postgres", marks=needs_postgres)]


@pytest.fixture(params=DATABASES)
def database(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[str, str]]:
    """A database URL and the schema the stores keep their tables in."""
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'martian.db'}", "martian"
    else:
        yield request.getfixturevalue("owner_url"), request.getfixturevalue("schema")


@pytest.fixture
def records(database: tuple[str, str]) -> Iterator[SqlRecordStore]:
    url, in_schema = database
    found = SqlRecordStore(url, schema=in_schema)
    yield found
    found.close()


@pytest.fixture
def queue(database: tuple[str, str], records: SqlRecordStore) -> Iterator[SqlWorkQueue]:
    url, in_schema = database
    found = SqlWorkQueue(url, schema=in_schema)
    yield found
    found.close()


class Overlapping:
    """Slow fake targets and judges, each of which waits, in every call, until the other one has
    been called too: two calls overlap, or the one that waits gives up after
    :data:`OVERLAP_SECONDS` with an error."""

    def __init__(self) -> None:
        self.started: set[str] = set()
        self.calls: list[tuple[str, float, float]] = []

    async def _call(self, name: str) -> None:
        start = time.monotonic()
        self.started.add(name)
        while len(self.started) < 2:
            if time.monotonic() - start > OVERLAP_SECONDS:
                raise TimeoutError(f"{name} was called, and the other never was while it waited")
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        self.calls.append((name, start, time.monotonic()))

    def target(self, name: str) -> Any:
        async def call(case: Case) -> str:
            await self._call(name)
            return await agent(case)

        return call

    async def judge(self, request: JudgeRequest) -> JudgeResult:
        await self._call(request.job_id)
        return await judge(request)

    def first_calls_overlap(self) -> bool:
        firsts = {}
        for name, start, end in self.calls:
            firsts.setdefault(name, (start, end))
        return len(firsts) == 2 and max(start for start, _ in firsts.values()) < min(end for _, end in firsts.values())


def served(
    records: SqlRecordStore, queue: SqlWorkQueue, targets: dict[str, TargetCall], judging: Judge = judge
) -> tuple[TestClient, QueueWorker]:
    """The core with a queue whose worker runs two pieces at once, for an organisation that may have
    two runs and two scoring jobs in progress at once."""
    store = MemoryStore()
    asyncio.run(publish(store))
    runner = InProcessWorker(store, records, targets=targets, judge=judging, target_timeout_seconds=SECONDS)
    worker = QueueWorker(queue, runner, records, in_process=True, lease_seconds=10, poll_seconds=0.05, concurrency=2)
    app = create_app(
        store=store, records=records, principal=lambda: SINGLE_USER, worker=worker, entitlements=lambda _p: TWO_AT_ONCE
    )
    return TestClient(app), worker


def launch(client: TestClient, agent_id: str, status: int = 202) -> dict[str, Any]:
    response = client.post("/v1/evals", json={"agent_id": agent_id, "dataset_name": "support-faq"})
    assert response.status_code == status, response.text
    found: dict[str, Any] = response.json()
    return found


def test_two_runs_of_one_organisation_progress_at_the_same_time(records: SqlRecordStore, queue: SqlWorkQueue) -> None:
    overlapping = Overlapping()
    targets = {AGENT: overlapping.target("first"), OTHER_AGENT: overlapping.target("second")}
    client, worker = served(records, queue, targets)
    with client:
        assert client.portal is not None
        first, second = launch(client, AGENT), launch(client, OTHER_AGENT)
        # Two are in progress, the organisation's limit: a third waits for one of them to end.
        refused = launch(client, AGENT, 409)
        assert refused == {
            "detail": "2 eval runs are already in progress, as many as this organisation may have at once"
        }
        client.portal.call(worker.drain)
        runs = [client.get(f"/v1/evals/{run['id']}").json() for run in (first, second)]
        # Neither target gave up waiting for the other: their calls overlapped.
        assert [(run["state"], run["passed"], run["failed"], run["errored"]) for run in runs] == [
            ("completed", 1, 1, 0)
        ] * 2
        assert overlapping.first_calls_overlap(), overlapping.calls
        third = launch(client, AGENT)  # with both ended, there is room again
        client.portal.call(worker.drain)
        assert client.get(f"/v1/evals/{third['id']}").json()["state"] == "completed"


def test_two_scoring_jobs_of_one_organisation_progress_at_the_same_time(
    records: SqlRecordStore, queue: SqlWorkQueue
) -> None:
    overlapping = Overlapping()
    client, worker = served(records, queue, {AGENT: agent}, overlapping.judge)
    runs = []
    with client:
        assert client.portal is not None
        for _ in range(3):
            runs.append(launch(client, AGENT))
            client.portal.call(worker.drain)
        body = {"scorers": ["correctness"], "acknowledge_model_cost": True}
        jobs = [
            client.post(f"/v1/evals/{run['id']}/scores", json={**body, "request_id": request_id()}) for run in runs[:2]
        ]
        assert [job.status_code for job in jobs] == [202, 202], [job.text for job in jobs]
        refused = client.post(f"/v1/evals/{runs[2]['id']}/scores", json={**body, "request_id": request_id()})
        assert (refused.status_code, refused.json()) == (
            409,
            {"detail": "2 scoring jobs are already in progress, as many as this organisation may have at once"},
        )
        client.portal.call(worker.drain)
        finished = [
            client.portal.call(records.list_scoring_jobs, SINGLE_USER, DEFAULT_PROJECT, run["id"])[0]
            for run in runs[:2]
        ]
    # faq-3 has no output to score; neither judge gave up waiting for the other.
    assert [(job.state, job.succeeded, job.errors) for job in finished] == [("partial", 2, 1)] * 2
    assert overlapping.first_calls_overlap(), overlapping.calls


class Recording:
    """A runner of any organisation's work that notes when each piece starts and takes a while."""

    def __init__(self) -> None:
        self.started: list[str] = []

    @property
    def judges(self) -> bool:
        return False

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        return {}

    async def submit(self, work: Work) -> None:
        raise AssertionError("the queue's worker runs the work")

    async def run(self, work: Work) -> None:
        self.started.append(work.run_id)
        await asyncio.sleep(0.3)


async def test_one_organisation_cannot_starve_another(records: SqlRecordStore, queue: SqlWorkQueue) -> None:
    # One organisation queues six runs, then another queues one.
    for number in range(6):
        work = Work(kind="run", principal=ORG_A, project_id="p1", run_id=f"a{number}")
        await queue.put(ORG_A, "p1", entry_id(work), "run", dump_work(work))
    single = Work(kind="run", principal=ORG_B, project_id="p1", run_id="b0")
    await queue.put(ORG_B, "p1", entry_id(single), "run", dump_work(single))
    runner = Recording()
    worker = QueueWorker(queue, runner, records, lease_seconds=10, poll_seconds=0.05, concurrency=2)
    serving = asyncio.get_running_loop().create_task(worker.serve())
    try:
        await asyncio.wait_for(worker.drain(), SECONDS)
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
    # The other organisation's run takes the worker's second place at once, beside the first
    # organisation's oldest, rather than waiting behind all six. (Two pieces taken one after the
    # other may start in either order: each reads its record before it starts.)
    assert set(runner.started[:2]) == {"a0", "b0"}
    assert sorted(runner.started[2:]) == ["a1", "a2", "a3", "a4", "a5"]


async def test_a_claim_takes_the_oldest_work_of_the_organisation_with_the_fewest_running(
    queue: SqlWorkQueue,
) -> None:
    for number in range(3):
        await queue.put(ORG_A, "p1", f"run:a{number}", "run", {"n": f"a{number}"})
    await queue.put(ORG_B, "p1", "run:b0", "run", {"n": "b0"})
    await queue.put(ORG_B, "p1", "run:b1", "run", {"n": "b1"})

    async def next_work(lease: float = 60) -> str:
        claim = await queue.claim(lease)
        assert claim is not None
        return str(claim.work["n"])

    # Organisation A's oldest, then B's while A has one running and B none, then by age while the
    # two have as many running.
    assert [await next_work(), await next_work(), await next_work()] == ["a0", "b0", "a1"]
    # A has two running, B one: B's next goes first, though A's is older.
    assert await next_work(0.3) == "b1"
    # Work whose lease ran out is not running but claimable: B has one running again, b0, and A two,
    # so b1 is taken again before A's older a2.
    await asyncio.sleep(0.5)
    assert await next_work() == "b1"
    assert await next_work() == "a2"
    assert await queue.claim(60) is None


def test_a_worker_runs_at_least_one_piece_at_a_time(records: SqlRecordStore, queue: SqlWorkQueue) -> None:
    assert QueueWorker(queue, Recording(), records).concurrency == 1
    with pytest.raises(ValueError, match="concurrency at least 1"):
        QueueWorker(queue, Recording(), records, concurrency=0)
