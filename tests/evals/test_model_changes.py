"""Evaluating again when the model behind an agent changes (MartianSecurity/martian-notes#200).

The model is recorded on every trace (mapped from its spans' attributes, kept by the store and a
filter of the trace search: the store contract) and on every run, as the target reports it: an
HTTP target at a path of its JSON response or in a header, a function or a registered agent
through a ``TargetReply``. A schedule set to run on a model change watches the project's traces
or the target's runs, and a changed model starts its suite once and is noted in its history; a
model that flaps back and forth starts no storm of runs. A fake target whose reported model
changes and fake traces stand in for the agent; nothing calls a model or a network target.
"""

from __future__ import annotations

import asyncio
import functools
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import martian.evals.worker.execute as execute_module
import martian.evals.worker.schedule as schedule_module
from martian.evals.judge_settings import MemoryJudgeSettings
from martian.evals.model import Case, Policy, Project, Run, Suite, Target, Trace, TraceQuery, digest, suite_digest
from martian.evals.records import (
    ModelChange,
    ModelWatch,
    ModelWatchProgress,
    Schedule,
    ScheduledRun,
    ScheduleProgress,
)
from martian.evals.records.queue import SqlWorkQueue
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import COMMUNITY, DEFAULT_PROJECT, SINGLE_USER, Entitlements, Principal, create_app
from martian.evals.server.entitlements import PAID
from martian.evals.server.launch import admit_run
from martian.evals.store import EvalStore, MemoryStore
from martian.evals.worker import InProcessWorker, QueueWorker, ScheduleServices, TargetReply, Work, Worker
from martian.evals.worker.model_watch import Observation, due_change, observe, read_traces
from martian.evals.worker.schedule import SCHEDULE_KEY, execute_schedule, next_due, scheduled_run_id
from martian.evals.worker.scorers import builtin_scorer
from tests.evals import test_records
from tests.evals.store_contract import EmptyFirstPages
from tests.evals.write_fixture import judge

needs_postgres = test_records.needs_postgres
schema, owner_url = test_records.schema, test_records.owner_url  # the record tests' fixtures

AGENT = str(uuid.UUID(int=11))
OTHER_AGENT = str(uuid.UUID(int=12))
OLD, NEW = "gpt-4o-2024-05-13", "gpt-4o-2024-08-06"
T0 = datetime(2026, 11, 15, 9, 0, 7, tzinfo=UTC)
"""Seven seconds after a minute, so no turn falls on a slot's boundary."""
ENTERPRISE = Entitlements(edition="enterprise", features=PAID, limits={"concurrent_runs": 2})
CASES = [
    Case(id=f"q-{n}", inputs={"message": f"What is answer {n}?"}, expectations={"expected_response": f"answer {n}"})
    for n in range(3)
]


def answer(case: Case) -> str:
    return f"answer {case.inputs['message'].removeprefix('What is answer ').removesuffix('?')}"


class Switching:
    """The target under test: it answers every case right and reports ``model``, which a provider
    may change at any time, as the model that answered."""

    def __init__(self, model: str = OLD) -> None:
        self.model, self.calls = model, 0

    async def __call__(self, case: Case) -> TargetReply:
        self.calls += 1
        return TargetReply(answer(case), model=self.model)


# --- the model a target reports ----------------------------------------------------------------


def test_an_http_target_names_where_its_response_reports_the_model() -> None:
    base = {"kind": "http", "url": "https://agent.example/chat", "body": {"message": "{{input}}"}}
    assert Target(**base, model_path="model").model_path == "model"  # type: ignore[arg-type]
    assert Target(**base, model_header="X-Served-Model").model_header == "X-Served-Model"  # type: ignore[arg-type]
    for fields, problem in (
        ({**base, "model_path": "model", "model_header": "X-Model"}, "not both"),
        ({**base, "model_header": "X Model"}, "string_pattern_mismatch"),
        ({"kind": "agent", "agent_id": AGENT, "model_path": "model"}, "model_path does not apply"),
    ):
        with pytest.raises(ValidationError, match=problem):
            Target(**fields)  # type: ignore[arg-type]
    # Unset, the fields are left out of the target as written, so its pin's digest is unchanged.
    written = Target(**base).model_dump(mode="json")  # type: ignore[arg-type]
    assert "model_path" not in written and "model_header" not in written
    assert digest(written) == digest(
        {**base, "file": None, "function": None, "response_path": None, "headers": {}, "agent_id": None}
    )


