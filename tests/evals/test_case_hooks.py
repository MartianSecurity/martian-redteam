"""Per-case hooks and installed checks in the worker (MartianSecurity/martian-notes#216).

A test edition installs, on the worker, hooks that prepare each case and clean it up, and checks it
grades with by name. Here the hooks open a ledger for each case, which the agent under test writes
to, and an installed check grades the case by what the ledger holds while it is still open. The
hooks run around every case in order, also when the target raises or times out; a hook's failure
is the case's error; the expectations never reach a hook or the target; and a suite file names an
installed check by its name and options only, which ``martian evals run`` refuses to run.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import mlflow
import pytest
import yaml
from typer.testing import CliRunner

import martian.evals.worker.schedule as schedule_module
from martian.cli import app
from martian.config import ConfigError
from martian.evals.audit import INSTALLED, audit_saved_suite, audit_suite
from martian.evals.model import Case, Project, Run, ScorerVersion, ScoringJob, Suite, Target, Trace, Turn, suite_digest
from martian.evals.records import Monitor, Schedule
from martian.evals.records.sql import SqlRecordStore
from martian.evals.runner import run_suite
from martian.evals.server import COMMUNITY, DEFAULT_PROJECT, SINGLE_USER, Entitlements
from martian.evals.server.entitlements import PAID
from martian.evals.server.launch import admit_run
from martian.evals.state import STATE
from martian.evals.store import EvalStore, MemoryStore
from martian.evals.suite_file import load_suite_file, suite_from_yaml, suite_to_yaml
from martian.evals.worker import CaseContext, InProcessWorker, ProcessWorker, ScheduleServices, Work, Worker
from martian.evals.worker.execute import CHECK_NOT_INSTALLED, CONVERSATION
from martian.evals.worker.hooks import INPUT_TAKEN, installed_check
from martian.evals.worker.schedule import SCHEDULE_KEY, after_scheduled_run, execute_schedule, suite_job_id
from martian.evals.worker.scorers import builtin_scorer
from tests.evals.process_fixture import HOOK_LOG, cases, hooked_services, member, open_stores
from tests.evals.write_fixture import AGENT


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


# --- the test edition -------------------------------------------------------------------------

LEDGER_CASES = [
    Case(id="right", inputs={"message": "Record A-1"}, expectations={"expected_response": "Done.", "entry": "A-1"}),
    Case(id="wrong", inputs={"message": "Record B-2"}, expectations={"expected_response": "Done.", "entry": "B-9"}),
    Case(id="raises", inputs={"message": "Break"}, expectations={"expected_response": "Done.", "entry": "C-3"}),
    Case(id="slow", inputs={"message": "Wait"}, expectations={"expected_response": "Done.", "entry": "D-4"}),
    Case(id="unusable", inputs={"message": "Record E-5"}),  # no expected text: the run skips it
]


class Edition:
    """A test edition's hooks, agent and checks. ``first`` opens each case's ledger; ``second``
    only records its steps. Every step goes to ``events``, in the order it happened."""

    def __init__(self, *, hooks: tuple[str, ...] = ("first", "second"), fail: dict[str, str] | None = None) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.ledgers: dict[str, list[str]] = {}
        self.hooked: list[Case] = []
        self.sent: list[Case] = []
        self.started = threading.Event()
        self.fail = fail or {}  # a hook step to fail, by case id: "setup" or "teardown" of `second`
        self.hooks = [Hook(self, name) for name in hooks]

    async def agent(self, case: Case) -> str:
        """The agent under test: writes the order in its message to the ledger its setup gave it."""
        self.sent.append(case)
        self.events.append(("call", case.id))
        self.started.set()
        message = str(case.inputs["message"])
        if message == "Break":
            raise RuntimeError("the agent broke")
        if message in ("Wait", "Hold"):
            await asyncio.sleep(30)
        self.ledgers[case.inputs["ledger"]].append(message.removeprefix("Record "))
        return "Done."

    def ledger_holds(self, state: Any, expectations: dict[str, Any], options: dict[str, Any]) -> tuple[bool, str]:
        """The installed check: the case's ledger, still open, holds its expected entry often enough."""
        lines = self.ledgers[state.values["ledger"]]
        self.events.append(("check", state.values["case"]))
        return lines.count(expectations["entry"]) >= options["at_least"], f"the ledger holds {lines}"

    @staticmethod
    def had_state(state: Any) -> tuple[bool, str]:
        return state is not None, "given the case's state" if state is not None else "given no state"


