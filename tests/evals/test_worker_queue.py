"""The durable queue and its worker (MorningHQ/martian-notes#139).

The queue holds run launches and scoring jobs in the record store's database, on SQLite and on
Postgres: each piece once, held by one worker at a time, and taken again once the lease of a
worker that stopped runs out. The queue-backed worker is a ``Worker`` the routes use unchanged,
scoring jobs publish their progress as runs do, and stopping the serving process mid-run and
starting it again loses and fails no queued or running run.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, update

from martian.evals.model import Case, CaseResult, Run, ScoringJob, StoreRef, Target
from martian.evals.records import Event
from martian.evals.records.queue import SqlWorkQueue
from martian.evals.records.sql import SqlRecordStore
from martian.evals.records.tables import work_queue
from martian.evals.server import DEFAULT_PROJECT, ENTERPRISE, SINGLE_USER, create_app
from martian.evals.store import EvalStore, MemoryStore
from martian.evals.worker import InProcessWorker, Lifecycle, QueueWorker, Runner, TargetCall, Work, Worker
from martian.evals.worker.queue import STOPPED_TOO_OFTEN, entry_id
from martian.evals.worker.scorers import builtin_scorer
from tests.evals import queue_fixture, test_records
from tests.evals.record_contract import ORG_A, ORG_B
from tests.evals.write_fixture import AGENT, agent, publish, request_id

POSTGRES_URL, needs_postgres = test_records.POSTGRES_URL, test_records.needs_postgres
schema, owner_url = test_records.schema, test_records.owner_url  # the record tests' fixtures
SECONDS = 90


async def eventually(check: Callable[[], Any], seconds: float = SECONDS) -> None:
    deadline = time.monotonic() + seconds
    while not await check():
        if time.monotonic() > deadline:
            raise TimeoutError("the condition never held")
        await asyncio.sleep(0.05)


# --- the queue -------------------------------------------------------------------------------


class QueueContract:
    """What the queue does on every database. ``queue`` and ``other`` are two workers' queues on one
    database, as two worker processes have."""

    async def test_work_is_queued_once_and_taken_oldest_first(self, queue: SqlWorkQueue) -> None:
        await queue.put(ORG_A, "p1", "run:r1", "run", {"n": 1})
        await queue.put(ORG_A, "p1", "run:r1", "run", {"n": 99})
        await queue.put(ORG_B, "p1", "run:r1", "run", {"n": 2})  # the same id is another organisation's
        assert await queue.pending() == 2
        first, second = await queue.claim(60), await queue.claim(60)
        assert first is not None and second is not None
        assert (first.space, first.kind, first.work, first.attempt) == ("org-a", "run", {"n": 1}, 1)
        assert (second.space, second.work) == ("org-b", {"n": 2})
        assert await queue.claim(60) is None
        await queue.done(first)
        await queue.done(second)
        assert await queue.pending() == 0

    async def test_a_claim_holds_its_work_until_its_lease_runs_out(self, queue: SqlWorkQueue) -> None:
        await queue.put(ORG_A, "p1", "run:r1", "run", {})
        held = await queue.claim(0.6)
        assert held is not None and await queue.claim(60) is None
        assert await queue.held(ORG_A, "p1", "run:r1") and not await queue.held(ORG_B, "p1", "run:r1")
        assert await queue.renew(held, 0.6)
        await asyncio.sleep(1)
        assert not await queue.held(ORG_A, "p1", "run:r1")
        taken = await queue.claim(60)
        assert taken is not None and (taken.id, taken.attempt) == ("run:r1", 2) and taken.token != held.token
        # The worker whose lease ran out changes nothing any more.
        assert not await queue.renew(held, 60)
        await queue.release(held)
        await queue.done(held)
        assert await queue.pending() == 1 and await queue.claim(60) is None
        await queue.done(taken)
        assert await queue.pending() == 0

    async def test_released_work_is_taken_again_at_once_without_counting_the_attempt(self, queue: SqlWorkQueue) -> None:
        await queue.put(ORG_A, "p1", "run:r1", "run", {})
        first = await queue.claim(60)
        assert first is not None
        await queue.release(first)
        again = await queue.claim(60)
        assert again is not None and again.attempt == 1 and again.token != first.token

    async def test_two_workers_never_hold_the_same_work(self, queue: SqlWorkQueue, other: SqlWorkQueue) -> None:
        for number in range(12):
            await queue.put(ORG_A if number % 2 else ORG_B, "p1", f"run:r{number}", "run", {"n": number})
        taken: list[int] = []
        for _ in range(20):
            claims = await asyncio.gather(*(q.claim(60) for q in (queue, other) for _ in range(8)))
            taken.extend(claim.work["n"] for claim in claims if claim is not None)
            if len(taken) >= 12:
                break
        assert sorted(taken) == list(range(12))


class TestQueueOnSqlite(QueueContract):
    @pytest.fixture
    def queue(self, tmp_path: Path):
        found = SqlWorkQueue(f"sqlite:///{tmp_path / 'martian.db'}")
        yield found
        found.close()

    @pytest.fixture
    def other(self, tmp_path: Path, queue: SqlWorkQueue):
        found = SqlWorkQueue(f"sqlite:///{tmp_path / 'martian.db'}")
        yield found
        found.close()


@needs_postgres
class TestQueueOnPostgres(QueueContract):
    """As an ordinary role that owns its schema, so row-level security applies to the queue."""

    @pytest.fixture
    def queue(self, schema: str, owner_url: str):
        found = SqlWorkQueue(owner_url, schema=schema)
        yield found
        found.close()

    @pytest.fixture
    def other(self, schema: str, owner_url: str, queue: SqlWorkQueue):
        found = SqlWorkQueue(owner_url, schema=schema)
        yield found
        found.close()

    async def test_row_level_security_hides_queued_work_from_sql_that_names_no_space(
        self,
        queue: SqlWorkQueue,
        schema: str,
        owner_url: str,
    ) -> None:
        await queue.put(ORG_A, "p1", "run:r1", "run", {})
        engine = create_engine(owner_url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            with engine.begin() as connection:
                assert connection.execute(text("SELECT count(*) FROM martian_work_queue")).scalar_one() == 0
                connection.execute(text("SELECT set_config('martian.space', 'org-b', true)"))
                assert connection.execute(text("SELECT count(*) FROM martian_work_queue")).scalar_one() == 0
        finally:
            engine.dispose()
        assert await queue.pending() == 1


# --- the worker ------------------------------------------------------------------------------


@pytest.fixture
def database(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'martian.db'}"


@pytest.fixture
def records(database: str):
    found = SqlRecordStore(database)
    yield found
    found.close()


@pytest.fixture
def queue(database: str):
    found = SqlWorkQueue(database)
    yield found
    found.close()


def queue_worker(
    store: EvalStore,
    records: SqlRecordStore,
    queue: SqlWorkQueue,
    target: TargetCall = agent,
    *,
    lease_seconds: float = 1,
    max_attempts: int = 3,
) -> QueueWorker:
    runner: Runner = InProcessWorker(store, records, targets={AGENT: target}, target_timeout_seconds=SECONDS)
    return QueueWorker(
        queue,
        runner,
        records,
        in_process=True,
        lease_seconds=lease_seconds,
        poll_seconds=0.05,
        max_attempts=max_attempts,
    )


async def queued_run(store: EvalStore, records: SqlRecordStore) -> tuple[Run, Work]:
    """A queued run over the published dataset, as a launch records it, and its work."""
    await publish(store)
    dataset = await store.find_dataset(SINGLE_USER, DEFAULT_PROJECT, "support-faq")
    assert dataset is not None
    run = await records.create_run(
        SINGLE_USER,
        Run(
            id=str(uuid.uuid4()),
            project_id=DEFAULT_PROJECT,
            source="console",
            state="queued",
            dataset_name=dataset.name,
            dataset_digest=dataset.digest,
            target=Target(kind="agent", agent_id=AGENT),
            max_cases=100,
            store=StoreRef(dataset_id=dataset.id),
        ),
    )
    return run, Work(kind="run", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=run.id)


async def stored(records: SqlRecordStore, run: Run) -> Run:
    found = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
    assert found is not None
    return found


def calls(folder: Path, case_id: str) -> list[str]:
    """The processes that called the waiting agent with ``case_id``, by id."""
    return sorted(path.name.rsplit("-", 1)[1] for path in folder.glob(f"called-{case_id}-*"))


def test_the_routes_hand_work_to_the_queue_and_its_worker_runs_it(records: SqlRecordStore, queue: SqlWorkQueue) -> None:
    store = MemoryStore()
    asyncio.run(publish(store))
    worker = queue_worker(store, records, queue)
    assert isinstance(worker, Worker) and isinstance(worker, Lifecycle) and not worker.judges
    app = create_app(
        store=store, records=records, principal=lambda: SINGLE_USER, worker=worker, entitlements=lambda _p: ENTERPRISE
    )
    with TestClient(app) as client:
        assert client.portal is not None
        launched = client.post("/v1/evals", json={"agent_id": AGENT, "dataset_name": "support-faq"})
        assert launched.status_code == 202 and launched.json()["state"] == "queued", launched.text
        run_id = launched.json()["id"]
        client.portal.call(worker.drain)
        run = client.get(f"/v1/evals/{run_id}").json()
        assert (run["state"], run["passed"], run["failed"], run["skipped"]) == ("completed", 1, 1, 1)
        scored = client.post(
            f"/v1/evals/{run_id}/scores", json={"request_id": request_id(), "scorers": ["exact_match"]}
        )
        assert scored.status_code == 202, scored.text
        client.portal.call(worker.drain)
        (job,) = client.portal.call(records.list_scoring_jobs, SINGLE_USER, DEFAULT_PROJECT, run_id)
        # faq-3 has no output to score, so the job is partial.
        assert (job.id, job.state, job.succeeded, job.errors) == (scored.json()["id"], "partial", 2, 1)
    assert asyncio.run(queue.pending()) == 0


async def test_work_whose_worker_died_is_taken_again_and_run_from_the_start(
    records: SqlRecordStore, queue: SqlWorkQueue
) -> None:
    store = MemoryStore()
    run, work = await queued_run(store, records)
    worker = queue_worker(store, records, queue)
    await worker.submit(work)
    # A worker takes the run, starts it, records one case and dies.
    assert await queue.claim(0.3) is not None
    await records.update_run(SINGLE_USER, run.model_copy(update={"state": "running"}), expected_state="queued")
    old = CaseResult(run_id=run.id, case_id="faq-1", position=0, status="error", error="the first worker's")
    await records.put_results(SINGLE_USER, DEFAULT_PROJECT, [old])
    await worker.start()
    try:
        await worker.drain()
    finally:
        await worker.stop()
    finished = await stored(records, run)
    assert (finished.state, finished.error) == ("completed", "")
    assert (finished.counts.passed, finished.counts.failed, finished.counts.skipped) == (1, 1, 1)
    results = await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run.id)
    assert [(r.case_id, r.status) for r in results.items] == [
        ("faq-1", "passed"),
        ("faq-2", "failed"),
        ("faq-3", "skipped"),
    ]


async def test_work_is_failed_once_its_workers_stopped_too_often(records: SqlRecordStore, queue: SqlWorkQueue) -> None:
    store = MemoryStore()
    run, work = await queued_run(store, records)
    called: list[str] = []

    async def recording(case: Case) -> str:
        called.append(case.id)
        return "answer"

    worker = queue_worker(store, records, queue, recording, max_attempts=2)
    await worker.submit(work)
    for _ in range(2):  # two workers took it and died
        assert await queue.claim(0.05) is not None
        await asyncio.sleep(0.1)
    await worker.start()
    try:
        await worker.drain()
    finally:
        await worker.stop()
    finished = await stored(records, run)
    assert (finished.state, finished.error, called) == ("failed", STOPPED_TOO_OFTEN, [])


async def test_a_stopped_worker_gives_its_work_back_and_the_next_one_finishes_it(
    records: SqlRecordStore, queue: SqlWorkQueue, tmp_path: Path
) -> None:
    store = MemoryStore()
    run, work = await queued_run(store, records)
    waiting = queue_fixture.waiting_agent(tmp_path)
    first = queue_worker(store, records, queue, waiting, lease_seconds=60)
    await first.submit(work)
    await first.start()

    async def called() -> bool:
        return bool(calls(tmp_path, "faq-1"))

    await eventually(called)
    await first.stop()
    # Not failed and not lost: still running, and in the queue for the next worker at once.
    assert (await stored(records, run)).state == "running"
    assert await queue.pending() == 1 and not await queue.held(SINGLE_USER, DEFAULT_PROJECT, entry_id(work))
    (tmp_path / "release").touch()
    second = queue_worker(store, records, queue, waiting, lease_seconds=60)
    await second.start()
    try:
        await second.drain()
    finally:
        await second.stop()
    finished = await stored(records, run)
    assert (finished.state, finished.counts.passed, finished.counts.failed) == ("completed", 1, 1)


async def test_a_worker_whose_claim_another_took_stops_its_work(
    records: SqlRecordStore, queue: SqlWorkQueue, tmp_path: Path, database: str
) -> None:
    store = MemoryStore()
    run, work = await queued_run(store, records)
    worker = queue_worker(store, records, queue, queue_fixture.waiting_agent(tmp_path), lease_seconds=0.6)
    await worker.submit(work)
    await worker.start()

    async def called() -> bool:
        return bool(calls(tmp_path, "faq-1"))

    try:
        await eventually(called)
        # Another worker holds the work now, for a long lease.
        engine = create_engine(database)
        with engine.begin() as connection:
            connection.execute(
                update(work_queue)
                .where(work_queue.c.id == entry_id(work))
                .values(token="another-worker", lease_expires_at=datetime(2999, 1, 1, tzinfo=UTC))
            )
        engine.dispose()
        await asyncio.sleep(0.6)  # the worker's next renewal finds the claim gone, and stops the run
        (tmp_path / "release").touch()
        await asyncio.sleep(0.6)
        assert calls(tmp_path, "faq-2") == [] and (await stored(records, run)).counts.passed == 0
    finally:
        await worker.stop()


async def test_scoring_jobs_publish_their_progress_as_runs_do(records: SqlRecordStore, queue: SqlWorkQueue) -> None:
    store = MemoryStore()
    run, work = await queued_run(store, records)
    worker = queue_worker(store, records, queue)
    listener = records.events.subscribe(SINGLE_USER)
    seen: list[tuple[str, str, int]] = []

    async def follow() -> None:
        """A live page's listener: it reads each changed run or job again, as events ask."""
        async for event in listener:
            if event.kind == "run":
                found = await records.get_run(SINGLE_USER, event.project_id, event.id)
                assert found is not None
                seen.append(("run", found.state, found.counts.passed + found.counts.failed + found.counts.skipped))
            elif event.kind == "scoring_job":
                assert event.run_id == run.id
                jobs = await records.list_scoring_jobs(SINGLE_USER, event.project_id, event.run_id)
                (job,) = (job for job in jobs if job.id == event.id)
                seen.append(("scoring_job", job.state, job.succeeded + job.errors))

    following = asyncio.get_running_loop().create_task(follow())
    await worker.start()
    try:
        await worker.submit(work)
        await worker.drain()
        job = ScoringJob(id="j1", run_id=run.id, scorers=[builtin_scorer("exact_match")], state="queued")
        await records.create_scoring_job(SINGLE_USER, DEFAULT_PROJECT, job)
        await worker.submit(
            Work(kind="scoring", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=run.id, job_id="j1")
        )
        await worker.drain()
        await asyncio.sleep(0.3)  # let the listener read the last events
    finally:
        await worker.stop()
        following.cancel()
        listener.close()
    for kind, outcome, total in (("run", "completed", 3), ("scoring_job", "partial", 3)):
        states = [state for found, state, _ in seen if found == kind]
        progress = [count for found, _, count in seen if found == kind]
        assert states[-1] == outcome and "running" in states, (kind, seen)
        assert progress == sorted(progress) and progress[-1] == total, (kind, seen)
    assert Event(kind="scoring_job", project_id="p", id="j", run_id="r").run_id == "r"