class ModelEndpoint:
    """An HTTP target on loopback that answers each case right under ``reply`` and names
    ``model`` as the model that answered, in its JSON and in ``X-Served-Model``, unless
    ``model`` is None."""

    def __init__(self) -> None:
        self.model: str | None = OLD
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                case = Case(id="c", inputs={"message": body["message"]})
                reply: dict[str, Any] = {"reply": answer(case)}
                if endpoint.model is not None:
                    reply["model"] = endpoint.model
                data = json.dumps(reply).encode()
                self.send_response(200)
                if endpoint.model is not None:
                    self.send_header("X-Served-Model", endpoint.model)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def target(self, **reports: str) -> Target:
        host, port = self._server.server_address[:2]
        return Target(
            kind="http",
            url=f"http://{host!s}:{port}/chat",
            body={"message": "{{input}}"},
            response_path="reply",
            **reports,
        )

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def records(tmp_path: Path) -> Iterator[SqlRecordStore]:
    found = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield found
    found.close()


async def publish(store: EvalStore) -> Any:
    if await store.get_project(SINGLE_USER, DEFAULT_PROJECT) is None:
        await store.create_project(SINGLE_USER, Project(id=DEFAULT_PROJECT, name="Default"))
    found = await store.find_dataset(SINGLE_USER, DEFAULT_PROJECT, "regression")
    return found or await store.create_dataset(SINGLE_USER, DEFAULT_PROJECT, "regression", CASES)


async def launch(store: EvalStore, records: SqlRecordStore, worker: InProcessWorker, agent_id: str = AGENT) -> Run:
    """Admit a run of ``agent_id`` as a launch does, run it, and return it as it ended."""
    dataset = await publish(store)
    target = Target(kind="agent", agent_id=agent_id)
    run = await admit_run(
        store,
        records,
        worker,
        SINGLE_USER,
        entitlements=ENTERPRISE,
        run_id=str(uuid.uuid4()),
        project=DEFAULT_PROJECT,
        target=target,
        pin=await worker.check_target(SINGLE_USER, target),
        dataset=dataset,
        max_cases=100,
    )
    await worker.drain()
    ended = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
    assert ended is not None and ended.state == "completed", ended
    return ended


async def case_traces(store: EvalStore, run: Run) -> list[Trace]:
    page = await store.search_traces(SINGLE_USER, TraceQuery(project_id=DEFAULT_PROJECT, run_id=run.id, limit=100))
    return [found for t in page.items if (found := await store.get_trace(SINGLE_USER, DEFAULT_PROJECT, t.id))]


async def test_an_http_target_s_reported_model_is_on_each_case_s_trace_and_on_the_run(
    records: SqlRecordStore,
) -> None:
    store, endpoint = MemoryStore(), ModelEndpoint()
    try:
        for reports, served in (({"model_path": "model"}, OLD), ({"model_header": "X-Served-Model"}, NEW)):
            endpoint.model = served
            worker = InProcessWorker(store, records, targets={AGENT: endpoint.target(**reports)})
            run = await launch(store, records, worker)
            assert run.provenance["model"] == {"name": served, "cases": {served: 3}}, reports
            assert run.counts.passed == 3
            traces = await case_traces(store, run)
            assert [(t.model, t.spans[0].attributes.get("gen_ai.response.model")) for t in traces] == [
                (served, served)
            ] * 3
        # The model is searchable: each run's traces are found by the model that answered them.
        for served in (OLD, NEW):
            page = await store.search_traces(SINGLE_USER, TraceQuery(project_id=DEFAULT_PROJECT, model=served))
            assert len(page.items) == 3
        # A response that names no model records none, and its case stands as it was.
        endpoint.model = None
        worker = InProcessWorker(store, records, targets={AGENT: endpoint.target(model_path="model")})
        silent = await launch(store, records, worker)
        assert "model" not in silent.provenance and silent.counts.passed == 3
        assert {t.model for t in await case_traces(store, silent)} == {None}
    finally:
        endpoint.close()