class Hook:
    def __init__(self, edition: Edition, name: str) -> None:
        self.edition, self.name = edition, name

    async def setup(self, context: CaseContext) -> None:
        edition, case = self.edition, context.case
        edition.events.append(("setup", self.name, case.id))
        edition.hooked.append(case)
        if self.name == "second" and edition.fail.get(case.id) == "setup":
            raise RuntimeError("could not prepare")
        if self.name == "second" and edition.fail.get(case.id) == "taken":
            context.state.target_inputs["message"] = "Something else"
        if self.name == "first":
            key = uuid.uuid4().hex
            edition.ledgers[key] = []
            context.state.target_inputs["ledger"] = key
            context.state.values.update(ledger=key, case=case.id)

    async def teardown(self, context: CaseContext, error: str) -> None:
        edition = self.edition
        edition.events.append(("teardown", self.name, context.case.id, error))
        if self.name == "second" and edition.fail.get(context.case.id) == "teardown":
            raise RuntimeError("could not clean up")
        if self.name == "first":
            del edition.ledgers[context.state.values["ledger"]]


def worker_for(store: EvalStore, records: SqlRecordStore, edition: Edition) -> InProcessWorker:
    return InProcessWorker(
        store,
        records,
        targets={AGENT: edition.agent},
        hooks=edition.hooks,
        checks={"ledger_holds": edition.ledger_holds, "had_state": edition.had_state},
        target_timeout_seconds=0.5,
    )


async def published(cases_: list[Case] = LEDGER_CASES) -> tuple[MemoryStore, Any]:
    store = MemoryStore()
    await store.create_project(SINGLE_USER, Project(id=DEFAULT_PROJECT, name="Default"))
    return store, await store.create_dataset(SINGLE_USER, DEFAULT_PROJECT, "ledger", cases_)


async def admitted(
    store: EvalStore, records: Any, worker: Worker, dataset: Any, checks: list[ScorerVersion], principal=SINGLE_USER
) -> Run:
    """A run of the agent over ``dataset`` that names ``checks``, as an edition's own launch admits one."""
    target = Target(kind="agent", agent_id=AGENT)
    return await admit_run(
        store,
        records,
        worker,
        principal,
        entitlements=COMMUNITY,
        run_id=str(uuid.uuid4()),
        project=DEFAULT_PROJECT,
        target=target,
        pin=await worker.check_target(principal, target),
        dataset=dataset,
        max_cases=100,
        checks=checks,
    )


def steps_of(events: list[tuple[Any, ...]], case_id: str) -> list[tuple[Any, ...]]:
    return [event for event in events if case_id in event]


# --- the worker -------------------------------------------------------------------------------


