"""Scheduled regression runs: a saved suite run against a target unattended
(MartianSecurity/martian-notes#197).

A schedule is created through its routes, which need the ``monitoring`` entitlement, and the
durable queue's worker takes a turn at each of its slots, every so many seconds or on a cron
expression in UTC, admitting the slot's run as a launch is admitted. A run that cannot be admitted
is recorded as skipped with the reason; slots missed while no worker ran are run once; each
schedule keeps its history, with each run's state, pass rate, suite scorecard and the paired
comparison with its previous completed run; and each scored run's gate result feeds the thresholds
on the schedule's gate results (MartianSecurity/martian-notes#198), which breach and recover. A fake
target and a fake judge stand in for the agent and the model; nothing calls a model or a network
target.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import martian.evals.worker.schedule as schedule_module
from martian.evals import slots
from martian.evals.judge_settings import MemoryJudgeSettings
from martian.evals.model import Case, Policy, Project, RunLimits, Suite, Target, suite_digest
from martian.evals.records import Schedule, ScheduledRun, ScheduleGates, Threshold, WebhookChannel
from martian.evals.records.queue import SqlWorkQueue
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import COMMUNITY, DEFAULT_PROJECT, SINGLE_USER, Entitlements, Principal, create_app
from martian.evals.server.entitlements import PAID
from martian.evals.server.principal import organisation_principal
from martian.evals.store import EvalStore, MemoryStore
from martian.evals.worker import InProcessWorker, QueueWorker, ScheduleServices, Work
from martian.evals.worker.queue import entry_id
from martian.evals.worker.schedule import (
    NOT_COMPLETED,
    SCHEDULE_KEY,
    after_scheduled_run,
    execute_schedule,
    scheduled_run_id,
    suite_job_id,
)
from martian.evals.worker.scorers import builtin_scorer
from tests.evals import test_records
from tests.evals.write_fixture import judge

needs_postgres = test_records.needs_postgres
schema, owner_url = test_records.schema, test_records.owner_url  # the record tests' fixtures
DATABASES = ["sqlite", pytest.param("postgres", marks=needs_postgres)]

AGENT = str(uuid.UUID(int=11))
POLL, SECONDS = 0.05, 60
CASES = [
    Case(id=f"q-{n}", inputs={"message": f"What is answer {n}?"}, expectations={"expected_response": f"answer {n}"})
    for n in range(10)
]
ONE_RUN = Entitlements(edition="enterprise", features=PAID, limits={"concurrent_runs": 1, "concurrent_scoring_jobs": 1})
"""An edition with monitoring that admits one run at a time, so a slot due while a run is in
progress is skipped."""
MEMBER = organisation_principal("member-1", "org-1", frozenset({"member"}))
VIEWER = organisation_principal("viewer-1", "org-1", frozenset({"viewer"}))
T0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
"""A Sunday."""


class Answers:
    """The target under test: it answers every case right until it has answered ``right`` cases,
    then wrongly, as a regression in the system under test would; ``delay`` seconds per call, and
    not before ``gate`` is open, when it has one."""

    def __init__(self, right: int = 10**6, delay: float = 0.0, gate: threading.Event | None = None) -> None:
        self.right, self.delay, self.gate, self.calls = right, delay, gate, 0

    async def __call__(self, case: Case) -> str:
        self.calls += 1
        while self.gate is not None and not self.gate.is_set():
            await asyncio.sleep(0.01)
        if self.delay:
            await asyncio.sleep(self.delay)
        number = case.inputs["message"].removeprefix("What is answer ").removesuffix("?")
        return f"answer {number}" if self.calls <= self.right else "I do not know."


# --- slots ------------------------------------------------------------------------------------


def test_an_interval_is_due_on_multiples_of_it_since_the_epoch_in_utc() -> None:
    hourly = slots.timing(every_seconds=3600, cron=None)
    assert hourly.after(T0 + timedelta(minutes=20)) == T0 + timedelta(hours=1)
    assert hourly.after(T0) == T0 + timedelta(hours=1)  # strictly after
    later = datetime(2026, 11, 15, 10, 20, tzinfo=UTC).astimezone()  # another zone, the same moment
    assert hourly.after(later) == datetime(2026, 11, 15, 11, tzinfo=UTC)
    assert hourly.count(T0, T0 + timedelta(hours=3), up_to=1000) == 3
    assert hourly.count(T0 + timedelta(minutes=1), T0 + timedelta(minutes=59), up_to=1000) == 0
    assert hourly.count(T0, T0 + timedelta(days=100), up_to=50) == 50


def test_a_cron_expression_is_read_in_utc_with_cron_s_day_rules() -> None:
    nightly = slots.timing(every_seconds=None, cron="0 2 * * *")
    assert nightly.after(T0) == datetime(2026, 11, 16, 2, tzinfo=UTC)
    assert nightly.after(datetime(2026, 11, 16, 2, tzinfo=UTC)) == datetime(2026, 11, 17, 2, tzinfo=UTC)
    working = slots.parse_cron("*/15 9-17 * * 1-5")
    # T0 is a Sunday: the next slot is Monday at 09:00, then every quarter of an hour.
    assert working.after(T0) == datetime(2026, 11, 16, 9, tzinfo=UTC)
    assert working.after(datetime(2026, 11, 16, 9, 7, tzinfo=UTC)) == datetime(2026, 11, 16, 9, 15, tzinfo=UTC)
    assert working.after(datetime(2026, 11, 20, 17, 45, tzinfo=UTC)) == datetime(2026, 11, 23, 9, tzinfo=UTC)
    # Both day fields restricted: either matches (the 1st of the month, or a Sunday, 0 or 7).
    either = slots.parse_cron("0 0 1 * 7")
    assert either.after(datetime(2026, 11, 16, tzinfo=UTC)) == datetime(2026, 11, 22, tzinfo=UTC)
    assert either.after(datetime(2026, 11, 29, 1, tzinfo=UTC)) == datetime(2026, 12, 1, tzinfo=UTC)
    assert slots.parse_cron("30 4 29 2 *").after(T0) == datetime(2028, 2, 29, 4, 30, tzinfo=UTC)
    assert nightly.count(T0, T0 + timedelta(days=3), up_to=1000) == 3


@pytest.mark.parametrize(
    ("expression", "detail"),
    [
        ("0 0 31 2 *", "never due"),
        ("60 * * * *", "not within 0-59"),
        ("0 0 * * MON", "not *, a number or a range"),
        ("0 2 * *", "five fields"),
        ("5/2 * * * *", "takes a step after * or a range"),
        ("*/0 * * * *", "at least 1"),
        ("0 9-5 * * *", "runs backwards"),
    ],
)
def test_a_cron_expression_it_cannot_read_is_refused_naming_why(expression: str, detail: str) -> None:
    with pytest.raises(ValueError, match=re.escape(detail)):
        slots.parse_cron(expression)


def test_a_schedule_is_due_one_way() -> None:
    for both in ({"every_seconds": None, "cron": None}, {"every_seconds": 60, "cron": "* * * * *"}):
        with pytest.raises(ValueError, match="every_seconds"):
            slots.timing(**both)


# --- one turn, called directly ------------------------------------------------------------------


@pytest.fixture
def records(tmp_path: Path) -> Iterator[SqlRecordStore]:
    found = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield found
    found.close()


async def publish(store: EvalStore, principal: Principal = SINGLE_USER) -> Any:
    if await store.get_project(principal, DEFAULT_PROJECT) is None:
        await store.create_project(principal, Project(id=DEFAULT_PROJECT, name="Default"))
    return await store.create_dataset(principal, DEFAULT_PROJECT, "regression", CASES)


async def saved_suite(records: SqlRecordStore) -> Suite:
    draft = Suite(
        id="suite-1",
        name="Regression",
        scorers=[builtin_scorer("contains_expected"), builtin_scorer("correctness")],
        policies=[Policy(scorer="contains_expected", threshold=0.8)],
        digest="0" * 64,
    )
    return await records.save_suite(
        SINGLE_USER, DEFAULT_PROJECT, draft.model_copy(update={"digest": suite_digest(draft)})
    )


async def make_schedule(
    store: EvalStore, records: SqlRecordStore, *, since: datetime, limits: RunLimits | None = None
) -> Work:
    dataset, suite = await publish(store), await saved_suite(records)
    await records.create_schedule(
        SINGLE_USER,
        DEFAULT_PROJECT,
        Schedule(
            id="sc1",
            project_id=DEFAULT_PROJECT,
            name="Every minute",
            every_seconds=60,
            suite_id=suite.id,
            suite_version=suite.version,
            suite_name=suite.name,
            scorers=suite.scorers,
            policies=suite.policies,
            target=Target(kind="agent", agent_id=AGENT),
            dataset_name=dataset.name,
            dataset_id=dataset.id,
            dataset_digest=dataset.digest,
            max_cases=100,
            limits=limits,
            active_since=since,
            created_at=since,
        ),
    )
    return Work(kind="schedule", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, schedule_id="sc1")


async def history(records: SqlRecordStore) -> list[ScheduledRun]:
    """The schedule's slots, oldest first."""
    return (await records.list_scheduled_runs(SINGLE_USER, DEFAULT_PROJECT, "sc1", limit=1000)).items[::-1]