async def test_a_function_or_a_registered_agent_reports_its_model_with_a_target_reply(
    records: SqlRecordStore,
) -> None:
    class Agents:
        """A registered agent through a stand-in for martian-agents, which reports its model."""

        async def check(self, principal: Principal, agent_id: str) -> dict[str, Any]:
            return {"kind": "dynamic", "agent_id": agent_id, "revision": 1}

        async def call(self, principal: Principal, run: Run, case: Case, *, timeout_seconds: float) -> Any:
            return TargetReply(answer(case), model="  agent-model-7  ")

    async def plain(case: Case) -> str:
        return answer(case)

    store, switching = MemoryStore(), Switching()
    worker = InProcessWorker(store, records, targets={AGENT: switching, OTHER_AGENT: plain}, executor=Agents())
    run = await launch(store, records, worker)
    assert run.provenance["model"] == {"name": OLD, "cases": {OLD: 3}}
    # A model that changes during a run is counted case by case; the latest is its name.
    switching.model = NEW
    assert (await launch(store, records, worker)).provenance["model"] == {"name": NEW, "cases": {NEW: 3}}
    assert "model" not in (await launch(store, records, worker, OTHER_AGENT)).provenance
    registered = await launch(store, records, worker, str(uuid.UUID(int=13)))
    assert registered.provenance["model"] == {"name": "agent-model-7", "cases": {"agent-model-7": 3}}


# --- when a change counts ----------------------------------------------------------------------


def test_a_new_model_counts_once_it_settles_and_at_most_once_an_interval() -> None:
    settings = ModelWatch(source="traces", settle_seconds=300, min_interval_seconds=3600)

    def seen(watch: ModelWatchProgress, *sights: tuple[int, str]) -> ModelWatchProgress:
        return observe(watch, [Observation(T0 + timedelta(seconds=at), model) for at, model in sights])

    # The first model seen is where the watch starts; nothing runs for it.
    watch = seen(ModelWatchProgress(), (0, OLD), (10, OLD))
    assert (watch.model, watch.candidate) == (OLD, None)
    # A new model is a candidate from when it was first seen, and a change once it was alone for
    # settle_seconds.
    watch = seen(watch, (20, NEW), (30, NEW))
    assert (watch.candidate, watch.candidate_since) == (NEW, T0 + timedelta(seconds=20))
    assert due_change(watch, settings, T0 + timedelta(seconds=319)) is None
    assert due_change(watch, settings, T0 + timedelta(seconds=320)) == (OLD, NEW)
    # Seeing the old model again drops the candidate; a third model is a candidate from then.
    assert seen(watch, (40, OLD)).candidate is None
    third = seen(watch, (40, "gpt-4o-mini"))
    assert (third.candidate, third.candidate_since) == ("gpt-4o-mini", T0 + timedelta(seconds=40))
    # A model that flaps more often than settle_seconds never settles.
    flapping = seen(watch, *[(50 + 10 * n, NEW if n % 2 else OLD) for n in range(200)])
    assert all(due_change(flapping, settings, T0 + timedelta(seconds=50 + 10 * n)) is None for n in range(1, 200))
    # Within min_interval_seconds of the last change that ran, a settled change waits.
    ran = watch.model_copy(update={"last_change_at": T0})
    assert due_change(ran, settings, T0 + timedelta(seconds=3599)) is None
    assert due_change(ran, settings, T0 + timedelta(seconds=3600)) == (OLD, NEW)
    # A run of the schedule's own evaluated the model it reported: the watch moves there.
    own = observe(watch, [Observation(T0 + timedelta(seconds=50), NEW, own=True)])
    assert (own.model, own.candidate) == (NEW, None)


# --- the schedule's turns, called directly at fixed times ---------------------------------------