async def test_a_test_editions_hooks_run_around_every_case_in_order_and_a_named_check_grades_its_state(tmp_path):
    store, dataset = await published()
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    edition = Edition()
    worker = worker_for(store, records, edition)
    try:
        checks = [installed_check("ledger_holds", {"at_least": 1}), installed_check("not_here")]
        run = await admitted(store, records, worker, dataset, checks)
        await worker.drain()
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        results = {r.case_id: r for r in (await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run.id)).items}
        scores = await records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run.id)
        trace = await store.get_trace(SINGLE_USER, DEFAULT_PROJECT, results["right"].store.trace_id or "")
    finally:
        records.close()
    assert finished is not None and finished.state == "completed", finished
    assert finished.scorers[1:] == checks
    # Each case the run called: both setups in order, the call, the check, both teardowns in the
    # opposite order. A failed or timed-out case is torn down too, with its error, and not checked.
    for case_id in ("right", "wrong"):
        assert steps_of(edition.events, case_id) == [
            ("setup", "first", case_id),
            ("setup", "second", case_id),
            ("call", case_id),
            ("check", case_id),
            ("teardown", "second", case_id, ""),
            ("teardown", "first", case_id, ""),
        ]
    for case_id, error in (("raises", "RuntimeError: the agent broke"), ("slow", "item timed out")):
        assert steps_of(edition.events, case_id) == [
            ("setup", "first", case_id),
            ("setup", "second", case_id),
            ("call", case_id),
            ("teardown", "second", case_id, error),
            ("teardown", "first", case_id, error),
        ]
        assert (results[case_id].status, results[case_id].error) == ("error", error)
    # The cases ran one after the other, and the skipped case, which calls no target, was not set up.
    assert [event[2] for event in edition.events if event[0] == "setup"] == [
        case for case in ("right", "wrong", "raises", "slow") for _ in range(2)
    ]
    assert results["unusable"].status == "skipped" and not steps_of(edition.events, "unusable")
    assert edition.ledgers == {}  # every case's ledger was closed
    # Neither a hook nor the target was given the expectations; the target got the ledger.
    assert all(case.expectations == {} and case.outputs is None for case in edition.hooked)
    assert all(set(case.inputs) == {"message", "ledger"} and case.expectations == {} for case in edition.sent)
    # The named check graded each answered case from its open ledger, beside exact match.
    graded = {(s.case_id, s.scorer): s for s in scores}
    assert set(graded) == {(c, s) for c in ("right", "wrong") for s in ("exact_match", "ledger_holds", "not_here")}
    right, wrong = graded[("right", "ledger_holds")], graded[("wrong", "ledger_holds")]
    assert (right.value, right.passed, right.rationale, right.source_id) == (
        True,
        True,
        "the ledger holds ['A-1']",
        "installed:ledger_holds",
    )
    assert (wrong.value, wrong.rationale, wrong.error) == (False, "the ledger holds ['B-2']", "")
    assert graded[("right", "not_here")].error == CHECK_NOT_INSTALLED.format(name="not_here")
    assert results["right"].status == results["wrong"].status == "passed"  # exact match decides the status
    # The check's value is on the case's trace too.
    assert trace is not None and {score.scorer for score in trace.scores} == {"exact_match", "ledger_holds"}


async def test_a_failed_setup_or_teardown_is_the_cases_error_and_the_other_hooks_still_tear_down(tmp_path):
    third = Case(id="third", inputs={"message": "Record F-6"}, expectations={"expected_response": "Done."})
    store, dataset = await published([*LEDGER_CASES[:2], third])
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    edition = Edition(fail={"right": "setup", "wrong": "teardown", "third": "taken"})
    worker = worker_for(store, records, edition)
    try:
        run = await admitted(store, records, worker, dataset, [installed_check("ledger_holds", {"at_least": 1})])
        await worker.drain()
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        results = {r.case_id: r for r in (await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run.id)).items}
        scores = await records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        records.close()
    assert finished is not None and finished.state == "completed", finished
    setup_failed = "The case's setup failed: RuntimeError: could not prepare"
    # A setup that raised: no call, no trace, no score; the hook set up before it is torn down, and
    # the one that raised is not.
    assert steps_of(edition.events, "right") == [
        ("setup", "first", "right"),
        ("setup", "second", "right"),
        ("teardown", "first", "right", setup_failed),
    ]
    assert (results["right"].status, results["right"].error, results["right"].store.trace_id) == (
        "error",
        setup_failed,
        None,
    )
    # A teardown that raised: the case answered and was graded, the other hook still tore down, and
    # the case is an error.
    assert steps_of(edition.events, "wrong")[-2:] == [
        ("teardown", "second", "wrong", ""),
        ("teardown", "first", "wrong", ""),
    ]
    assert (results["wrong"].status, results["wrong"].error) == (
        "error",
        "The case's teardown failed: RuntimeError: could not clean up",
    )
    assert results["wrong"].output == "Done." and results["wrong"].store.trace_id
    # A setup that gave the target an input the case already has: no call, and every hook tears down.
    taken = INPUT_TAKEN.format(names="message")
    assert steps_of(edition.events, "third")[2:] == [
        ("teardown", "second", "third", taken),
        ("teardown", "first", "third", taken),
    ]
    assert (results["third"].status, results["third"].error) == ("error", taken)
    assert [(s.case_id, s.scorer) for s in scores if s.scorer == "ledger_holds"] == [("wrong", "ledger_holds")]
    assert edition.ledgers == {}


