"""The durable queue and its worker (MorningHQ/martian-notes#139), and resuming work it takes again
(MorningHQ/martian-notes#141).

The queue holds run launches and scoring jobs in the record store's database, on SQLite and on
Postgres: each piece once, held by one worker at a time, and taken again once the lease of a
worker that stopped runs out. The queue-backed worker is a ``Worker`` the routes use unchanged,
scoring jobs publish their progress as runs do, and stopping the serving process mid-run and
starting it again loses and fails no queued or running run. Work taken again resumes after the
cases already finished: their target or judge is not called again.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, update

from martian.evals.model import Case, CaseResult, Run, ScoringJob, StoreRef, Target
from martian.evals.records import Event
from martian.evals.records.queue import RECORDING, SqlWorkQueue
from martian.evals.records.sql import SqlRecordStore
from martian.evals.records.tables import work_queue
from martian.evals.server import DEFAULT_PROJECT, ENTERPRISE, SINGLE_USER, create_app
from martian.evals.store import EvalStore, MemoryStore
from martian.evals.worker import (
    InProcessWorker,
    InterruptedRuns,
    Judge,
    JudgeRequest,
    JudgeResult,
    Lifecycle,
    QueueWorker,
    Runner,
    TargetCall,
    Work,
    Worker,
    dump_work,
)
from martian.evals.worker.execute import INTERRUPTED, NOT_ACCEPTED
from martian.evals.worker.queue import STOPPED_TOO_OFTEN, entry_id
from martian.evals.worker.scorers import builtin_scorer
from tests.evals import queue_fixture, test_records
from tests.evals.record_contract import ORG_A, ORG_B
from tests.evals.write_fixture import AGENT, CASES, Inert, agent, judge, publish, request_id, serve

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

    async def test_rescheduled_work_is_not_taken_before_it_is_due(self, queue: SqlWorkQueue) -> None:
        await queue.put(ORG_A, "p1", "monitor:m1", "monitor", {"n": 1})
        first = await queue.claim(60)
        assert first is not None and first.attempt == 1
        assert await queue.reschedule(first, datetime.now(UTC) + timedelta(seconds=0.6))
        assert not await queue.reschedule(first, datetime.now(UTC))  # the claim no longer holds it
        # Not due yet: no claim takes it, and the queue counts it as nothing to wait for.
        assert await queue.claim(60) is None and await queue.pending() == 0
        await queue.put(ORG_A, "p1", "monitor:m1", "monitor", {"n": 99})  # still in the queue: unchanged
        await asyncio.sleep(0.8)
        assert await queue.pending() == 1
        again = await queue.claim(60)
        assert again is not None and (again.work, again.attempt) == ({"n": 1}, 1)
        await queue.done(again)
        assert await queue.pending() == 0

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
    judge: Judge | None = None,
    lease_seconds: float = 1,
    max_attempts: int = 3,
) -> QueueWorker:
    runner: Runner = InProcessWorker(
        store, records, targets={AGENT: target}, judge=judge, target_timeout_seconds=SECONDS
    )
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
            provenance={"target": {"kind": "agent", "agent_id": AGENT}},  # the pin of the worker's function
            store=StoreRef(dataset_id=dataset.id),
        ),
    )
    return run, Work(kind="run", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=run.id)


async def stored(records: SqlRecordStore, run: Run) -> Run:
    found = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
    assert found is not None
    return found


def calls(folder: Path, case_id: str) -> list[str]:
    """The process of each call of the waiting agent with ``case_id``, by id: one entry per call."""
    prefix = f"called-{case_id}-"
    return sorted(path.name.removeprefix(prefix).split("-")[0] for path in folder.glob(f"{prefix}*"))


def test_the_routes_hand_work_to_the_queue_and_its_worker_runs_it(records: SqlRecordStore, queue: SqlWorkQueue) -> None:
    store = MemoryStore()
    asyncio.run(publish(store))
    worker = queue_worker(store, records, queue)
    assert isinstance(worker, Worker) and isinstance(worker, Lifecycle) and isinstance(worker, InterruptedRuns)
    assert not worker.judges
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


class Refusing(Inert):
    """A worker whose submit fails after the route recorded the work, as a queue that is away does."""

    async def submit(self, work: Work) -> None:
        raise RuntimeError("the queue is away")


def test_a_submit_that_fails_leaves_no_run_or_scoring_job_queued(records: SqlRecordStore) -> None:
    client, core = serve(MemoryStore(), records, entitlements=lambda _p: ENTERPRISE)
    with client:
        assert client.portal is not None
        completed = core.completed_run()
        client.app.state.martian_evals_worker = Refusing()  # type: ignore[attr-defined]
        for _ in range(2):  # the first refusal blocks no later launch with 409
            with pytest.raises(RuntimeError, match="the queue is away"):
                core.launch(request_id=request_id())
        page = client.portal.call(records.list_runs, SINGLE_USER, DEFAULT_PROJECT)
        refused = [run for run in page.items if run.id != completed["id"]]
        assert [(run.state, run.error) for run in refused] == [("failed", NOT_ACCEPTED)] * 2
        for _ in range(2):  # nor a later scoring request
            with pytest.raises(RuntimeError, match="the queue is away"):
                client.post(
                    f"/v1/evals/{completed['id']}/scores", json={"request_id": request_id(), "scorers": ["exact_match"]}
                )
        jobs = client.portal.call(records.list_scoring_jobs, SINGLE_USER, DEFAULT_PROJECT, completed["id"])
        assert [(job.state, job.error) for job in jobs] == [("failed", NOT_ACCEPTED)] * 2


async def test_work_whose_worker_died_is_taken_again_and_resumes_after_its_finished_cases(
    records: SqlRecordStore, queue: SqlWorkQueue
) -> None:
    store = MemoryStore()
    run, work = await queued_run(store, records)
    called: list[str] = []

    async def counting(case: Case) -> str:
        called.append(case.id)
        return await agent(case)

    worker = queue_worker(store, records, queue, counting)
    await worker.submit(work)
    # A worker takes the run, starts it, records one case and dies.
    assert await queue.claim(0.3) is not None
    started = datetime(2026, 11, 15, 9, tzinfo=UTC)
    await records.update_run(
        SINGLE_USER, run.model_copy(update={"state": "running", "started_at": started}), expected_state="queued"
    )
    old = CaseResult(run_id=run.id, case_id="faq-1", position=0, status="error", error="the first worker's")
    await records.put_results(SINGLE_USER, DEFAULT_PROJECT, [old])
    await worker.start()
    try:
        await worker.drain()
    finally:
        await worker.stop()
    # faq-1 keeps the result the first worker recorded and is not called again; faq-3 has no
    # expected text, so no worker calls it.
    assert called == ["faq-2"]
    finished = await stored(records, run)
    assert (finished.state, finished.error, finished.started_at) == ("completed", "", started)
    counts = finished.counts
    assert (counts.cases, counts.passed, counts.failed, counts.errors, counts.skipped) == (3, 0, 1, 1, 1)
    results = await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run.id)
    assert [(r.case_id, r.status, r.error) for r in results.items] == [
        ("faq-1", "error", "the first worker's"),
        ("faq-2", "failed", ""),
        ("faq-3", "skipped", "item is missing a usable prompt or expected output"),
    ]


def judge_of(called: list[str], hold: str | None = None, gate: asyncio.Event | None = None) -> Judge:
    """A judge that notes the prompt of each request in ``called``; with ``hold``, a request of
    that prompt waits for ``gate`` before it answers."""

    async def judging(request: JudgeRequest) -> JudgeResult:
        called.append(request.prompt)
        if request.prompt == hold and gate is not None:
            await gate.wait()
        return await judge(request)

    return judging


async def test_a_scoring_job_taken_again_resumes_after_the_cases_it_scored(
    records: SqlRecordStore, queue: SqlWorkQueue
) -> None:
    store = MemoryStore()
    run, work = await queued_run(store, records)
    first_calls: list[str] = []
    gate = asyncio.Event()
    held = "Can I change my username?"  # faq-2, the job's second case
    first = queue_worker(store, records, queue, judge=judge_of(first_calls, held, gate), lease_seconds=60)
    await first.start()
    await first.submit(work)
    await first.drain()
    job = ScoringJob(
        id="j1",
        run_id=run.id,
        scorers=[builtin_scorer("exact_match"), builtin_scorer("correctness")],
        state="queued",
    )
    await records.create_scoring_job(SINGLE_USER, DEFAULT_PROJECT, job)
    await first.submit(
        Work(kind="scoring", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=run.id, job_id="j1")
    )

    async def judging_the_second() -> bool:
        return held in first_calls

    await eventually(judging_the_second)
    # The worker stops while the judge grades faq-2, as a worker whose process dies does: the next
    # worker takes the job again.
    await first.stop()
    second_calls: list[str] = []
    second = queue_worker(store, records, queue, judge=judge_of(second_calls), lease_seconds=60)
    await second.start()
    try:
        await second.drain()
    finally:
        await second.stop()
    # faq-1 was scored and is not judged again; faq-2 was in flight and is; faq-3 has no output.
    assert first_calls == ["How do I reset my password?", held]
    assert second_calls == [held]
    (done,) = await records.list_scoring_jobs(SINGLE_USER, DEFAULT_PROJECT, run.id)
    assert (done.state, done.total, done.succeeded, done.errors) == ("partial", 6, 4, 2)
    scores = [s for s in await records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run.id) if s.scoring_job_id == "j1"]
    assert sorted((s.case_id, s.scorer) for s in scores) == [
        (case_id, scorer) for case_id in ("faq-1", "faq-2", "faq-3") for scorer in ("correctness", "exact_match")
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


async def test_a_run_nothing_holds_is_cancelled_at_once_and_a_recording_whose_process_stopped_is_interrupted(
    records: SqlRecordStore, queue: SqlWorkQueue
) -> None:
    worker = queue_worker(MemoryStore(), records, queue)

    async def recorded() -> tuple[Run, Work]:
        """A run another process records itself, as ``martian evals run`` does."""
        run = Run(id=str(uuid.uuid4()), project_id=DEFAULT_PROJECT, source="local", state="running")
        await records.create_run(SINGLE_USER, run)
        return run, Work(kind="run", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=run.id)

    held, work = await recorded()
    claim = await queue.hold(SINGLE_USER, DEFAULT_PROJECT, entry_id(work), RECORDING, dump_work(work), 60)
    # While a live claim holds it, cancelling it waits for its next case.
    assert await worker.cancel_interrupted(SINGLE_USER, held) is None
    await queue.done(claim)
    # Held by nothing, it is cancelled at once.
    stopped = await worker.cancel_interrupted(SINGLE_USER, held)
    assert stopped is not None and (stopped.state, stopped.error) == ("cancelled", INTERRUPTED)

    # The worker never runs a recording; once its process stops renewing it, it is interrupted.
    gone, work = await recorded()
    await queue.hold(SINGLE_USER, DEFAULT_PROJECT, entry_id(work), RECORDING, dump_work(work), 0.2)
    await worker.start()
    try:
        await worker.drain()
    finally:
        await worker.stop()
    finished = await stored(records, gone)
    assert (finished.state, finished.error, finished.finished_at is not None) == ("failed", INTERRUPTED, True)


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
        pytest.param("processes", "postgres", marks=needs_postgres),
    ],
)
def test_stopping_the_serving_process_mid_run_and_starting_it_again_finishes_the_run_without_repeating_cases(
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
        launched = first.call("POST", "/v1/evals", {"agent_id": AGENT, "dataset_name": "support-faq"})
        run_id = launched["id"]
        assert first.run(run_id)["state"] == "queued"
    finally:
        first.kill()

    # A process that serves the queue takes it, finishes faq-1, and stops while faq-2 is in flight.
    (tmp_path / "release-faq-1").touch()
    second = Server(tmp_path, runner, url, schema, "serve")
    try:
        wait_until(lambda: bool(calls(tmp_path, "faq-2")))
        midway = second.run(run_id)
        assert (midway["state"], midway["passed"], midway["failed"]) == ("running", 1, 0)
    finally:
        second.kill()
    (caller,) = calls(tmp_path, "faq-2")
    wait_until(lambda: gone(int(caller)))  # a run's own process ends with the worker that started it
    assert calls(tmp_path, "faq-1") == [caller]

    # A newer version of the dataset, under which faq-2 would pass, is published meanwhile.
    newer = [
        case.model_copy(update={"expectations": {"expected_response": "I am not sure."}})
        if case.id == "faq-2"
        else case
        for case in CASES
    ]
    asyncio.run(queue_fixture.store_in(tmp_path).create_dataset(SINGLE_USER, DEFAULT_PROJECT, "support-faq", newer))

    # Started again, it takes the run once its lease runs out and finishes it, on the dataset
    # version the run was admitted with.
    (tmp_path / "release").touch()
    third = Server(tmp_path, runner, url, schema, "serve")
    try:
        wait_until(lambda: third.run(run_id)["state"] not in ("queued", "running"))
        run = third.run(run_id)
    finally:
        third.stop()
    assert (run["state"], run["error"], run["dataset_id"]) == ("completed", "", launched["dataset_id"])
    assert (run["passed"], run["failed"], run["skipped"], run["errored"]) == (1, 1, 1, 0)
    # The target calls of each case: faq-1 had finished and is not called again, faq-2 was in flight
    # and is, once, and faq-3 has no expected text, so it is never called.
    assert calls(tmp_path, "faq-1") == [caller]
    again = calls(tmp_path, "faq-2")
    assert len(again) == 2 and caller in again
    assert calls(tmp_path, "faq-3") == []