class Clock:
    """The time the schedule's turns and the worker's runs see."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = T0
        monkeypatch.setattr(schedule_module, "_now", lambda: self.now)
        monkeypatch.setattr(execute_module, "_now", lambda: self.now)

    def at(self, seconds: float) -> datetime:
        self.now = T0 + timedelta(seconds=seconds)
        return self.now


async def watching(
    store: EvalStore,
    records: SqlRecordStore,
    source: str,
    *,
    settle_seconds: float = 300,
    min_interval_seconds: float = 3600,
) -> Work:
    """A daily schedule, created at ``T0``, that also runs when the model changes."""
    dataset = await publish(store)
    draft = Suite(
        id="suite-1",
        name="Regression",
        scorers=[builtin_scorer("contains_expected")],
        policies=[Policy(scorer="contains_expected", threshold=0.8)],
        digest="0" * 64,
    )
    suite = await records.save_suite(
        SINGLE_USER, DEFAULT_PROJECT, draft.model_copy(update={"digest": suite_digest(draft)})
    )
    await records.create_schedule(
        SINGLE_USER,
        DEFAULT_PROJECT,
        Schedule(
            id="sc1",
            project_id=DEFAULT_PROJECT,
            name="Nightly",
            every_seconds=86400,
            on_model_change=ModelWatch(
                source=source,  # type: ignore[arg-type]
                settle_seconds=settle_seconds,
                min_interval_seconds=min_interval_seconds,
            ),
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
            active_since=T0,
            created_at=T0,
        ),
    )
    return Work(kind="schedule", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, schedule_id="sc1")


async def live(store: EvalStore, seconds: float, model: str, run_id: str | None = None, *, base: datetime = T0) -> None:
    """A trace of live traffic at ``base + seconds`` whose model call reports ``model``."""
    await store.record_trace(
        SINGLE_USER,
        Trace(
            id=f"tr-{uuid.uuid4().hex}",
            project_id=DEFAULT_PROJECT,
            name="chat",
            state="ok",
            request_time=base + timedelta(seconds=seconds),
            request="Where is my order?",
            response="On its way.",
            run_id=run_id,
            spans=[
                {  # type: ignore[list-item]
                    "id": uuid.uuid4().hex[:16],
                    "name": "chat",
                    "kind": "CHAT_MODEL",
                    "status": "ok",
                    "attributes": {"gen_ai.request.model": "gpt-4o", "gen_ai.response.model": model},
                }
            ],
        ),
    )


async def history(records: SqlRecordStore) -> list[ScheduledRun]:
    """The schedule's slots, oldest first."""
    return (await records.list_scheduled_runs(SINGLE_USER, DEFAULT_PROJECT, "sc1", limit=1000)).items[::-1]


async def turn(services: ScheduleServices, records: SqlRecordStore, worker: InProcessWorker, work: Work) -> None:
    await execute_schedule(services, records, worker, work)
    await worker.drain()