async def test_a_conversation_case_is_skipped_before_any_hook_or_installed_check(tmp_path):
    talk = Case(id="talk", turns=[Turn(inputs={"message": "Record G-7"}), Turn(inputs={"message": "Record H-8"})])
    store, dataset = await published([LEDGER_CASES[0], talk])
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    edition = Edition()
    worker = worker_for(store, records, edition)
    try:
        run = await admitted(store, records, worker, dataset, [installed_check("ledger_holds", {"at_least": 1})])
        await worker.drain()
        results = {r.case_id: r for r in (await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run.id)).items}
        scores = await records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        records.close()
    # The worker runs no conversation, so the case is refused with its reason before any hook.
    assert (results["talk"].status, results["talk"].error) == ("skipped", CONVERSATION)
    assert not steps_of(edition.events, "talk") and {s.case_id for s in scores} == {"right"}
    assert results["right"].status == "passed" and edition.ledgers == {}


def test_a_monitor_refuses_an_installed_check():
    with pytest.raises(ValueError, match="A monitor cannot run the custom Python scorer ledger_holds"):
        Monitor(id="m1", project_id="p1", name="Live", scorers=[installed_check("ledger_holds")], sampling_rate=1)


class Holding:
    """A worker that admits the run and keeps its work, for the test to run."""

    def __init__(self, worker: InProcessWorker) -> None:
        self.worker, self.works = worker, []  # type: ignore[var-annotated]

    @property
    def judges(self) -> bool:
        return False

    async def check_target(self, principal: Any, target: Target) -> dict[str, Any]:
        return await self.worker.check_target(principal, target)

    async def submit(self, work: Work) -> None:
        self.works.append(work)


async def test_work_cancelled_during_a_case_tears_the_case_down(tmp_path):
    hold = Case(id="hold", inputs={"message": "Hold"}, expectations={"expected_response": "Done."})
    store, dataset = await published([hold])
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    edition = Edition()
    worker = InProcessWorker(store, records, targets={AGENT: edition.agent}, hooks=edition.hooks)
    holding = Holding(worker)
    try:
        await admitted(store, records, holding, dataset, [])
        running = asyncio.create_task(worker.run(holding.works[0]))
        assert await asyncio.to_thread(edition.started.wait, 30)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        records.close()
    assert steps_of(edition.events, "hold") == [
        ("setup", "first", "hold"),
        ("setup", "second", "hold"),
        ("call", "hold"),
        ("teardown", "second", "hold", ""),
        ("teardown", "first", "hold", ""),
    ]
    assert edition.ledgers == {}


async def test_a_scoring_job_runs_an_installed_check_with_no_state(tmp_path):
    store, dataset = await published(LEDGER_CASES[:1])
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    edition = Edition()
    worker = worker_for(store, records, edition)
    try:
        run = await admitted(store, records, worker, dataset, [installed_check("had_state")])
        await worker.drain()
        job = ScoringJob(
            id=str(uuid.uuid4()),
            run_id=run.id,
            scorers=[installed_check("had_state"), installed_check("not_here")],
            state="queued",
            requested_by=SINGLE_USER.user_id,
            created_at=datetime.now(UTC),
        )
        await records.create_scoring_job(SINGLE_USER, DEFAULT_PROJECT, job)
        await worker.submit(Work("scoring", SINGLE_USER, DEFAULT_PROJECT, run.id, job_id=job.id))
        await worker.drain()
        scores = await records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        records.close()
    by = {(s.scoring_job_id, s.scorer): (s.value, s.rationale, s.error) for s in scores}
    assert by[(None, "had_state")] == (True, "given the case's state", "")  # in the run, with its state
    assert by[(job.id, "had_state")] == (False, "given no state", "")  # in the job, after its teardown
    assert by[(job.id, "not_here")] == (None, "", CHECK_NOT_INSTALLED.format(name="not_here"))
    # The scoring job set nothing up: the hooks ran only around the run's case.
    assert [event[0] for event in edition.events].count("setup") == 2