def at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(schedule_module, "_now", lambda: moment)


async def test_slots_missed_while_no_worker_ran_are_run_once_and_a_busy_slot_is_skipped(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, gate = MemoryStore(), threading.Event()
    worker = InProcessWorker(store, records, targets={AGENT: Answers(gate=gate)}, judge=judge)
    services = ScheduleServices(store, lambda _p: ONE_RUN)
    work = await make_schedule(store, records, since=T0 - timedelta(seconds=30))
    # Before its first slot, a turn does nothing but note when it is due.
    at(monkeypatch, T0 - timedelta(seconds=5))
    await execute_schedule(services, records, worker, work)
    assert await history(records) == []
    progress = await records.schedule_progress(SINGLE_USER, DEFAULT_PROJECT, "sc1")
    assert progress is not None and progress.next_slot == T0

    # No worker for five minutes and ten seconds: the six slots due are one run, for the first.
    at(monkeypatch, T0 + timedelta(minutes=5, seconds=10))
    await execute_schedule(services, records, worker, work)
    await execute_schedule(services, records, worker, work)  # the same turn again changes nothing
    [first] = await history(records)
    assert (first.slot, first.missed, first.state, first.run_id) == (T0, 5, "admitted", scheduled_run_id("sc1", T0))
    progress = await records.schedule_progress(SINGLE_USER, DEFAULT_PROJECT, "sc1")
    assert progress is not None and (progress.next_slot, progress.last_slot) == (T0 + timedelta(minutes=6), T0)

    # The next slot comes while that run is in progress: one run at a time, so it is skipped.
    at(monkeypatch, T0 + timedelta(minutes=6, seconds=1))
    await execute_schedule(services, records, worker, work)
    _, busy = await history(records)
    assert (busy.state, busy.run_id, busy.reason) == ("skipped", None, "An eval run is already in progress")

    # Once it has run, the next slot's run is admitted, and each run is the schedule's.
    gate.set()
    await worker.drain()
    at(monkeypatch, T0 + timedelta(minutes=7, seconds=1))
    await execute_schedule(services, records, worker, work)
    await worker.drain()
    *_, third = await history(records)
    assert (third.slot, third.state, third.missed) == (T0 + timedelta(minutes=7), "admitted", 0)
    for entry in (first, third):
        assert entry.run_id is not None
        run = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, entry.run_id)
        assert run is not None and run.state == "completed" and (run.counts.passed, run.counts.failed) == (10, 0)
        assert run.provenance[SCHEDULE_KEY] == {"id": "sc1", "name": "Every minute", "slot": entry.slot.isoformat()}
        assert (run.suite_id, run.suite_version, run.source) == ("suite-1", 1, "console")
        # Its suite's scoring job was recorded before the run was queued, with the schedule's scorers.
        [job] = await records.list_scoring_jobs(SINGLE_USER, DEFAULT_PROJECT, run.id)
        assert (job.id, job.state, [s.name for s in job.scorers]) == (
            suite_job_id(run.id),
            "queued",
            ["contains_expected", "correctness"],
        )