async def test_a_changed_model_in_the_project_s_traces_starts_the_suite_once_and_is_noted(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, store = Clock(monkeypatch), MemoryStore()
    worker = InProcessWorker(store, records, targets={AGENT: Switching(NEW)})
    services = ScheduleServices(store, lambda _p: ENTERPRISE, trace_settle_seconds=20)
    await publish(store)
    await live(store, -600, OLD)  # traffic before the schedule: the model it starts from
    work = await watching(store, records, "traces", settle_seconds=60)
    await live(store, 30, OLD)
    await store.start_run(SINGLE_USER, Run(id="eval-run", project_id=DEFAULT_PROJECT, source="ci", state="running"))
    await live(store, 40, NEW, run_id="eval-run")  # an eval run's trace is not traffic
    clock.at(70)
    await turn(services, records, worker, work)
    progress = await records.schedule_progress(SINGLE_USER, DEFAULT_PROJECT, "sc1")
    assert progress is not None and progress.model_watch is not None
    assert (progress.model_watch.model, progress.model_watch.candidate) == (OLD, None)
    assert progress.model_watch.watermark == T0 + timedelta(seconds=50)
    assert await history(records) == []

    # The provider changes the model: the new one is seen, but has not settled yet.
    for seconds in (100, 110, 120):
        await live(store, seconds, NEW)
    clock.at(150)
    await turn(services, records, worker, work)
    assert await history(records) == []
    # A minute after it was first seen, with nothing else seen, it is a change: the suite runs once.
    clock.at(181)
    await turn(services, records, worker, work)
    [entry] = await history(records)
    change = ModelChange(previous=OLD, current=NEW, at=T0 + timedelta(seconds=181))
    assert (entry.slot, entry.trigger, entry.model_change, entry.state) == (
        change.at,
        "model_change",
        change,
        "admitted",
    )
    assert entry.run_id == scheduled_run_id("sc1", change.at)
    run = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, entry.run_id)
    assert run is not None and run.state == "completed" and run.provenance["model"]["name"] == NEW
    assert run.provenance[SCHEDULE_KEY] == {
        "id": "sc1",
        "name": "Nightly",
        "slot": change.at.isoformat(),
        "trigger": "model_change",
        "model_change": {"from": OLD, "to": NEW},
    }
    assert change.note == f"model changed from {OLD} to {NEW}"
    # The same model goes on, and later turns start nothing more.
    for seconds in (200, 400, 900):
        await live(store, seconds, NEW)
        clock.at(seconds + 60)
        await turn(services, records, worker, work)
    assert len(await history(records)) == 1
    progress = await records.schedule_progress(SINGLE_USER, DEFAULT_PROJECT, "sc1")
    assert progress is not None and progress.model_watch is not None
    assert (progress.model_watch.model, progress.model_watch.change) == (NEW, None)
    assert progress.model_watch.last_change_at == T0 + timedelta(seconds=181)