def test_a_worker_refuses_a_check_it_could_not_call_and_a_hook_without_both_steps(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    try:
        with pytest.raises(ValueError, match="takes trace, which a scorer is not given; it takes inputs, outputs"):
            InProcessWorker(MemoryStore(), records, checks={"traced": lambda outputs, trace: True})

        class SetupOnly:
            async def setup(self, context: CaseContext) -> None: ...

        with pytest.raises(TypeError, match="async setup"):
            InProcessWorker(MemoryStore(), records, hooks=[SetupOnly()])  # type: ignore[list-item]
        with pytest.raises(ValueError, match="a run's checks are installed checks"):
            asyncio.run(
                admit_run(
                    MemoryStore(),
                    records,
                    InProcessWorker(MemoryStore(), records),
                    SINGLE_USER,
                    entitlements=COMMUNITY,
                    run_id=str(uuid.uuid4()),
                    project=DEFAULT_PROJECT,
                    target=Target(kind="agent", agent_id=AGENT),
                    pin={},
                    dataset=None,  # type: ignore[arg-type]
                    max_cases=1,
                    checks=[ScorerVersion(name="python", kind="code", type="python")],
                )
            )
    finally:
        records.close()


async def test_a_process_per_run_runs_the_hooks_and_checks_its_setup_installs(tmp_path):
    store, records = await asyncio.to_thread(open_stores, tmp_path)
    principal = member("org-a")
    await store.create_project(principal, Project(id=DEFAULT_PROJECT, name="Default"))
    dataset = await store.create_dataset(principal, DEFAULT_PROJECT, "support-faq", cases("org-a"))
    worker = ProcessWorker(hooked_services, records, agents=[AGENT], options={"folder": str(tmp_path)})
    try:
        run = await admitted(store, records, worker, dataset, [installed_check("carries_token")], principal)
        await worker.drain()
        finished = await records.get_run(principal, DEFAULT_PROJECT, run.id)
        scores = await records.list_scores(principal, DEFAULT_PROJECT, run.id)
    finally:
        records.close()
    assert finished is not None and finished.state == "completed", finished
    checked = {s.case_id: (s.value, s.rationale) for s in scores if s.scorer == "carries_token"}
    assert checked == {f"org-a-{n}": (True, "the reply ends with the case's token") for n in range(3)}
    steps = [json.loads(line) for line in (tmp_path / HOOK_LOG).read_text(encoding="utf-8").splitlines()]
    assert [step[:-1] for step in steps] == [
        step for n in range(3) for step in (["setup", f"org-a-{n}"], ["teardown", f"org-a-{n}", ""])
    ]
    # The hooks ran in the run's own process, never in this one.
    assert os.getpid() not in {step[-1] for step in steps}


# --- schedules and monitors (MartianSecurity/martian-notes#197, #196) ---------------------------

MONITORED = Entitlements(edition="enterprise", features=PAID, limits={"concurrent_runs": 1})


async def test_a_scheduled_run_runs_the_hooks_and_its_suite_job_grades_an_installed_check_with_no_state(
    tmp_path, monkeypatch
):
    store, dataset = await published(LEDGER_CASES[:2])
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    edition = Edition()
    worker = worker_for(store, records, edition)
    holding = Holding(worker)
    slot = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    try:
        draft = Suite(id="suite-1", name="Ledger", scorers=[installed_check("had_state")], digest="0" * 64)
        suite = await records.save_suite(
            SINGLE_USER, DEFAULT_PROJECT, draft.model_copy(update={"digest": suite_digest(draft)})
        )
        schedule = Schedule(
            id="sc1",
            project_id=DEFAULT_PROJECT,
            name="Hourly",
            every_seconds=3600,
            suite_id=suite.id,
            suite_version=suite.version,
            suite_name=suite.name,
            scorers=suite.scorers,
            target=Target(kind="agent", agent_id=AGENT),
            dataset_name=dataset.name,
            dataset_id=dataset.id,
            dataset_digest=dataset.digest,
            max_cases=100,
            active_since=slot - timedelta(seconds=30),
            created_at=slot - timedelta(seconds=30),
        )
        await records.create_schedule(SINGLE_USER, DEFAULT_PROJECT, schedule)
        monkeypatch.setattr(schedule_module, "_now", lambda: slot + timedelta(seconds=1))
        turn = Work(kind="schedule", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, schedule_id="sc1")
        await execute_schedule(ScheduleServices(store, lambda _p: MONITORED), records, holding, turn)
        # The turn admitted the slot's run and ran no hook.
        assert edition.events == []
        [work] = holding.works
        await worker.run(work)  # the run it admitted, as the queue's worker runs it
        await after_scheduled_run(records, worker, work)  # which queues the run's suite job
        await worker.drain()
        run = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, work.run_id)
        scores = await records.list_scores(SINGLE_USER, DEFAULT_PROJECT, work.run_id)
    finally:
        records.close()
    assert run is not None and run.state == "completed" and run.provenance[SCHEDULE_KEY]["id"] == "sc1"
    # A run like any other: the hooks ran around each of its cases, and the target got the ledger.
    for case_id in ("right", "wrong"):
        assert [event[:2] for event in steps_of(edition.events, case_id)] == [
            ("setup", "first"),
            ("setup", "second"),
            ("call", case_id),
            ("teardown", "second"),
            ("teardown", "first"),
        ]
    assert all("ledger" in case.inputs for case in edition.sent) and edition.ledgers == {}
    # It was admitted with no installed check: the suite's check graded in its suite job, after
    # the case's teardown, with no state.
    assert [scorer.name for scorer in run.scorers] == ["exact_match"]
    graded = {(s.scoring_job_id, s.case_id, s.scorer): (s.value, s.rationale) for s in scores}
    job = suite_job_id(run.id)
    assert {key: value for key, value in graded.items() if key[2] == "had_state"} == {
        (job, case_id, "had_state"): (False, "given no state") for case_id in ("right", "wrong")
    }