async def test_a_run_that_cannot_be_admitted_is_skipped_with_the_reason(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    editions = {"current": COMMUNITY}
    services = ScheduleServices(store, lambda _p: editions["current"])
    work = await make_schedule(store, records, since=T0 - timedelta(seconds=30))
    no_target = InProcessWorker(store, records, judge=judge)
    reasons = []
    for minute, worker in enumerate(
        [no_target, no_target, InProcessWorker(store, records, targets={AGENT: Answers()})]
    ):
        at(monkeypatch, T0 + timedelta(minutes=minute, seconds=1))
        await execute_schedule(services, records, worker, work)
        reasons.append((await history(records))[-1].reason)
        editions["current"] = ONE_RUN
    assert reasons == [
        "The Community edition does not include monitoring",
        "Agent not found",
        "Model judges require a judge configured for this installation's worker",
    ]
    assert all(entry.state == "skipped" and entry.run_id is None for entry in await history(records))
    # An archived project takes no more runs.
    await store.archive_project(SINGLE_USER, DEFAULT_PROJECT)
    at(monkeypatch, T0 + timedelta(minutes=3, seconds=1))
    await execute_schedule(
        services, records, InProcessWorker(store, records, targets={AGENT: Answers()}, judge=judge), work
    )
    assert (await history(records))[-1].reason == "This evaluation project is archived or unavailable"
    assert (await records.list_runs(SINGLE_USER, DEFAULT_PROJECT)).items == []


async def test_a_turn_that_stopped_after_admitting_its_run_never_admits_a_second(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    worker = InProcessWorker(store, records, targets={AGENT: Answers()}, judge=judge)
    services = ScheduleServices(store, lambda _p: ONE_RUN)
    work = await make_schedule(store, records, since=T0 - timedelta(seconds=30))
    recorded = records.add_scheduled_run

    async def lost(*_args: Any) -> Any:
        raise ConnectionError("the database went away")

    monkeypatch.setattr(records, "add_scheduled_run", lost)
    at(monkeypatch, T0 + timedelta(seconds=1))
    with pytest.raises(ConnectionError):
        await execute_schedule(services, records, worker, work)
    monkeypatch.setattr(records, "add_scheduled_run", recorded)
    await execute_schedule(services, records, worker, work)
    [entry] = await history(records)
    assert (entry.state, entry.run_id) == ("admitted", scheduled_run_id("sc1", T0))
    assert len((await records.list_runs(SINGLE_USER, DEFAULT_PROJECT)).items) == 1


async def test_a_paused_schedule_admits_nothing_and_resumed_skips_the_slots_of_its_pause(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    worker = InProcessWorker(store, records, targets={AGENT: Answers()}, judge=judge)
    services = ScheduleServices(store, lambda _p: ONE_RUN)
    work = await make_schedule(store, records, since=T0 - timedelta(seconds=30))
    found = await records.get_schedule(SINGLE_USER, DEFAULT_PROJECT, "sc1")
    assert found is not None
    paused = await records.update_schedule(
        SINGLE_USER, DEFAULT_PROJECT, found.model_copy(update={"state": "paused"}), expected_revision=1
    )
    at(monkeypatch, T0 + timedelta(minutes=10))
    await execute_schedule(services, records, worker, work)
    assert await history(records) == []
    resumed_at = T0 + timedelta(minutes=10, seconds=20)
    resumed = paused.model_copy(update={"state": "active", "active_since": resumed_at})
    await records.update_schedule(SINGLE_USER, DEFAULT_PROJECT, resumed, expected_revision=2)
    at(monkeypatch, T0 + timedelta(minutes=11, seconds=5))
    await execute_schedule(services, records, worker, work)
    [entry] = await history(records)
    assert (entry.slot, entry.missed) == (T0 + timedelta(minutes=11), 0)


async def test_a_scheduled_run_that_does_not_complete_cancels_its_suite_job(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Queued:
        """The queue's worker, as far as the follow-up after a run needs it."""

        judges = True

        def __init__(self) -> None:
            self.submitted: list[Work] = []

        async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
            raise AssertionError("not asked")

        async def submit(self, work: Work) -> None:
            self.submitted.append(work)

    store, follow = MemoryStore(), Queued()
    worker = InProcessWorker(store, records, targets={AGENT: Answers(delay=0.3)}, judge=judge)
    services = ScheduleServices(store, lambda _p: ONE_RUN)
    work = await make_schedule(store, records, since=T0 - timedelta(seconds=30), limits=RunLimits(max_seconds=0.1))
    at(monkeypatch, T0 + timedelta(seconds=1))
    await execute_schedule(services, records, worker, work)
    await worker.drain()
    [entry] = await history(records)
    assert entry.run_id is not None
    stopped = Work(kind="run", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=entry.run_id)
    run = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, entry.run_id)
    assert run is not None and run.state == "stopped" and run.provenance["limits"]["max_seconds"] == 0.1
    await after_scheduled_run(records, follow, stopped)
    [job] = await records.list_scoring_jobs(SINGLE_USER, DEFAULT_PROJECT, entry.run_id)
    assert (job.state, job.error, follow.submitted) == ("cancelled", NOT_COMPLETED, [])

    # A run that completed has its suite job queued, once.
    at(monkeypatch, T0 + timedelta(minutes=1, seconds=1))
    fast = InProcessWorker(store, records, targets={AGENT: Answers()}, judge=judge)
    await execute_schedule(services, records, fast, work)
    await fast.drain()
    *_, done = await history(records)
    assert done.run_id is not None
    completed = Work(kind="run", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=done.run_id)
    await after_scheduled_run(records, follow, completed)
    assert [(w.kind, w.job_id) for w in follow.submitted] == [("scoring", suite_job_id(done.run_id))]


async def test_a_threshold_on_a_schedule_s_gate_results_breaches_and_recovers(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, target = MemoryStore(), Answers(right=0)  # wrong, until it is fixed
    worker = InProcessWorker(store, records, targets={AGENT: target}, judge=judge)
    services = ScheduleServices(store, lambda _p: ONE_RUN)
    work = await make_schedule(store, records, since=T0 - timedelta(seconds=30))
    gates = Threshold(
        id="t1",
        project_id=DEFAULT_PROJECT,
        name="Regression gate",
        source=ScheduleGates(schedule_id="sc1"),
        measure="pass_rate",
        direction="at_least",
        value=0.5,
        window_seconds=600,
        min_count=4,
        webhook=WebhookChannel(url="https://alerts.example/hook"),
    )
    await records.create_threshold(SINGLE_USER, DEFAULT_PROJECT, gates, webhook_secret="s" * 16)
    states = []
    for minute in range(14):
        if minute == 4:
            target.right = 10**6  # the system under test is fixed
        at(monkeypatch, T0 + timedelta(minutes=minute, seconds=1))
        await execute_schedule(services, records, worker, work)
        await worker.drain()
        entry = (await history(records))[-1]
        assert entry.run_id is not None
        # The run's suite scores it, as the queue's worker queues its suite job once it completes.
        scoring = Work(
            kind="scoring",
            principal=SINGLE_USER,
            project_id=DEFAULT_PROJECT,
            run_id=entry.run_id,
            job_id=suite_job_id(entry.run_id),
        )
        await worker.run(scoring)
        status = await records.threshold_status(SINGLE_USER, DEFAULT_PROJECT, "t1")
        assert status is not None
        states.append(status.state)
    # Each slot records its run's gate: failed while the target was wrong, passed once it was fixed.
    assert [e.gate for e in await history(records)] == ["failed"] * 4 + ["passed"] * 10
    # Four failed gates in the window breach it; it recovers once its interval clears the value,
    # with nine of the window's ten gates passed. One alert per change of state.
    assert states == ["unknown"] * 3 + ["breached"] * 9 + ["ok"] * 2
    alerts = (await records.list_alerts(SINGLE_USER, DEFAULT_PROJECT, "t1")).items
    assert [(a.event, a.sequence, a.channel) for a in alerts] == [
        ("recovered", 2, "webhook"),
        ("breached", 1, "webhook"),
    ]
    assert alerts[1].payload["threshold"]["source"] == {"kind": "gate", "schedule_id": "sc1"}
    assert alerts[1].payload["observation"]["count"] == 4 and alerts[1].payload["observation"]["estimate"] == 0.0


# --- through the routes and the durable queue ----------------------------------------------------


@pytest.fixture(params=DATABASES)
def database(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[str, str]]:
    """A database URL and the schema the stores keep their tables in."""
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'martian.db'}", "martian"
    else:
        yield request.getfixturevalue("owner_url"), request.getfixturevalue("schema")


@pytest.fixture
def every_second(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schedule may be due every second, so a test sees several slots in a few seconds."""
    monkeypatch.setattr(slots, "MIN_INTERVAL_SECONDS", 1)


class Core:
    """The core with a queue whose worker runs schedules, two pieces of work at once, in the app's
    process unless ``serving`` is False."""

    def __init__(
        self,
        url: str,
        in_schema: str,
        target: Answers,
        *,
        entitlements: Callable[[Principal], Entitlements] = lambda _p: ONE_RUN,
        serving: bool = True,
        principal: Principal = SINGLE_USER,
    ) -> None:
        self.store = MemoryStore()
        asyncio.run(publish(self.store, principal))
        self.records, self.queue = SqlRecordStore(url, schema=in_schema), SqlWorkQueue(url, schema=in_schema)
        self.principal: dict[str, Principal] = {"current": principal}
        runner = InProcessWorker(
            self.store, self.records, organisation_id=principal.organisation_id, targets={AGENT: target}, judge=judge
        )
        self.worker = QueueWorker(
            self.queue,
            runner,
            self.records,
            in_process=serving,
            lease_seconds=5,
            poll_seconds=POLL,
            concurrency=2,
            schedules=ScheduleServices(self.store, entitlements),
        )
        app = create_app(
            store=self.store,
            records=self.records,
            principal=lambda: self.principal["current"],
            worker=self.worker,
            entitlements=entitlements,
            judge_settings=MemoryJudgeSettings(),
        )
        self.client = TestClient(app)

    def close(self) -> None:
        self.queue.close()
        self.records.close()

    def call(self, method: str, path: str, status: int, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        assert response.status_code == status, response.text
        return response.json() if response.content else None

    def suite(self) -> str:
        """Save the suite the schedules run: a check gated at 80% and a model judge."""
        saved = self.call(
            "POST",
            "/v1/evals/suites",
            201,
            json={
                "request_id": str(uuid.uuid4()),
                "name": "Regression",
                "scorers": ["contains_expected", "correctness"],
                "policies": [{"name": "contains_expected", "threshold": 0.8}],
            },
        )
        return str(saved["id"])

    def create(self, status: int = 201, **body: Any) -> Any:
        defaults = {
            "request_id": str(uuid.uuid4()),
            "name": "Every second",
            "agent_id": AGENT,
            "dataset_name": "regression",
            "every_seconds": 3600,
            "acknowledge_model_cost": True,
        }
        return self.call("POST", "/v1/evals/schedules", status, json={**defaults, **body})

    def history(self, schedule_id: str) -> list[dict[str, Any]]:
        """The schedule's history, newest first."""
        found: list[dict[str, Any]] = self.call("GET", f"/v1/evals/schedules/{schedule_id}/runs?limit=100", 200)[
            "items"
        ]
        return found

    def until(self, check: Callable[[], bool], seconds: float = SECONDS) -> None:
        started = time.monotonic()
        while not check():
            if time.monotonic() - started > seconds:
                raise TimeoutError("the condition never held")
            time.sleep(0.05)


@pytest.fixture
def core_on(database: tuple[str, str]) -> Iterator[Callable[..., Core]]:
    made: list[Core] = []

    def make(target: Answers, **options: Any) -> Core:
        made.append(Core(*database, target, **options))
        return made[-1]

    yield make
    for core in made:
        core.close()


def test_schedules_need_the_monitoring_entitlement_and_the_run_capability(core_on: Callable[..., Core]) -> None:
    editions = {"current": COMMUNITY}
    core = core_on(Answers(), entitlements=lambda _p: editions["current"], principal=MEMBER)
    with core.client:
        suite = core.suite()
        refused = "The Community edition does not include monitoring"
        assert core.call("GET", "/v1/evals/schedules", 403)["detail"] == refused
        assert core.create(403, suite_version_id=suite)["detail"] == refused
        editions["current"] = ONE_RUN
        core.principal["current"] = VIEWER
        assert core.create(403, suite_version_id=suite)["detail"] == "Requires member role"
        assert core.call("GET", "/v1/evals/schedules", 200) == {"items": []}
        core.principal["current"] = MEMBER
        made = core.create(suite_version_id=suite, every_seconds=None, cron="0  2 * * *")
        assert (made["state"], made["cron"], made["every_seconds"], made["timezone"]) == (
            "active",
            "0 2 * * *",
            None,
            "UTC",
        )
        assert made["next_slot"] is not None and datetime.fromisoformat(made["next_slot"]).time() == dtime(2)
        assert [(s["name"], s["kind"]) for s in made["scorers"]] == [
            ("contains_expected", "check"),
            ("correctness", "llm_judge"),
        ]
        core.principal["current"] = VIEWER
        assert core.call("GET", f"/v1/evals/schedules/{made['id']}", 200)["id"] == made["id"]
        assert core.call("GET", f"/v1/evals/schedules/{made['id']}/runs", 200)["items"] == []
        core.call("POST", f"/v1/evals/schedules/{made['id']}/pause", 403)
        core.call("DELETE", f"/v1/evals/schedules/{made['id']}", 403)


def test_a_schedule_refuses_what_it_cannot_run(core_on: Callable[..., Core]) -> None:
    core = core_on(Answers())
    with core.client:
        suite = core.suite()
        for body, status, detail in [
            ({"suite_version_id": str(uuid.uuid4())}, 404, "Suite version not found"),
            ({"suite_version_id": suite, "dataset_name": "missing"}, 422, "Unknown dataset"),
            ({"suite_version_id": suite, "agent_id": str(uuid.uuid4())}, 404, "Agent not found"),
            ({"suite_version_id": suite, "acknowledge_model_cost": False}, 422, "cost acknowledgement"),
            ({"suite_version_id": suite, "every_seconds": None}, 422, "every_seconds"),
            ({"suite_version_id": suite, "cron": "0 2 * * *"}, 422, "every_seconds"),
            ({"suite_version_id": suite, "every_seconds": None, "cron": "0 0 31 2 *"}, 422, "never due"),
            ({"suite_version_id": suite, "every_seconds": 59}, 422, "every_seconds is at least 60"),
            ({"suite_version_id": suite, "limits": {"max_turns": 3}}, 422, "Extra inputs"),
        ]:
            assert detail in str(core.create(status, **body)["detail"]), body
        # The same request again is the same schedule; its id with another request is refused.
        request_id = str(uuid.uuid4())
        first = core.create(request_id=request_id, suite_version_id=suite)
        again = core.create(request_id=request_id, suite_version_id=suite)
        assert again["id"] == first["id"]
        core.create(409, request_id=request_id, suite_version_id=suite, every_seconds=7200)
    # A worker that does not run schedules cannot take one.
    store = MemoryStore()
    asyncio.run(publish(store))
    for worker in (
        InProcessWorker(store, core.records, targets={AGENT: Answers()}),
        QueueWorker(core.queue, InProcessWorker(store, core.records, targets={AGENT: Answers()}), core.records),
    ):
        app = create_app(
            store=store,
            records=core.records,
            principal=lambda: SINGLE_USER,
            worker=worker,
            entitlements=lambda _p: ONE_RUN,
        )
        with TestClient(app) as client:
            body = {"request_id": str(uuid.uuid4()), "name": "x", "suite_version_id": suite, "agent_id": AGENT}
            refused = client.post(
                "/v1/evals/schedules", json={**body, "dataset_name": "regression", "every_seconds": 3600}
            )
            assert (refused.status_code, refused.json()["detail"]) == (
                503,
                "This installation's worker does not run schedules",
            )


def test_a_threshold_names_a_schedule_s_gate_results_through_its_routes(core_on: Callable[..., Core]) -> None:
    core = core_on(Answers())
    with core.client:
        made = core.create(suite_version_id=core.suite())
        body = {"request_id": str(uuid.uuid4()), "name": "Nightly gate", "measure": "pass_rate", "value": 0.8}
        source = {"kind": "gate", "schedule_id": made["id"]}
        gate = core.call("POST", "/v1/evals/thresholds", 201, json={**body, "source": source, "min_count": 5})
        assert gate["source"] == source and gate["status"]["state"] == "unknown"
        mean = {**body, "request_id": str(uuid.uuid4()), "measure": "mean", "min_count": 5, "source": source}
        assert core.call("POST", "/v1/evals/thresholds", 422, json=mean)["detail"] == (
            "A schedule's gate results are measured by their pass_rate"
        )
        missing = {
            **body,
            "request_id": str(uuid.uuid4()),
            "source": {"kind": "gate", "schedule_id": str(uuid.uuid4())},
        }
        assert core.call("POST", "/v1/evals/thresholds", 404, json=missing)["detail"] == "Schedule not found"
        listed = core.call("GET", f"/v1/evals/thresholds?schedule_id={made['id']}", 200)["items"]
        assert [t["id"] for t in listed] == [gate["id"]]
        # Deleting the schedule deletes the thresholds on its gate results.
        core.call("DELETE", f"/v1/evals/schedules/{made['id']}", 204)
        core.call("GET", f"/v1/evals/thresholds/{gate['id']}", 404)


@pytest.mark.usefixtures("every_second")
def test_a_scheduled_suite_runs_unattended_and_its_history_compares_each_run_with_the_last(
    core_on: Callable[..., Core],
) -> None:
    # Right for the first run's ten cases, wrong after: the regression the schedule is for. Each
    # run takes over a second, so the slot after it is admitted comes while it is in progress.
    target = Answers(right=10, delay=0.2)
    core = core_on(target)
    with core.client:
        made = core.create(suite_version_id=core.suite(), every_seconds=1)
        path = f"/v1/evals/schedules/{made['id']}"

        def two_scored_runs() -> bool:
            done = [e for e in core.history(made["id"]) if e["scoring"] and e["scoring"]["state"] == "completed"]
            return len(done) >= 2

        core.until(two_scored_runs)
        assert core.call("POST", f"{path}/pause", 200)["state"] == "paused"
        entries = core.history(made["id"])
        shown = core.call("GET", path, 200)
    slots_seen = [datetime.fromisoformat(e["slot"]) for e in entries]
    assert slots_seen == sorted(slots_seen, reverse=True) and len(set(slots_seen)) == len(slots_seen)
    assert all(slot.microsecond == 0 for slot in slots_seen)
    # A slot due while the previous run was in progress was skipped, saying why, and queued nothing.
    skipped = [e for e in entries if e["state"] == "skipped"]
    assert skipped and all(
        (e["reason"], e["run_id"], e["comparison"]) == ("An eval run is already in progress", None, None)
        for e in skipped
    )
    scored = [e for e in entries if e["scoring"] and e["scoring"]["state"] == "completed"][::-1]
    first, second = scored[0], scored[1]
    assert (first["state"], first["pass_rate"], first["counts"]["passed"]) == ("completed", 1.0, 10)
    assert first["scoring"]["gate"] == "passed" and first["comparison"] is None
    assert {c["name"]: c["pass_rate"] for c in first["scoring"]["criteria"]} == {
        "contains_expected": 1.0,
        "correctness": 1.0,
    }
    assert (second["pass_rate"], second["scoring"]["gate"]) == (0.0, "failed")
    assert (first["gate"], second["gate"]) == ("passed", "failed")
    # The paired comparison with the previous completed run of the schedule: every case regressed.
    comparison = second["comparison"]
    assert (comparison["baseline_id"], comparison["candidate_id"]) == (first["run_id"], second["run_id"])
    assert comparison["compatible"] and comparison["changes"]["regressed"] == 10
    assert (comparison["significance"]["label"], comparison["significance"]["regressed"]) == ("worse", 10)
    assert comparison["baseline_job_id"] == suite_job_id(first["run_id"])
    assert comparison["criteria_significance"]["contains_expected"]["label"] == "worse"
    assert "items" not in comparison
    assert shown["progress"]["last_slot"] is not None and shown["next_slot"] is None  # paused
    # The runs are the schedule's, each scored by its suite.
    with core.client:
        run = core.call("GET", f"/v1/evals/{second['run_id']}", 200)
    assert run["provenance"][SCHEDULE_KEY]["id"] == made["id"] and run["provenance"]["suite"]["name"] == "Regression"


@pytest.mark.usefixtures("every_second")
def test_slots_missed_while_no_worker_served_the_queue_run_once_when_it_returns(
    core_on: Callable[..., Core],
) -> None:
    core = core_on(Answers(), serving=False)
    with core.client:
        made = core.create(suite_version_id=core.suite(), every_seconds=1)
        created = datetime.fromisoformat(made["created_at"])
        time.sleep(3.4)  # three slots, and no worker to take them
        returned = datetime.now(UTC)
        assert core.client.portal is not None
        serving = core.client.portal.start_task_soon(core.worker.serve)
        try:
            core.until(lambda: any(e["state"] == "completed" for e in core.history(made["id"])))
            core.call("POST", f"/v1/evals/schedules/{made['id']}/pause", 200)
        finally:
            serving.cancel()
        entries = core.history(made["id"])[::-1]
    first = entries[0]
    # One run for the slots that passed without a worker, for the first of them, counting the rest.
    assert datetime.fromisoformat(first["slot"]) == slots.timing(every_seconds=1, cron=None).after(created)
    assert first["missed"] >= 2 and first["state"] == "completed"
    assert datetime.fromisoformat(first["fired_at"]) >= returned
    assert all(datetime.fromisoformat(e["slot"]) > returned - timedelta(seconds=1) for e in entries[1:])


@pytest.mark.usefixtures("every_second")
def test_pausing_resuming_and_deleting_a_schedule_through_the_queue(core_on: Callable[..., Core]) -> None:
    core = core_on(Answers())
    with core.client:
        made = core.create(suite_version_id=core.suite(), every_seconds=1)
        path = f"/v1/evals/schedules/{made['id']}"
        work = Work(kind="schedule", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, schedule_id=made["id"])
        core.until(lambda: any(e["state"] == "completed" for e in core.history(made["id"])))

        assert core.call("POST", f"{path}/pause", 200)["state"] == "paused"
        paused_at, during = datetime.now(UTC), len(core.history(made["id"]))
        time.sleep(2.5)  # two slots: a paused schedule's turns admit nothing
        assert len(core.history(made["id"])) <= during + 1  # a turn already under way may finish

        resumed = core.call("POST", f"{path}/resume", 200)
        resumed_at = datetime.fromisoformat(resumed["active_since"])
        assert resumed["state"] == "active" and resumed["revision"] == 3
        core.until(lambda: any(datetime.fromisoformat(e["slot"]) > resumed_at for e in core.history(made["id"])))
        slots_seen = [datetime.fromisoformat(e["slot"]) for e in core.history(made["id"])]
        assert not [s for s in slots_seen if paused_at + timedelta(seconds=1) < s <= resumed_at]
        run_id = next(e["run_id"] for e in core.history(made["id"]) if e["run_id"])

        core.call("DELETE", path, 204)
        core.call("GET", path, 404)
        core.call("GET", f"{path}/runs", 404)
        portal = core.client.portal
        assert portal is not None
        core.until(lambda: not portal.call(core.queue.contains, SINGLE_USER, DEFAULT_PROJECT, entry_id(work)))
        # The runs it started stay.
        core.call("GET", f"/v1/evals/{run_id}", 200)