async def test_a_model_flapping_back_and_forth_starts_no_storm(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, store = Clock(monkeypatch), MemoryStore()
    worker = InProcessWorker(store, records, targets={AGENT: Switching()})
    services = ScheduleServices(store, lambda _p: ENTERPRISE, trace_settle_seconds=0)
    await publish(store)
    await live(store, -60, OLD)
    work = await watching(store, records, "traces", settle_seconds=300, min_interval_seconds=3600)
    # Thirty minutes of traffic that flips model every ten seconds, with a turn every minute.
    for minute in range(30):
        for tick in range(6):
            seconds = 60 * minute + 10 * tick + 1
            await live(store, seconds, NEW if (minute * 6 + tick) % 2 else OLD)
        clock.at(60 * minute + 60)
        await turn(services, records, worker, work)
    assert await history(records) == []
    # Then it flips every ten minutes for three hours: each new model settles after five minutes,
    # but a change runs at most once an hour, and one that flipped back meanwhile runs nothing.
    for minute in range(30, 210):
        model = NEW if (minute // 10) % 2 == 0 else OLD
        await live(store, 60 * minute + 1, model)
        clock.at(60 * minute + 60)
        await turn(services, records, worker, work)
    # The model changed 17 times; the suite ran three times, an hour or more apart, each for the
    # change from the model the last run was for.
    changes = [entry.model_change for entry in await history(records)]
    assert [(c.previous, c.current, c.at - T0) for c in changes if c is not None] == [
        (OLD, NEW, timedelta(minutes=46)),
        (NEW, OLD, timedelta(minutes=116)),
        (OLD, NEW, timedelta(minutes=186)),
    ]
    assert len(changes) == 3


async def test_a_target_whose_reported_model_changes_starts_the_suite_on_its_runs(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, store, target = Clock(monkeypatch), MemoryStore(), Switching(OLD)
    worker = InProcessWorker(store, records, targets={AGENT: target, OTHER_AGENT: Switching("unrelated-model")})
    services = ScheduleServices(store, lambda _p: ENTERPRISE)
    clock.at(-120)
    await launch(store, records, worker)  # a run before the schedule: the model it starts from
    work = await watching(store, records, "runs", settle_seconds=60)
    clock.at(30)
    await launch(store, records, worker, OTHER_AGENT)  # another target's model is not this one's
    clock.at(60)
    await turn(services, records, worker, work)
    progress = await records.schedule_progress(SINGLE_USER, DEFAULT_PROJECT, "sc1")
    assert progress is not None and progress.model_watch is not None and progress.model_watch.model == OLD

    # The provider changes the model; the next run of the target reports it.
    target.model = NEW
    clock.at(100)
    await launch(store, records, worker)
    clock.at(130)
    await turn(services, records, worker, work)
    assert await history(records) == []
    clock.at(161)
    await turn(services, records, worker, work)
    [entry] = await history(records)
    assert (entry.trigger, entry.model_change) == (
        "model_change",
        ModelChange(previous=OLD, current=NEW, at=T0 + timedelta(seconds=161)),
    )
    assert entry.run_id is not None
    own = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, entry.run_id)
    assert own is not None and own.provenance["model"] == {"name": NEW, "cases": {NEW: 3}}
    # The schedule's own run reported the model it evaluated; more runs on it start nothing more.
    for seconds in (300, 600):
        clock.at(seconds)
        await launch(store, records, worker)
        clock.at(seconds + 90)
        await turn(services, records, worker, work)
    assert len(await history(records)) == 1


async def test_a_change_noticed_when_a_slot_is_due_rides_on_that_slot_s_run(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, store = Clock(monkeypatch), MemoryStore()
    worker = InProcessWorker(store, records, targets={AGENT: Switching(NEW)})
    services = ScheduleServices(store, lambda _p: ENTERPRISE, trace_settle_seconds=0)
    await publish(store)
    await live(store, -60, OLD)
    work = await watching(store, records, "traces", settle_seconds=0)
    await live(store, 50000, NEW)  # in the afternoon, before the first slot at midnight
    midnight = datetime(2026, 11, 16, tzinfo=UTC)
    clock.now = midnight + timedelta(seconds=3)
    await turn(services, records, worker, work)
    [entry] = await history(records)
    assert (entry.slot, entry.trigger, entry.state) == (midnight, "slot", "admitted")
    assert entry.model_change == ModelChange(previous=OLD, current=NEW, at=midnight)
    assert entry.run_id == scheduled_run_id("sc1", midnight)


async def test_a_turn_that_stopped_after_admitting_a_change_never_admits_a_second(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, store = Clock(monkeypatch), MemoryStore()
    worker = InProcessWorker(store, records, targets={AGENT: Switching(NEW)})
    services = ScheduleServices(store, lambda _p: ENTERPRISE, trace_settle_seconds=0)
    await publish(store)
    await live(store, -60, OLD)
    work = await watching(store, records, "traces", settle_seconds=0)
    await live(store, 10, NEW)
    recorded = records.add_scheduled_run

    async def lost(*_args: Any) -> Any:
        raise ConnectionError("the database went away")

    monkeypatch.setattr(records, "add_scheduled_run", lost)
    clock.at(20)
    with pytest.raises(ConnectionError):
        await turn(services, records, worker, work)
    monkeypatch.setattr(records, "add_scheduled_run", recorded)
    clock.at(80)
    await turn(services, records, worker, work)
    [entry] = await history(records)
    assert entry.model_change == ModelChange(previous=OLD, current=NEW, at=T0 + timedelta(seconds=20))
    assert len((await records.list_runs(SINGLE_USER, DEFAULT_PROJECT)).items) == 1


async def test_a_watching_schedule_is_due_every_watch_interval_and_at_once_with_more_to_read() -> None:
    schedule = Schedule(
        id="sc1",
        project_id=DEFAULT_PROJECT,
        name="Nightly",
        every_seconds=86400,
        suite_id="suite-1",
        suite_version=1,
        scorers=[builtin_scorer("contains_expected")],
        target=Target(kind="agent", agent_id=AGENT),
        dataset_name="regression",
        dataset_id="d1",
        max_cases=10,
        active_since=T0,
    )
    midnight = datetime(2026, 11, 16, tzinfo=UTC)
    assert next_due(schedule, None, T0, watch_seconds=60) == midnight
    watching_one = schedule.model_copy(update={"on_model_change": ModelWatch(source="traces")})
    assert next_due(watching_one, None, T0, watch_seconds=60) == T0 + timedelta(seconds=60)
    behind = ScheduleProgress(model_watch=ModelWatchProgress(caught_up=False))
    assert next_due(watching_one, behind, T0, watch_seconds=60) == T0
    paused = watching_one.model_copy(update={"state": "paused"})
    assert next_due(paused, None, T0, watch_seconds=60) == midnight


async def test_a_watch_starts_from_the_latest_model_past_a_search_page_with_no_traces() -> None:
    # A binding may answer a page of the trace search with no traces and a next_cursor; the watch
    # reads on to the traces before it started.
    store = EmptyFirstPages()
    await store.create_project(SINGLE_USER, Project(id=DEFAULT_PROJECT, name="Default"))
    await live(store, -600, OLD)
    schedule = Schedule(
        id="sc1",
        project_id=DEFAULT_PROJECT,
        name="Nightly",
        every_seconds=86400,
        on_model_change=ModelWatch(source="traces"),
        suite_id="suite-1",
        suite_version=1,
        scorers=[builtin_scorer("contains_expected")],
        target=Target(kind="agent", agent_id=AGENT),
        dataset_name="regression",
        dataset_id="d1",
        max_cases=10,
        active_since=T0,
        created_at=T0,
    )
    watch = await read_traces(store, SINGLE_USER, schedule, ModelWatchProgress(), T0 + timedelta(seconds=60))
    assert (watch.model, watch.caught_up) == (OLD, True)


# --- through the routes and the durable queue ----------------------------------------------------


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=needs_postgres)])
def database(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[str, str]]:
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'martian.db'}", "martian"
    else:
        yield request.getfixturevalue("owner_url"), request.getfixturevalue("schema")


