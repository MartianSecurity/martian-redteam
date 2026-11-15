"""The worker interface: the routes hand work over and never run it, and the work itself moves each
record on only from the state it expects and never leaves one running."""

from __future__ import annotations

import uuid

import pytest

from martian.evals.model import Case, Run, ScoringJob, StoreRef, Target
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, ENTERPRISE, SINGLE_USER, organisation_principal
from martian.evals.store import MemoryStore, StoreError
from martian.evals.worker import InProcessWorker, Work, Worker, execute_run, execute_scoring
from martian.evals.worker.scorers import builtin_scorer, code_score
from tests.evals.write_fixture import AGENT, Inert, publish, request_id, serve


@pytest.fixture
def records(tmp_path):
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


def test_work_names_a_job_exactly_when_it_is_scoring():
    Work(kind="run", principal=SINGLE_USER, project_id="p", run_id="r")
    Work(kind="scoring", principal=SINGLE_USER, project_id="p", run_id="r", job_id="j")
    for kind, job in (("run", "j"), ("scoring", None)):
        with pytest.raises(ValueError, match="scoring work names its job"):
            Work(kind=kind, principal=SINGLE_USER, project_id="p", run_id="r", job_id=job)  # type: ignore[arg-type]


def test_both_workers_implement_the_interface(records):
    assert isinstance(InProcessWorker(MemoryStore(), records), Worker)
    assert isinstance(Inert(), Worker)
    assert not isinstance(object(), Worker)


async def test_the_in_process_worker_knows_only_the_agents_it_was_given(records):
    worker = InProcessWorker(MemoryStore(), records, targets={AGENT: lambda case: None})  # type: ignore[dict-item]
    assert await worker.check_target(SINGLE_USER, Target(kind="agent", agent_id=AGENT)) == {
        "kind": "agent",
        "agent_id": AGENT,
    }
    with pytest.raises(StoreError) as refused:
        await worker.check_target(SINGLE_USER, Target(kind="agent", agent_id=str(uuid.uuid4())))
    assert (refused.value.kind, refused.value.detail) == ("not_found", "Agent not found")
    assert not worker.judges


async def test_the_in_process_worker_runs_only_its_own_organisations_work(records):
    worker = InProcessWorker(MemoryStore(), records, organisation_id="org-a", targets={AGENT: lambda case: None})  # type: ignore[dict-item]
    own = organisation_principal("member-a", "org-a", frozenset({"member"}))
    assert await worker.check_target(own, Target(kind="agent", agent_id=AGENT))
    for other in (organisation_principal("member-b", "org-b", frozenset({"member"})), SINGLE_USER):
        with pytest.raises(ValueError, match="one organisation's work"):
            await worker.check_target(other, Target(kind="agent", agent_id=AGENT))
        with pytest.raises(ValueError, match="one organisation's work"):
            await worker.submit(Work(kind="run", principal=other, project_id=DEFAULT_PROJECT, run_id="r"))
    await worker.drain()  # nothing was started


def test_a_launch_and_a_scoring_request_hand_their_work_over_without_running_it(records):
    calls: list[str] = []

    async def recording(case: Case) -> str:
        calls.append(case.id)
        return "answer"

    client, core = serve(MemoryStore(), records, targets={AGENT: recording}, entitlements=lambda _p: ENTERPRISE)
    inert = Inert()
    core.client.app.state.martian_evals_worker = inert  # type: ignore[attr-defined]
    with client:
        run = core.launch()
        assert run["state"] == "queued" and calls == []
        (work,) = inert.submitted
        assert (work.kind, work.project_id, work.run_id) == ("run", DEFAULT_PROJECT, run["id"])
        # The real worker runs it later, outside any request.
        assert core.client.portal is not None
        core.client.portal.call(core.worker._execute, work)
        assert calls == ["faq-1", "faq-2"]  # faq-3 has no expected text and is skipped
        job = core.call(
            "POST", f"/v1/evals/{run['id']}/scores", 202, json={"request_id": request_id(), "scorers": ["exact_match"]}
        )
        assert job["state"] == "queued" and inert.submitted[-1].job_id == job["id"]


PIN = {"kind": "agent", "agent_id": AGENT}
"""A function target's pin, as a launch records it."""


def _run(**changes) -> Run:
    base = Run(
        id=str(uuid.uuid4()),
        project_id=DEFAULT_PROJECT,
        source="console",
        state="queued",
        target=Target(kind="agent", agent_id=AGENT),
        dataset_name="support-faq",
        dataset_digest="0" * 64,
        provenance={"target": PIN},
        store=StoreRef(dataset_id="ds-missing"),
    )
    return base.model_copy(update=changes)


async def _pinned(run: Run) -> dict[str, str]:
    """The pin of the run's target as the worker has it now: the one it was admitted with."""
    return PIN


async def test_a_run_fails_with_its_reason_and_work_not_queued_is_left_alone(records):
    store = MemoryStore()
    await publish(store)
    work = lambda run: Work(kind="run", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=run.id)  # noqa: E731

    gone = await records.create_run(SINGLE_USER, _run())
    await execute_run(store, records, work(gone), call_for=lambda run: None, pin_for=_pinned, timeout_seconds=1)
    failed = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, gone.id)
    assert (failed.state, failed.error) == ("failed", "The worker cannot call this run's target")  # type: ignore[union-attr]

    async def answer(case: Case) -> str:
        return "x"

    changed = await records.create_run(SINGLE_USER, _run())
    await execute_run(store, records, work(changed), call_for=lambda run: answer, pin_for=_pinned, timeout_seconds=1)
    failed = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, changed.id)
    assert (failed.state, failed.error) == ("failed", "Dataset identity changed; submit a new eval run")  # type: ignore[union-attr]

    finished = await records.create_run(SINGLE_USER, _run(state="completed"))
    await execute_run(store, records, work(finished), call_for=lambda run: answer, pin_for=_pinned, timeout_seconds=1)
    assert await records.get_run(SINGLE_USER, DEFAULT_PROJECT, finished.id) == finished


async def test_scoring_a_run_that_is_not_complete_fails_the_job(records):
    store = MemoryStore()
    run = await records.create_run(SINGLE_USER, _run(state="running"))
    job = ScoringJob(id="j1", run_id=run.id, scorers=[builtin_scorer("exact_match")], state="queued")
    await records.create_scoring_job(SINGLE_USER, DEFAULT_PROJECT, job)
    work = Work(kind="scoring", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=run.id, job_id="j1")
    await execute_scoring(store, records, work, judge=None)
    (failed,) = await records.list_scoring_jobs(SINGLE_USER, DEFAULT_PROJECT, run.id)
    assert failed.state == "failed" and failed.error.startswith("Scoring interrupted")


def test_code_scorers_read_only_the_saved_output():
    assert code_score("exact_match", "  Yes ", "yes")[0] is True
    assert code_score("contains_expected", "It is yes.", "Yes")[0] is True
    assert code_score("contains_expected", "anything", " ")[0] is False
    assert code_score("valid_json", '{"a": 1}', "")[0] is True
    assert code_score("valid_json", "NaN", "")[0] is False
    assert code_score("response_length", "héllo", "")[0] == 5
    with pytest.raises(ValueError, match="not a code scorer"):
        code_score("correctness", "x", "y")