# --- restarting the serving process ----------------------------------------------------------


class Server:
    """``tests.evals.queue_fixture`` serving in a process of its own."""

    def __init__(self, folder: Path, runner: str, database_url: str, schema: str, mode: str) -> None:
        port_file = folder / f"port-{uuid.uuid4().hex}"
        command = [sys.executable, "-m", "tests.evals.queue_fixture", str(folder), runner, database_url, schema, mode]
        self.process = subprocess.Popen([*command, str(port_file)])
        deadline = time.monotonic() + SECONDS
        while not port_file.exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the server did not start")
            time.sleep(0.05)
        self.base = f"http://127.0.0.1:{port_file.read_text(encoding='utf-8')}"

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=SECONDS) as response:
            found: dict[str, Any] = json.loads(response.read())
            return found

    def run(self, run_id: str) -> dict[str, Any]:
        return self.call("GET", f"/v1/evals/{run_id}")

    def kill(self) -> None:
        """Stop the server at once, as a crash or a lost machine does: nothing it held is released."""
        self.process.kill()
        self.process.wait(timeout=SECONDS)

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=SECONDS)
        except subprocess.TimeoutExpired:
            self.kill()


def wait_until(check: Callable[[], bool], seconds: float = SECONDS) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("the condition never held")
        time.sleep(0.1)


