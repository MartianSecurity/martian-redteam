"""Monitors: judges and checks that score a sample of a project's live traces
(MorningHQ/martian-notes#196).

A monitor is created through its routes, which need the ``monitoring`` entitlement, and scored by
the durable queue's worker in passes over the project's new traces, read by time only. New traces
are scored within the stated delay; sampling is fixed by trace id and keeps its rate; the daily
judge token cap stops scoring for the rest of the day and says so; a trace is never scored twice
by one monitor; pausing and deleting stop it. A fake judge stands in for the model; nothing calls a
model or a network target.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, update

import martian.evals.worker.monitor as monitor_module
from martian.evals.judge_settings import MemoryJudgeSettings
from martian.evals.model import Project, Run, Score, ScorerVersion, Span, Trace, TraceQuery
from martian.evals.records import Monitor, MonitorFilter
from martian.evals.records.queue import SqlWorkQueue
from martian.evals.records.sql import SqlRecordStore
from martian.evals.records.tables import work_queue
from martian.evals.server import COMMUNITY, DEFAULT_PROJECT, ENTERPRISE, SINGLE_USER, Principal, create_app
from martian.evals.server.principal import organisation_principal
from martian.evals.store import EvalStore, MemoryStore
from martian.evals.store.mlflow_local import MlflowStore
from martian.evals.worker import InProcessWorker, JudgeRequest, JudgeResult, QueueWorker, Verdict, Work
from martian.evals.worker.monitor import MONITOR_KEY, PASS_FAILED, execute_monitor, sampled
from martian.evals.worker.queue import MONITOR_STOPPED_TOO_OFTEN, entry_id
from martian.evals.worker.scorers import builtin_scorer
from tests.evals import test_records

needs_postgres = test_records.needs_postgres
schema, owner_url = test_records.schema, test_records.owner_url  # the record tests' fixtures
DATABASES = ["sqlite", pytest.param("postgres", marks=needs_postgres)]

# The settings of the served worker: a trace is read SETTLE seconds after its request time, a
# monitor's next pass is due EVERY seconds after its last, and an idle worker looks at the queue
# every POLL seconds. The delay is at most their sum and the time a pass takes.
SETTLE, EVERY, POLL = 0.5, 0.5, 0.05
SECONDS = 30
SAFETY, LENGTH = builtin_scorer("safety"), builtin_scorer("response_length")
JUDGED = {"scorers": ["safety", "response_length"], "acknowledge_model_cost": True, "max_judge_tokens_per_day": 10_000}
MEMBER = organisation_principal("member-1", "org-1", frozenset({"member"}))
VIEWER = organisation_principal("viewer-1", "org-1", frozenset({"viewer"}))


class FakeJudge:
    """Calls the reply safe and reports ``tokens`` used, or none."""

    def __init__(self, tokens: int | None = 15, before: Callable[[JudgeRequest], Any] | None = None) -> None:
        self.tokens = tokens
        self.before = before
        self.calls: list[JudgeRequest] = []

    async def __call__(self, request: JudgeRequest) -> JudgeResult:
        self.calls.append(request)
        if self.before is not None:
            await self.before(request)
        return JudgeResult(
            model=request.model or "fake-judge@1",
            judge_version="fake-v1",
            verdicts={s.name: Verdict(True, "No harm in it.") for s in request.scorers},
            tokens=self.tokens,
        )


def trace(number: int, at: datetime, **fields: Any) -> Trace:
    start = at.timestamp() * 1000
    span = Span(id=f"{number:016x}", name="chat", kind="CHAT_MODEL", status="ok", start_ms=start, end_ms=start + 120)
    values: dict[str, Any] = {
        "id": "tr-" + uuid.uuid5(uuid.NAMESPACE_URL, f"monitor-trace-{number}").hex,
        "project_id": DEFAULT_PROJECT,
        "name": "support turn",
        "state": "ok",
        "request_time": at,
        "latency_ms": 120,
        "request": f"Where is order {number}?",
        "response": f"Order {number} ships tomorrow.",
        "spans": [span],
    }
    return Trace(**{**values, **fields})


async def monitor_scores(store: EvalStore, monitor_id: str, trace_id: str) -> list[Score]:
    found = await store.get_trace(SINGLE_USER, DEFAULT_PROJECT, trace_id)
    assert found is not None
    return sorted((s for s in found.scores if s.metadata.get(MONITOR_KEY) == monitor_id), key=lambda s: s.scorer)


@pytest.fixture
def records(tmp_path: Path) -> Iterator[SqlRecordStore]:
    found = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield found
    found.close()


# --- sampling ---------------------------------------------------------------------------------

IDS = ["tr-" + uuid.uuid5(uuid.NAMESPACE_URL, f"sample-{number}").hex for number in range(4000)]


@pytest.mark.parametrize("rate", [0.01, 0.1, 0.25, 0.5, 0.9])
def test_sampling_is_fixed_by_trace_id_and_keeps_its_rate_on_a_fixed_set(rate: float) -> None:
    chosen = [trace_id for trace_id in IDS if sampled(trace_id, rate)]
    # Within four standard deviations of the rate, over 4,000 fixed trace ids.
    expected, sigma = len(IDS) * rate, math.sqrt(len(IDS) * rate * (1 - rate))
    assert abs(len(chosen) - expected) <= 4 * sigma, (len(chosen), expected)
    # The same ids every time, and a trace in at one rate is in at every higher one.
    assert chosen == [trace_id for trace_id in IDS if sampled(trace_id, rate)]
    assert set(chosen) <= {trace_id for trace_id in IDS if sampled(trace_id, min(1.0, rate * 2))}


def test_a_rate_of_zero_scores_nothing_and_one_everything() -> None:
    assert not any(sampled(trace_id, 0) for trace_id in IDS)
    assert all(sampled(trace_id, 1) for trace_id in IDS)


# --- one pass, called directly ------------------------------------------------------------------


async def make_monitor(
    store: EvalStore,
    records: SqlRecordStore,
    *,
    since: datetime,
    rate: float = 1.0,
    scorers: list[Any] | None = None,
    cap: int | None = None,
    found: MonitorFilter | None = None,
) -> Work:
    if await store.get_project(SINGLE_USER, DEFAULT_PROJECT) is None:
        await store.create_project(SINGLE_USER, Project(id=DEFAULT_PROJECT, name="Default"))
    await records.create_monitor(
        SINGLE_USER,
        DEFAULT_PROJECT,
        Monitor(
            id="m1",
            project_id=DEFAULT_PROJECT,
            name="Live safety",
            scorers=scorers or [LENGTH, SAFETY],
            sampling_rate=rate,
            filter=found or MonitorFilter(),
            max_judge_tokens_per_day=cap,
            active_since=since,
            created_at=since,
        ),
    )
    return Work(kind="monitor", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, monitor_id="m1")


async def taken(records: SqlRecordStore) -> dict[str, str]:
    page = await records.list_monitored_traces(SINGLE_USER, DEFAULT_PROJECT, "m1", limit=1000)
    return {t.trace_id: t.state for t in page.items}


async def test_a_monitor_scores_the_same_sample_on_every_pass_and_each_trace_once(records: SqlRecordStore) -> None:
    store, judge = MemoryStore(), FakeJudge()
    since = datetime.now(UTC) - timedelta(hours=1)
    work = await make_monitor(store, records, since=since, rate=0.3, cap=1_000_000)
    traces = [await store.record_trace(SINGLE_USER, trace(n, since + timedelta(seconds=n))) for n in range(200)]
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    chosen = {t.id for t in traces if sampled(t.id, 0.3)}
    assert 40 <= len(chosen) <= 80
    assert await taken(records) == dict.fromkeys(chosen, "scored")
    assert len(judge.calls) == len(chosen)
    for found in traces:
        scores = await monitor_scores(store, "m1", found.id)
        if found.id in chosen:
            assert [(s.scorer, s.scorer_version, s.source) for s in scores] == [
                ("response_length", "v1", "code"),
                ("safety", "v1", "llm_judge"),
            ]
            assert scores[0].value == len(found.response) and scores[1].value is True
        else:
            assert scores == []
    progress = await records.monitor_progress(SINGLE_USER, DEFAULT_PROJECT, "m1")
    assert progress is not None
    assert (progress.examined, progress.sampled, progress.scored, progress.errors) == (
        200,
        len(chosen),
        2 * len(chosen),
        0,
    )
    assert progress.judge_tokens == 15 * len(chosen) and progress.caught_up

    # Read again from the start: the same sample, and nothing is scored a second time.
    await records.set_monitor_progress(
        SINGLE_USER, DEFAULT_PROJECT, "m1", progress.model_copy(update={"watermark": None})
    )
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    assert len(judge.calls) == len(chosen)
    assert await taken(records) == dict.fromkeys(chosen, "scored")
    for trace_id in chosen:
        assert len(await monitor_scores(store, "m1", trace_id)) == 2


async def test_a_monitor_scores_only_the_traces_its_filter_matches(records: SqlRecordStore) -> None:
    store, judge = MemoryStore(), FakeJudge()
    since = datetime.now(UTC) - timedelta(hours=1)
    found = MonitorFilter(state="ERROR", min_latency_ms=100, user_id="u-1")
    work = await make_monitor(store, records, since=since, cap=1000, found=found)
    await store.start_run(SINGLE_USER, Run(id="r1", project_id=DEFAULT_PROJECT, source="local", state="running"))
    at = since + timedelta(minutes=1)
    wanted = await store.record_trace(SINGLE_USER, trace(1, at, state="error", user_id="u-1"))
    for number, fields in enumerate(
        [
            {"state": "ok", "user_id": "u-1"},  # not an error
            {"state": "error", "user_id": "u-2"},  # another user
            {"state": "error", "user_id": "u-1", "latency_ms": 50},  # too fast
            {"state": "error", "user_id": "u-1", "run_id": "r1"},  # an eval run's trace
            {"state": "in_progress", "user_id": "u-1"},  # not finished
        ],
        start=2,
    ):
        await store.record_trace(SINGLE_USER, trace(number, at, **fields))
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    assert await taken(records) == {wanted.id: "scored"}


async def test_a_pass_reads_new_traces_by_time_only_and_a_bounded_number_at_a_time(records: SqlRecordStore) -> None:
    class Watched(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.queries: list[TraceQuery] = []

        async def search_traces(self, principal: Principal, query: TraceQuery) -> Any:
            self.queries.append(query)
            return await super().search_traces(principal, query)

    store, judge = Watched(), FakeJudge()
    since = datetime.now(UTC) - timedelta(hours=1)
    work = await make_monitor(store, records, since=since, cap=1_000_000)
    # Traffic from before the monitor existed is never read.
    old = [await store.record_trace(SINGLE_USER, trace(n, since - timedelta(minutes=n + 1))) for n in range(50)]
    new = [await store.record_trace(SINGLE_USER, trace(100 + n, since + timedelta(seconds=n))) for n in range(250)]
    passes = 0
    while True:
        await execute_monitor(store, records, work, judge=judge, settle_seconds=0, read_bound=100)
        passes += 1
        progress = await records.monitor_progress(SINGLE_USER, DEFAULT_PROJECT, "m1")
        assert progress is not None
        if progress.caught_up:
            break
    assert passes == 3
    assert set(await taken(records)) == {t.id for t in new}
    assert not set(await taken(records)) & {t.id for t in old}
    assert store.queries and all(
        q.since is not None and q.since >= since and q.until is not None and q.oldest_first for q in store.queries
    )


async def test_the_daily_cap_stops_scoring_for_the_rest_of_the_day_and_says_so(
    records: SqlRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, judge = MemoryStore(), FakeJudge(tokens=15)
    now = datetime.now(UTC)
    since = now - timedelta(hours=1)
    work = await make_monitor(store, records, since=since, scorers=[SAFETY], cap=40)
    traces = [await store.record_trace(SINGLE_USER, trace(n, since + timedelta(seconds=n))) for n in range(6)]
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    # 15, 30, then 45 tokens: the third reaches the cap of 40, and the rest are not scored.
    assert list(await taken(records)) == [t.id for t in traces[:3]][::-1]
    assert len(judge.calls) == 3
    progress = await records.monitor_progress(SINGLE_USER, DEFAULT_PROJECT, "m1")
    assert progress is not None and progress.capped is not None and progress.judge_tokens == 45
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
    assert (progress.capped["limit"], progress.capped["value"], progress.capped["used"]) == (
        "max_judge_tokens_per_day",
        40,
        45,
    )
    assert progress.capped["message"] == (
        f"Stopped until {midnight:%Y-%m-%dT%H:%M:%SZ} at its daily judge token cap "
        "(max_judge_tokens_per_day 40) after 45 tokens; traces until then are not scored"
    )

    # Until the day ends, new traffic is passed over without being read or scored.
    late = await store.record_trace(SINGLE_USER, trace(10, midnight - timedelta(seconds=1)))
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    assert len(judge.calls) == 3

    # The next day it scores again, from that day's traffic, with the day's tokens counted afresh.
    tomorrow = midnight + timedelta(hours=2)
    monkeypatch.setattr(monitor_module, "_now", lambda: tomorrow)
    fresh = await store.record_trace(SINGLE_USER, trace(11, midnight + timedelta(hours=1)))
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    assert [r.prompt for r in judge.calls[3:]] == [fresh.request]
    assert late.id not in await taken(records)
    progress = await records.monitor_progress(SINGLE_USER, DEFAULT_PROJECT, "m1")
    assert progress is not None
    assert (progress.capped, progress.judge_tokens, progress.day) == (None, 15, tomorrow.date())


async def test_a_cap_the_judge_reports_no_tokens_for_stops_scoring_and_says_so(records: SqlRecordStore) -> None:
    store, judge = MemoryStore(), FakeJudge(tokens=None)
    since = datetime.now(UTC) - timedelta(hours=1)
    work = await make_monitor(store, records, since=since, scorers=[SAFETY], cap=1000)
    for number in range(3):
        await store.record_trace(SINGLE_USER, trace(number, since + timedelta(seconds=number)))
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    assert len(judge.calls) == 1
    progress = await records.monitor_progress(SINGLE_USER, DEFAULT_PROJECT, "m1")
    assert progress is not None and progress.capped is not None and progress.capped["used"] is None
    assert re.fullmatch(
        r"Stopped until \S+: the judge reported no token usage, so the daily judge token cap "
        r"\(max_judge_tokens_per_day 1000\) cannot be kept; traces until then are not scored",
        progress.capped["message"],
    )


async def test_a_trace_a_pass_did_not_finish_is_finished_by_the_next_without_scoring_twice(
    records: SqlRecordStore,
) -> None:
    class Failing(MemoryStore):
        """Fails to store the first judge score, as a database lost part way would."""

        failed = False

        async def log_score(self, principal: Principal, project_id: str, trace_id: str, score: Score) -> Score:
            if score.scorer == "safety" and not self.failed:
                self.failed = True
                raise RuntimeError("the database went away")
            return await super().log_score(principal, project_id, trace_id, score)

    store, judge = Failing(), FakeJudge()
    since = datetime.now(UTC) - timedelta(hours=1)
    work = await make_monitor(store, records, since=since, cap=1000)
    found = await store.record_trace(SINGLE_USER, trace(1, since + timedelta(seconds=1)))
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    progress = await records.monitor_progress(SINGLE_USER, DEFAULT_PROJECT, "m1")
    assert progress is not None and progress.error == PASS_FAILED
    assert await taken(records) == {found.id: "scoring"}
    assert [s.scorer for s in await monitor_scores(store, "m1", found.id)] == ["response_length"]

    # The next pass takes the trace over and stores only the score that is missing.
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    assert [[s.name for s in r.scorers] for r in judge.calls] == [["safety"], ["safety"]]
    assert [s.scorer for s in await monitor_scores(store, "m1", found.id)] == ["response_length", "safety"]
    assert await taken(records) == {found.id: "scored"}
    progress = await records.monitor_progress(SINGLE_USER, DEFAULT_PROJECT, "m1")
    assert progress is not None and progress.error == ""


async def test_pausing_a_monitor_stops_its_pass_before_the_next_trace(records: SqlRecordStore) -> None:
    async def pause(_request: JudgeRequest) -> None:
        current = await records.get_monitor(SINGLE_USER, DEFAULT_PROJECT, "m1")
        assert current is not None
        paused = current.model_copy(update={"state": "paused"})
        await records.update_monitor(SINGLE_USER, DEFAULT_PROJECT, paused, expected_revision=current.revision)

    store, judge = MemoryStore(), FakeJudge(before=pause)
    since = datetime.now(UTC) - timedelta(hours=1)
    work = await make_monitor(store, records, since=since, cap=1000)
    for number in range(5):
        await store.record_trace(SINGLE_USER, trace(number, since + timedelta(seconds=number)))
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    await execute_monitor(store, records, work, judge=judge, settle_seconds=0)
    assert len(judge.calls) == 1 and len(await taken(records)) == 1


# --- through the routes and the durable queue ----------------------------------------------------


@pytest.fixture(params=DATABASES)
def database(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[str, str]]:
    """A database URL and the schema the stores keep their tables in."""
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'martian.db'}", "martian"
    else:
        yield request.getfixturevalue("owner_url"), request.getfixturevalue("schema")


class Core:
    """The core with a queue whose worker runs in the app's process, as the local edition's does."""

    def __init__(
        self, store: EvalStore, records: SqlRecordStore, queue: SqlWorkQueue, judge: FakeJudge, entitlements: Any
    ) -> None:
        self.store, self.records, self.queue, self.judge = store, records, queue, judge
        self.principal: dict[str, Principal] = {"current": SINGLE_USER}
        runner = InProcessWorker(store, records, judge=judge, monitor_settle_seconds=SETTLE)
        self.worker = QueueWorker(queue, runner, records, in_process=True, poll_seconds=POLL, monitor_seconds=EVERY)
        app = create_app(
            store=store,
            records=records,
            principal=lambda: self.principal["current"],
            worker=self.worker,
            entitlements=entitlements,
            judge_settings=MemoryJudgeSettings(),
        )
        self.client = TestClient(app)

    def call(self, method: str, path: str, status: int, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        assert response.status_code == status, response.text
        return response.json() if response.content else None

    def run(self, function: Callable[..., Any], *args: Any) -> Any:
        assert self.client.portal is not None
        return self.client.portal.call(function, *args)

    def record(self, number: int) -> Trace:
        return self.run(self.store.record_trace, SINGLE_USER, trace(number, datetime.now(UTC)))

    def scores(self, monitor_id: str, trace_id: str) -> list[Score]:
        return self.run(monitor_scores, self.store, monitor_id, trace_id)

    def until(self, check: Callable[[], bool], seconds: float = SECONDS) -> datetime:
        """Wait for ``check`` to hold, and return when it did."""
        started = time.monotonic()
        while not check():
            if time.monotonic() - started > seconds:
                raise TimeoutError("the condition never held")
            time.sleep(0.02)
        return datetime.now(UTC)


def create(core: Core, status: int = 201, **body: Any) -> Any:
    return core.call("POST", "/v1/evals/monitors", status, json={"request_id": str(uuid.uuid4()), **body})


@pytest.fixture
def queue(records: SqlRecordStore, tmp_path: Path) -> Iterator[SqlWorkQueue]:
    found = SqlWorkQueue(f"sqlite:///{tmp_path / 'martian.db'}")
    yield found
    found.close()


def test_monitors_need_the_monitoring_entitlement_and_the_score_capability(
    records: SqlRecordStore, queue: SqlWorkQueue
) -> None:
    editions = {"current": COMMUNITY}
    core = Core(MemoryStore(), records, queue, FakeJudge(), lambda _p: editions["current"])
    with core.client:
        body = {"name": "Live safety", "sampling_rate": 0.1, **JUDGED}
        refused = "The Community edition does not include monitoring"
        assert core.call("GET", "/v1/evals/monitors", 403)["detail"] == refused
        assert create(core, 403, **body)["detail"] == refused
        editions["current"] = ENTERPRISE
        core.principal["current"] = VIEWER
        assert create(core, 403, **body)["detail"] == "Requires member role"
        assert core.call("GET", "/v1/evals/monitors", 200) == {"items": []}
        core.principal["current"] = MEMBER
        made = create(core, **body)
        assert (made["state"], made["sampling_rate"], made["max_judge_tokens_per_day"]) == ("active", 0.1, 10_000)
        assert [(s["name"], s["version"]) for s in made["scorers"]] == [("response_length", "v1"), ("safety", "v1")]
        core.principal["current"] = VIEWER
        assert core.call("GET", f"/v1/evals/monitors/{made['id']}", 200)["id"] == made["id"]
        core.call("POST", f"/v1/evals/monitors/{made['id']}/pause", 403)
        core.call("DELETE", f"/v1/evals/monitors/{made['id']}", 403)


def test_a_monitor_refuses_what_it_cannot_score(records: SqlRecordStore, queue: SqlWorkQueue) -> None:
    core = Core(MemoryStore(), records, queue, FakeJudge(), lambda _p: ENTERPRISE)
    with core.client:
        base = {"name": "Live", "sampling_rate": 0.5}
        for body, detail in [
            ({"scorers": ["correctness"], "acknowledge_model_cost": True, "max_judge_tokens_per_day": 9}, "expected"),
            ({"scorers": ["exact_match"]}, "expected"),
            ({"scorers": ["safety"], "acknowledge_model_cost": True}, "daily judge token cap"),
            ({"scorers": ["safety"], "max_judge_tokens_per_day": 9}, "cost acknowledgement"),
            ({"scorers": ["valid_json"], "sampling_rate": 1.5}, "less than or equal to 1"),
            ({"scorers": ["valid_json"], "filter": {"state": "IN_PROGRESS"}}, "'OK' or 'ERROR'"),
            ({"scorers": ["valid_json"], "filter": {"order": "slowest"}}, "Extra inputs"),
        ]:
            assert detail in str(create(core, 422, **{**base, **body})["detail"]), body
        # The same request again is the same monitor; its id with another request is refused.
        request_id = str(uuid.uuid4())
        first = create(core, request_id=request_id, **base, scorers=["valid_json"])
        assert create(core, request_id=request_id, **base, scorers=["valid_json"])["id"] == first["id"]
        create(core, 409, request_id=request_id, **{**base, "sampling_rate": 0.2}, scorers=["valid_json"])
    # A worker that does not run monitors cannot take one.
    app = create_app(
        store=MemoryStore(),
        records=records,
        principal=lambda: SINGLE_USER,
        worker=InProcessWorker(MemoryStore(), records),
        entitlements=lambda _p: ENTERPRISE,
    )
    with TestClient(app) as client:
        refused = client.post(
            "/v1/evals/monitors", json={"request_id": str(uuid.uuid4()), **base, "scorers": ["valid_json"]}
        )
        assert (refused.status_code, refused.json()["detail"]) == (
            503,
            "This installation's worker does not run monitors",
        )


def test_a_monitor_holds_no_custom_python_scorer(records: SqlRecordStore, queue: SqlWorkQueue) -> None:
    # A custom scorer reads a case's inputs and expectations, which a production trace does not
    # have, so a monitor refuses one in its record, whoever writes it, and its route names none.
    custom = ScorerVersion(name="names_order", kind="code", type="python")
    with pytest.raises(ValidationError, match="custom Python scorer names_order"):
        Monitor(id="m1", project_id=DEFAULT_PROJECT, name="Live", scorers=[LENGTH, custom], sampling_rate=1.0)
    core = Core(MemoryStore(), records, queue, FakeJudge(), lambda _p: ENTERPRISE)
    with core.client:
        create(core, 422, name="Live", sampling_rate=0.5, scorers=["names_order"])
        assert core.call("GET", "/v1/evals/monitors", 200)["items"] == []


def test_new_traces_are_scored_within_the_stated_delay(
    records: SqlRecordStore, queue: SqlWorkQueue, tmp_path: Path
) -> None:
    store = MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", str(tmp_path / "artifacts"))
    core = Core(store, records, queue, FakeJudge(), lambda _p: ENTERPRISE)
    with core.client:
        made = create(core, name="Live safety", sampling_rate=1, **JUDGED)
        delays = []
        for number in range(3):
            recorded = core.record(number)
            scored = core.until(lambda trace_id=recorded.id: len(core.scores(made["id"], trace_id)) == 2)
            assert recorded.request_time is not None
            delays.append((scored - recorded.request_time).total_seconds())
        length, safety = core.scores(made["id"], recorded.id)
        progress = core.call("GET", f"/v1/evals/monitors/{made['id']}", 200)["progress"]
    # Each is scored no sooner than it settles, and within its settling time, the time to its
    # monitor's next pass and the worker's look at the queue after its request time, plus the
    # time a pass takes.
    print(f"scored {', '.join(f'{d:.2f}' for d in delays)} s after the request time")  # noqa: T201
    assert all(SETTLE <= delay <= SETTLE + EVERY + POLL + 1.5 for delay in delays), delays
    # As assessments on the trace, naming the scorer, its version and the monitor.
    assert (safety.scorer, safety.scorer_version, safety.source, safety.value) == ("safety", "v1", "llm_judge", True)
    assert safety.source_id == "fake-judge@1" and safety.rationale == "No harm in it."
    assert safety.metadata[MONITOR_KEY] == made["id"] and safety.metadata["martian.judge.version"] == "fake-v1"
    assert safety.store.assessment_id is not None
    assert (length.scorer, length.scorer_version, length.source) == ("response_length", "v1", "code")
    assert length.value == len(recorded.response) and length.source_id == "martian.response_length:v1"
    assert (progress["sampled"], progress["scored"], progress["errors"]) == (3, 6, 0)
    assert progress["judge_tokens"] == 45 and progress["capped"] is None


def test_pausing_resuming_and_deleting_a_monitor_through_the_queue(database: tuple[str, str]) -> None:
    url, in_schema = database
    records, queue = SqlRecordStore(url, schema=in_schema), SqlWorkQueue(url, schema=in_schema)
    core = Core(MemoryStore(), records, queue, FakeJudge(), lambda _p: ENTERPRISE)
    try:
        with core.client:
            made = create(core, name="Live safety", sampling_rate=1, **JUDGED)
            path = f"/v1/evals/monitors/{made['id']}"
            work = Work(kind="monitor", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, monitor_id=made["id"])
            first = core.record(1)
            core.until(lambda: len(core.scores(made["id"], first.id)) == 2)

            assert core.call("POST", f"{path}/pause", 200)["state"] == "paused"
            during = core.record(2)
            time.sleep(SETTLE + 2 * EVERY + 0.5)  # two passes' worth: nothing is scored while paused
            assert core.scores(made["id"], during.id) == []

            resumed = core.call("POST", f"{path}/resume", 200)
            assert resumed["state"] == "active" and resumed["revision"] == 3
            after = core.record(3)
            core.until(lambda: len(core.scores(made["id"], after.id)) == 2)
            assert core.scores(made["id"], during.id) == []  # recorded while it was paused

            core.call("DELETE", path, 204)
            core.call("GET", path, 404)
            core.until(lambda: not core.run(queue.contains, SINGLE_USER, DEFAULT_PROJECT, entry_id(work)))
            gone = core.record(4)
            time.sleep(SETTLE + 2 * EVERY + 0.5)
            assert core.scores(made["id"], gone.id) == []
            # The scores it stored stay on their traces.
            assert len(core.scores(made["id"], first.id)) == 2
    finally:
        queue.close()
        records.close()


async def test_a_monitor_whose_worker_stops_during_its_pass_too_often_is_paused(
    records: SqlRecordStore, queue: SqlWorkQueue, tmp_path: Path
) -> None:
    store = MemoryStore()
    work = await make_monitor(store, records, since=datetime.now(UTC), cap=1000)
    runner = InProcessWorker(store, records, judge=FakeJudge())
    worker = QueueWorker(queue, runner, records, poll_seconds=POLL, monitor_seconds=EVERY, max_attempts=3)
    await worker.submit(work)
    # Three workers took it and stopped before giving it back.
    engine = create_engine(f"sqlite:///{tmp_path / 'martian.db'}")
    with engine.begin() as connection:
        connection.execute(update(work_queue).where(work_queue.c.id == entry_id(work)).values(attempts=3))
    engine.dispose()
    serving = asyncio.get_running_loop().create_task(worker.serve())
    try:
        deadline = time.monotonic() + SECONDS
        while (
            found := await records.get_monitor(SINGLE_USER, DEFAULT_PROJECT, "m1")
        ) is None or found.state == "active":
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
    progress = await records.monitor_progress(SINGLE_USER, DEFAULT_PROJECT, "m1")
    assert progress is not None and progress.error == MONITOR_STOPPED_TOO_OFTEN
    # Its row stays, for when it is resumed.
    assert await queue.contains(SINGLE_USER, DEFAULT_PROJECT, entry_id(work))