def wait_for(check: Callable[[], bool], seconds: float = 60) -> None:
    started = time.monotonic()
    while not check():
        if time.monotonic() - started > seconds:
            raise TimeoutError("the condition never held")
        time.sleep(0.05)


def test_a_schedule_set_to_run_on_a_model_change_through_its_routes_and_the_queue(
    database: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("martian.evals.slots.MIN_INTERVAL_SECONDS", 1)
    url, in_schema = database
    store, editions = MemoryStore(), {"current": COMMUNITY}
    asyncio.run(publish(store))
    asyncio.run(live(store, -60, OLD, base=datetime.now(UTC)))  # the traffic before the schedule
    records, queue = SqlRecordStore(url, schema=in_schema), SqlWorkQueue(url, schema=in_schema)
    target = Switching(OLD)
    runner = InProcessWorker(store, records, targets={AGENT: target}, judge=judge)
    services = ScheduleServices(store, lambda _p: editions["current"], model_watch_seconds=0.1, trace_settle_seconds=0)
    worker: Worker = QueueWorker(
        queue, runner, records, in_process=True, lease_seconds=5, poll_seconds=0.05, schedules=services
    )
    app = create_app(
        store=store,
        records=records,
        principal=lambda: SINGLE_USER,
        worker=worker,
        entitlements=lambda _p: editions["current"],
        judge_settings=MemoryJudgeSettings(),
    )
    try:
        with TestClient(app) as client:

            def call(method: str, path: str, status: int, **kwargs: Any) -> Any:
                response = client.request(method, path, **kwargs)
                assert response.status_code == status, response.text
                return response.json() if response.content else None

            body = {
                "request_id": str(uuid.uuid4()),
                "name": "Nightly",
                "agent_id": AGENT,
                "dataset_name": "regression",
                "every_seconds": 86400,
                "on_model_change": {"source": "traces", "settle_seconds": 0, "min_interval_seconds": 3600},
            }
            # Scheduling is part of monitoring: Community is refused.
            assert (
                call("POST", "/v1/evals/schedules", 403, json={**body, "suite_version_id": str(uuid.uuid4())})["detail"]
                == "The Community edition does not include monitoring"
            )
            editions["current"] = ENTERPRISE
            suite = call(
                "POST",
                "/v1/evals/suites",
                201,
                json={"request_id": str(uuid.uuid4()), "name": "Regression", "scorers": ["contains_expected"]},
            )["id"]
            body["suite_version_id"] = suite
            short = {**body["on_model_change"], "min_interval_seconds": 0.5}  # type: ignore[dict-item]
            refused = call("POST", "/v1/evals/schedules", 422, json={**body, "on_model_change": short})
            assert "on_model_change.min_interval_seconds is at least 1" in str(refused["detail"])
            made = call("POST", "/v1/evals/schedules", 201, json=body)
            assert made["on_model_change"] == body["on_model_change"]
            assert call("POST", "/v1/evals/schedules", 201, json=body)["id"] == made["id"]  # the same request
            path = f"/v1/evals/schedules/{made['id']}"

            def watched() -> bool:
                watch = call("GET", path, 200)["progress"]["model_watch"]
                return watch is not None and watch["model"] == OLD

            wait_for(watched)
            # The provider changes the model: the live traffic and the target report the new one.
            target.model = NEW
            assert client.portal is not None  # the app's loop, which the worker shares the store on
            client.portal.call(functools.partial(live, store, 0, NEW, base=datetime.now(UTC)))

            def changed() -> list[dict[str, Any]]:
                items: list[dict[str, Any]] = call("GET", f"{path}/runs", 200)["items"]
                return [e for e in items if e["trigger"] == "model_change" and e["state"] == "completed"]

            wait_for(lambda: bool(changed()))
            time.sleep(0.5)  # several more turns of the watch
            entries = call("GET", f"{path}/runs", 200)["items"]
            assert call("POST", f"{path}/pause", 200)["state"] == "paused"
        [entry] = [e for e in entries if e["trigger"] == "model_change"]
        assert (entry["trigger"], entry["model_change"], entry["note"]) == (
            "model_change",
            {"from": OLD, "to": NEW},
            f"model changed from {OLD} to {NEW}",
        )
        assert (entry["model"], entry["counts"]["passed"], entry["missed"]) == (NEW, 3, 0)
    finally:
        queue.close()
        records.close()


@pytest.mark.parametrize("on", ["sqlite", pytest.param("postgres", marks=needs_postgres)])
async def test_the_record_store_keeps_a_schedule_s_model_watch_and_its_noted_changes(
    on: str, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    if on == "sqlite":
        found = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    else:
        found = SqlRecordStore(request.getfixturevalue("owner_url"), schema=request.getfixturevalue("schema"))
    try:
        settings = ModelWatch(source="runs", settle_seconds=30, min_interval_seconds=600)
        schedule = Schedule(
            id="sc1",
            project_id=DEFAULT_PROJECT,
            name="Nightly",
            cron="0 2 * * *",
            on_model_change=settings,
            suite_id="suite-1",
            suite_version=1,
            scorers=[builtin_scorer("contains_expected")],
            target=Target(kind="agent", agent_id=AGENT),
            dataset_name="regression",
            dataset_id="d1",
            max_cases=10,
        )
        assert (await found.create_schedule(SINGLE_USER, DEFAULT_PROJECT, schedule)).on_model_change == settings
        change = ModelChange(previous=OLD, current=NEW, at=T0 + timedelta(microseconds=123456))
        watch = ModelWatchProgress(model=OLD, candidate=NEW, candidate_since=T0, change=change, observed=4)
        await found.set_schedule_progress(SINGLE_USER, DEFAULT_PROJECT, "sc1", ScheduleProgress(model_watch=watch))
        progress = await found.schedule_progress(SINGLE_USER, DEFAULT_PROJECT, "sc1")
        assert progress is not None and progress.model_watch == watch
        entry = ScheduledRun(
            schedule_id="sc1",
            slot=change.at,
            state="admitted",
            run_id=scheduled_run_id("sc1", change.at),
            trigger="model_change",
            model_change=change,
        )
        await found.add_scheduled_run(SINGLE_USER, DEFAULT_PROJECT, entry)
        read = await found.get_scheduled_run(SINGLE_USER, DEFAULT_PROJECT, "sc1", change.at)
        assert read is not None and (read.trigger, read.model_change) == ("model_change", change)
        gated = await found.set_scheduled_run_gate(SINGLE_USER, DEFAULT_PROJECT, "sc1", change.at, "passed")
        assert gated is not None and (gated.gate, gated.model_change) == ("passed", change)
        with pytest.raises(ValidationError, match="names the change"):
            ScheduledRun(schedule_id="sc1", slot=T0, state="skipped", trigger="model_change")
    finally:
        found.close()