async def test_a_monitors_pass_runs_no_hook_and_takes_no_trace_of_a_hooked_run(tmp_path):
    store, dataset = await published(LEDGER_CASES[:1])
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    edition = Edition()
    worker = InProcessWorker(
        store, records, targets={AGENT: edition.agent}, hooks=edition.hooks, monitor_settle_seconds=0
    )
    since = datetime.now(UTC) - timedelta(minutes=5)
    try:
        await records.create_monitor(
            SINGLE_USER,
            DEFAULT_PROJECT,
            Monitor(
                id="m1",
                project_id=DEFAULT_PROJECT,
                name="Live length",
                scorers=[builtin_scorer("response_length")],
                sampling_rate=1,
                active_since=since,
                created_at=since,
            ),
        )
        run = await admitted(store, records, worker, dataset, [])
        await worker.drain()
        hooked = list(edition.events)
        live = await store.record_trace(
            SINGLE_USER,
            Trace(
                id="tr-" + uuid.uuid4().hex,
                project_id=DEFAULT_PROJECT,
                name="support turn",
                state="ok",
                request_time=since + timedelta(minutes=1),
                request="Where is my order?",
                response="It ships tomorrow.",
            ),
        )
        await worker.submit(Work(kind="monitor", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, monitor_id="m1"))
        await worker.drain()
        taken = await records.list_monitored_traces(SINGLE_USER, DEFAULT_PROJECT, "m1", limit=100)
        [result] = (await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run.id)).items
    finally:
        records.close()
    assert [event[0] for event in hooked] == ["setup", "setup", "call", "teardown", "teardown"]
    # The pass scored the live trace and ran no hook; the hooked run's trace is an eval run's, which
    # a monitor never takes.
    assert edition.events == hooked
    assert [(t.trace_id, t.state) for t in taken.items] == [(live.id, "scored")]
    assert result.store.trace_id not in {t.trace_id for t in taken.items}


# --- the suite file ---------------------------------------------------------------------------

INSTALLED_CHECKS = [
    {"type": "installed", "name": "ledger_holds", "options": {"at_least": 1, "ledger": "orders"}},
    {"type": "installed", "name": "installed"},
]