def gone(pid: int) -> bool:
    """Whether the process ``pid`` has ended: no such process, or one only waiting to be reaped."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False


@pytest.mark.parametrize(
    ("runner", "database"),
    [
        ("in-process", "sqlite"),
        ("processes", "sqlite"),
        pytest.param("in-process", "postgres", marks=needs_postgres),
    ],
)
def test_stopping_the_serving_process_mid_run_and_starting_it_again_loses_and_fails_no_run(
    tmp_path: Path,
    runner: str,
    database: str,
    schema: str,
) -> None:
    url = f"sqlite:///{tmp_path / 'martian.db'}" if database == "sqlite" else str(POSTGRES_URL)
    asyncio.run(publish(queue_fixture.store_in(tmp_path)))

    # An API process that only queues work: the run is queued when it stops.
    first = Server(tmp_path, runner, url, schema, "queue")
    try:
        run_id = first.call("POST", "/v1/evals", {"agent_id": AGENT, "dataset_name": "support-faq"})["id"]
        assert first.run(run_id)["state"] == "queued"
    finally:
        first.kill()

    # A process that serves the queue takes it, and stops while its first case is in flight.
    second = Server(tmp_path, runner, url, schema, "serve")
    try:
        wait_until(lambda: bool(calls(tmp_path, "faq-1")))
        assert second.run(run_id)["state"] == "running"
    finally:
        second.kill()
    (caller,) = calls(tmp_path, "faq-1")
    wait_until(lambda: gone(int(caller)))  # a run's own process ends with the worker that started it
    assert calls(tmp_path, "faq-2") == []

    # Started again, it takes the run once its lease runs out and finishes it.
    (tmp_path / "release").touch()
    third = Server(tmp_path, runner, url, schema, "serve")
    try:
        wait_until(lambda: third.run(run_id)["state"] not in ("queued", "running"))
        run = third.run(run_id)
    finally:
        third.stop()
    assert (run["state"], run["error"]) == ("completed", "")
    assert (run["passed"], run["failed"], run["skipped"], run["errored"]) == (1, 1, 1, 0)
    taken_again = calls(tmp_path, "faq-1")
    assert len(taken_again) == 2 and caller in taken_again