def write_suite(folder: Path, checks: list[dict[str, Any]]) -> Path:
    rows = [{"id": "one", "input": "Record A-1", "output": "Done.", "expectations": {"entry": "A-1"}}]
    (folder / "cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    path = folder / "evals.yaml"
    path.write_text(yaml.safe_dump({"data": "cases.jsonl", "checks": checks}, sort_keys=False), encoding="utf-8")
    return path


def test_a_suite_file_names_an_installed_check_by_its_name_and_options_only(tmp_path):
    suite, cases_ = load_suite_file(write_suite(tmp_path, INSTALLED_CHECKS))
    assert suite.scorers == [
        installed_check("ledger_holds", {"at_least": 1, "ledger": "orders"}),
        ScorerVersion(name="installed", kind="code", type="installed"),
    ]
    text = suite_to_yaml(suite)
    assert yaml.safe_load(text)["checks"] == INSTALLED_CHECKS
    assert suite_from_yaml(text, name=suite.name, cases=cases_) == suite


@pytest.mark.parametrize(
    ("check", "problem"),
    [
        ({"type": "installed"}, "an installed check needs a name"),
        ({"type": "installed", "name": "x", "file": "a.py"}, "file does not apply to installed checks"),
        ({"type": "installed", "name": "x", "options": "at_least"}, "options: Input should be a valid dictionary"),
        ({"type": "not_contains", "values": ["x"], "options": {}}, "options does not apply to not_contains checks"),
    ],
)
def test_an_installed_checks_fields_are_checked_when_the_suite_loads(tmp_path, check, problem):
    with pytest.raises(ConfigError, match=problem):
        load_suite_file(write_suite(tmp_path, [check]))


def test_martian_evals_run_refuses_an_installed_check_before_anything_is_written(tmp_path):
    path = write_suite(tmp_path, [{"type": "is_json"}, INSTALLED_CHECKS[0]])
    refusal = "check ledger_holds: an installed check runs in a worker that installs a check under that name"
    with pytest.raises(ConfigError, match=refusal):
        run_suite(path)
    result = CliRunner().invoke(app, ["evals", "run", str(path)])
    assert result.exit_code == 2 and refusal in result.output
    assert not (tmp_path / "results").exists() and not (tmp_path / "evals-home").exists()


def test_martian_evals_run_refuses_an_installed_check_in_a_conversation_suite(tmp_path):
    example = Path(__file__).resolve().parents[2] / "examples" / "evals" / "conversation"
    for name in ("cases.jsonl", "agent.py"):
        (tmp_path / name).write_text((example / name).read_text(encoding="utf-8"), encoding="utf-8")
    text = (example / "evals.yaml").read_text(encoding="utf-8")
    (tmp_path / "evals.yaml").write_text(
        text + "  - type: installed\n    name: ledger_holds\n    scope: conversation\n", encoding="utf-8"
    )
    suite, _ = load_suite_file(tmp_path / "evals.yaml")
    assert suite.scorers[-1] == installed_check("ledger_holds").model_copy(update={"scope": "conversation"})
    with pytest.raises(ConfigError, match="check ledger_holds: an installed check runs in a worker"):
        run_suite(tmp_path / "evals.yaml")
    assert not (tmp_path / "results").exists() and not (tmp_path / "evals-home").exists()


def test_an_audit_lists_an_installed_check_as_not_audited(tmp_path):
    path = write_suite(tmp_path, [{"type": "contains", "values": ["A-1"]}, INSTALLED_CHECKS[0]])
    for audit in (audit_suite(path, output=tmp_path / "audit"), audit_suite(path, run_judges=True, output=tmp_path)):
        assert audit["not_audited"] == [{"label": "ledger_holds", "type": "installed", "reason": INSTALLED}]
    saved, cases_ = load_suite_file(path)
    core = saved.model_copy(update={"scorers": [installed_check("ledger_holds")]})
    assert audit_saved_suite(core, cases_)["not_audited"] == [
        {"label": "ledger_holds", "type": "installed", "reason": INSTALLED}
    ]
