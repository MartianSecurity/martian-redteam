"""A required installed check decides its case's status (MartianSecurity/martian-notes#539).

An edition's installed check (``type: installed``) adds a score to a case. Marked ``required: true``
in its options, it also decides the case: ``passed`` only when the answer matched (when the case has
an expected answer) and every required check returned true; ``failed`` when one returned false, with
its reason as the case's reason; ``error`` when one gave no verdict. A test edition's hook gives
each case the effect its environment shows, and a required check grades it. Nothing here changes a
run with no required check, or a scoring job, which only adds scores to cases a run already decided.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

import mlflow
import pytest
import yaml

import martian.evals.limits
from martian.config import ConfigError
from martian.evals.local_runs import RunAgainRequest, run_again
from martian.evals.model import Case, Run, RunLimits, ScorerVersion, ScoringJob, Turn
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import COMMUNITY, DEFAULT_PROJECT, SINGLE_USER
from martian.evals.server.launch import resume_run
from martian.evals.server.projection import item_out, run_out
from martian.evals.state import STATE
from martian.evals.store import EvalStore
from martian.evals.suite_file import load_suite_file, suite_from_yaml, suite_to_yaml
from martian.evals.worker import CaseContext, InProcessWorker, Work
from martian.evals.worker.execute import CONVERSATION, UNUSABLE
from martian.evals.worker.hooks import installed_check, is_installed
from tests.evals.test_case_hooks import Holding, admitted, published
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

EFFECTS = {"yes": "a record was created", "no": "no record was created"}
ENVIRONMENT_GONE = "the scorer raised RuntimeError: the environment is gone"


def case(name: str, reply: str, effect: str, *, expected: str | None = "Done.", also: str = "yes") -> Case:
    """A case whose agent replies ``reply`` and whose environment shows ``effect``."""
    return Case(
        id=name,
        inputs={"message": f"Do {name}", "reply": reply, "effect": effect, "also": also},
        expectations={"expected_response": expected} if expected else {},
    )


async def agent(case_: Case) -> str:
    return str(case_.inputs["reply"])


class Environment:
    """The edition's hook: gives each case the effect its environment shows, for the checks."""

    async def setup(self, context: CaseContext) -> None:
        context.state.values["effect"] = context.case.inputs["effect"]
        context.state.values["also"] = context.case.inputs["also"]

    async def teardown(self, context: CaseContext, error: str) -> None: ...


def effect_seen(state: Any) -> tuple[bool, str] | int:
    """The required check: did the environment show the expected change?"""
    effect = state.values["effect"]
    if effect == "boom":
        raise RuntimeError("the environment is gone")
    if effect == "number":
        return 3
    return effect == "yes", EFFECTS[effect]


def also_required(state: Any) -> tuple[bool, str]:
    return state.values["also"] == "yes", f"also {state.values['also']}"


def advisory(options: dict[str, Any]) -> tuple[bool, str]:
    """An optional check that always says no: it adds a score and never decides a case."""
    return False, "advisory says no"


def had_state(state: Any) -> tuple[bool, str]:
    return state is not None, "given the case's state" if state is not None else "given no state"


def options_given(options: dict[str, Any]) -> tuple[bool, str]:
    return True, repr(options)


CHECKS = {
    "effect_seen": effect_seen,
    "also_required": also_required,
    "advisory": advisory,
    "had_state": had_state,
    "options_given": options_given,
}


def worker_for(store: EvalStore, records: SqlRecordStore) -> InProcessWorker:
    return InProcessWorker(
        store, records, targets={AGENT: agent}, hooks=[Environment()], checks=CHECKS, target_timeout_seconds=5
    )


def required(name: str, options: dict[str, Any] | None = None) -> ScorerVersion:
    return installed_check(name, {**(options or {}), "required": True})


def records_at(tmp_path: Any) -> SqlRecordStore:
    return SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")


async def results_of(records: SqlRecordStore, run_id: str) -> dict[str, Any]:
    return {r.case_id: r for r in (await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run_id)).items}


def statuses(results: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {case_id: (result.status, result.error) for case_id, result in results.items()}


MATRIX = [
    case("match_true", "Done.", "yes"),
    case("match_false", "Done.", "no"),
    case("match_error", "Done.", "boom"),
    case("match_number", "Done.", "number"),
    case("miss_true", "Nope.", "yes"),
    case("miss_false", "Nope.", "no"),
    case("miss_error", "Nope.", "boom"),
    case("none_true", "Done.", "yes", expected=None),
    case("none_false", "Done.", "no", expected=None),
    case("none_error", "Done.", "boom", expected=None),
    case("also_false", "Done.", "yes", also="no"),
    case("both_false", "Done.", "no", also="no"),
    case("false_and_error", "Done.", "boom", also="no"),
]

NO_RECORD = "Required check effect_seen failed: no record was created"
GONE = f"Required check effect_seen gave no verdict: {ENVIRONMENT_GONE}"
NUMBER = "Required check effect_seen gave no verdict: it returned a number, not true or false"
ALSO_NO = "Required check also_required failed: also no"


# --- the rule ---------------------------------------------------------------------------------


async def test_a_required_check_decides_every_combination_of_answer_and_check(tmp_path):
    store, dataset = await published(MATRIX)
    records = records_at(tmp_path)
    worker = worker_for(store, records)
    try:
        # The optional check says no on every case, so a case that passes passed in spite of it.
        checks = [required("effect_seen"), required("also_required"), installed_check("advisory")]
        run = await admitted(store, records, worker, dataset, checks)
        await worker.drain()
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        results = await results_of(records, run.id)
        scores = await records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        records.close()
    assert finished is not None and finished.state == "completed", finished
    assert statuses(results) == {
        # The answer matched: the required checks decide.
        "match_true": ("passed", ""),
        "match_false": ("failed", NO_RECORD),
        "match_error": ("error", GONE),
        "match_number": ("error", NUMBER),
        # The answer did not match: never passed. A check that is true adds no reason, one that is
        # false gives its own, and one that gave no verdict makes the case an error.
        "miss_true": ("failed", ""),
        "miss_false": ("failed", NO_RECORD),
        "miss_error": ("error", GONE),
        # No expected answer: the required checks grade the case alone.
        "none_true": ("passed", ""),
        "none_false": ("failed", NO_RECORD),
        "none_error": ("error", GONE),
        # Two required checks: every one must be true, and each that is not says why.
        "also_false": ("failed", ALSO_NO),
        "both_false": ("failed", f"{NO_RECORD}; {ALSO_NO}"),
        "false_and_error": ("error", f"{GONE}; {ALSO_NO}"),
    }
    # A case's score is each check's own: the exact match only where there is an expected answer, and
    # a required check's error or value as the check gave it.
    graded = {(s.case_id, s.scorer): s for s in scores}
    assert {s for c, s in graded if c == "none_true"} == {"effect_seen", "also_required", "advisory"}
    assert {s for c, s in graded if c == "match_true"} == {"exact_match", "effect_seen", "also_required", "advisory"}
    assert (graded["match_false", "effect_seen"].value, graded["match_false", "effect_seen"].passed) == (False, False)
    assert graded["match_error", "effect_seen"].error == ENVIRONMENT_GONE
    assert graded["match_error", "effect_seen"].value is None
    assert graded["match_true", "advisory"].value is False and graded["match_true", "exact_match"].value is True
    assert graded["miss_true", "exact_match"].value is False and graded["miss_true", "effect_seen"].value is True


async def test_the_runs_counts_and_pass_rate_follow_the_cases_the_required_check_decided(tmp_path):
    cases_ = [
        case("works", "Done.", "yes"),
        case("no_change", "Done.", "no"),
        case("also_no_change", "Done.", "no", expected=None),
        case("gone", "Done.", "boom"),
    ]
    store, dataset = await published(cases_)
    records = records_at(tmp_path)
    worker = worker_for(store, records)
    try:
        run = await admitted(store, records, worker, dataset, [required("effect_seen")])
        await worker.drain()
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        results = await results_of(records, run.id)
    finally:
        records.close()
    assert finished is not None
    # A case that replied correctly and changed nothing is failed in the run's counts, and the run's
    # pass rate (passed of those that passed or failed) is the environment's, not the replies'.
    assert finished.counts.model_dump() == {"cases": 4, "passed": 1, "failed": 2, "errors": 1, "skipped": 0}
    shown = run_out(finished)
    assert (shown.passed, shown.failed, shown.errored, shown.skipped) == (1, 2, 1, 0)
    assert shown.passed / (shown.passed + shown.failed) == pytest.approx(1 / 3)
    item = item_out(results["no_change"])
    assert (item.status, item.output, item.error) == ("failed", "Done.", NO_RECORD)
    assert item_out(results["works"]).status == "passed" and item_out(results["works"]).error == ""


async def test_without_a_required_check_nothing_changes(tmp_path):
    store, dataset = await published(MATRIX)
    records = records_at(tmp_path)
    worker = worker_for(store, records)
    try:
        # The checks are installed and graded, none required: ``required: false`` is the same as no option.
        checks = [installed_check("effect_seen", {"required": False}), installed_check("also_required")]
        run = await admitted(store, records, worker, dataset, [*checks, installed_check("advisory")])
        await worker.drain()
        results = await results_of(records, run.id)
        scores = await records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run.id)
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        records.close()
    # The status is the exact match's alone, and a failed or erroring check changes none of it.
    assert statuses(results) == {
        **{name: ("passed", "") for name in ("match_true", "match_false", "match_error", "match_number")},
        **{name: ("failed", "") for name in ("miss_true", "miss_false", "miss_error")},
        **{name: ("skipped", UNUSABLE) for name in ("none_true", "none_false", "none_error")},
        **{name: ("passed", "") for name in ("also_false", "both_false", "false_and_error")},
    }
    assert finished is not None and finished.counts.model_dump() == {
        "cases": 13,
        "passed": 7,
        "failed": 3,
        "errors": 0,
        "skipped": 3,
    }
    by = {(s.case_id, s.scorer): s for s in scores}
    assert by["match_error", "effect_seen"].error == ENVIRONMENT_GONE  # scored with an error, status untouched
    assert ("none_true", "exact_match") not in by and ("none_true", "effect_seen") not in by  # skipped: no check ran


async def test_a_case_whose_target_raised_is_still_the_targets_error_and_no_required_check_runs(tmp_path):
    async def raising(case_: Case) -> str:
        raise RuntimeError("the agent broke")

    store, dataset = await published([case("broke", "Done.", "no")])
    records = records_at(tmp_path)
    worker = InProcessWorker(store, records, targets={AGENT: raising}, hooks=[Environment()], checks=CHECKS)
    try:
        run = await admitted(store, records, worker, dataset, [required("effect_seen")])
        await worker.drain()
        results = await results_of(records, run.id)
        scores = await records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        records.close()
    # The target raised, so there is nothing to check: the case's error is the target's, as before.
    assert statuses(results) == {"broke": ("error", "RuntimeError: the agent broke")} and scores == []


async def test_a_conversation_case_is_still_skipped_and_never_graded_by_a_required_check(tmp_path):
    talk = Case(id="talk", turns=[Turn(inputs={"message": "Hello"}), Turn(inputs={"message": "Again"})])
    store, dataset = await published([talk])
    records = records_at(tmp_path)
    worker = worker_for(store, records)
    try:
        run = await admitted(store, records, worker, dataset, [required("effect_seen")])
        await worker.drain()
        results = await results_of(records, run.id)
    finally:
        records.close()
    assert statuses(results) == {"talk": ("skipped", CONVERSATION)}


# --- the option -------------------------------------------------------------------------------


async def test_the_check_is_given_its_options_without_the_cores_own_required(tmp_path):
    store, dataset = await published([case("one", "Done.", "yes")])
    records = records_at(tmp_path)
    worker = worker_for(store, records)
    try:
        check = required("options_given", {"kind": "refund"})
        run = await admitted(store, records, worker, dataset, [check])
        await worker.drain()
        job = ScoringJob(
            id=str(uuid.uuid4()),
            run_id=run.id,
            scorers=[check],
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
    given = {s.scoring_job_id: s.rationale for s in scores if s.scorer == "options_given"}
    assert given == {None: "{'kind': 'refund'}", job.id: "{'kind': 'refund'}"}  # in the run, and in the job


async def test_a_scoring_jobs_required_check_adds_a_score_and_never_changes_a_stored_status(tmp_path):
    cases_ = [case("works", "Done.", "yes"), case("no_change", "Done.", "no")]
    store, dataset = await published(cases_)
    records = records_at(tmp_path)
    worker = worker_for(store, records)
    try:
        run = await admitted(store, records, worker, dataset, [])  # a run with no installed check
        await worker.drain()
        before = await results_of(records, run.id)
        counts = (await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)).counts  # type: ignore[union-attr]
        # A job's required check answers false (a job has no state), and a job's other required check errors.
        job = ScoringJob(
            id=str(uuid.uuid4()),
            run_id=run.id,
            scorers=[required("had_state"), required("effect_seen")],
            state="queued",
            requested_by=SINGLE_USER.user_id,
            created_at=datetime.now(UTC),
        )
        await records.create_scoring_job(SINGLE_USER, DEFAULT_PROJECT, job)
        await worker.submit(Work("scoring", SINGLE_USER, DEFAULT_PROJECT, run.id, job_id=job.id))
        await worker.drain()
        after = await results_of(records, run.id)
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        scores = await records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        records.close()
    assert {s.case_id: s.value for s in scores if s.scorer == "had_state"} == {"works": False, "no_change": False}
    assert all(s.error for s in scores if s.scorer == "effect_seen")  # it reads a state a job does not have
    # The scores are there, and every case's status, reason and the run's counts are as the run left them.
    assert statuses(after) == statuses(before) == {"works": ("passed", ""), "no_change": ("passed", "")}
    assert finished is not None and finished.counts == counts and finished.counts.passed == 2


# --- resume and run again ---------------------------------------------------------------------


async def test_a_resumed_run_keeps_its_finished_cases_and_decides_the_rest_by_the_same_rule(tmp_path, monkeypatch):
    called: list[str] = []
    # The run's clock runs an hour on once its first case is in flight, so its time is up then.
    real = time.monotonic
    monkeypatch.setattr(martian.evals.limits, "monotonic", lambda: real() + 3600 * (len(called) == 1))

    async def counted(case_: Case) -> str:
        called.append(case_.id)
        return await agent(case_)

    cases_ = [case("first", "Done.", "no"), case("second", "Done.", "yes"), case("third", "Done.", "boom")]
    store, dataset = await published(cases_)
    records = records_at(tmp_path)
    worker = InProcessWorker(
        store,
        records,
        targets={AGENT: counted},
        hooks=[Environment()],
        checks=CHECKS,
        limits=RunLimits(max_seconds=60),
    )
    try:
        run = await admitted(store, records, worker, dataset, [required("effect_seen")])
        await worker.drain()
        stopped = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        part = await results_of(records, run.id)
        assert stopped is not None and stopped.state == "stopped"
        # It stopped before its second case: the first is kept as the required check decided it.
        assert statuses(part) == {"first": ("failed", NO_RECORD)}
        assert (stopped.counts.passed, stopped.counts.failed, stopped.counts.errors) == (0, 1, 0)
        await resume_run(
            store, records, worker, SINGLE_USER, stopped, entitlements=COMMUNITY, limits=RunLimits(max_seconds=120)
        )
        await worker.drain()
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        results = await results_of(records, run.id)
    finally:
        records.close()
    assert called == ["first", "second", "third"]  # the first case was not called again
    assert finished is not None and finished.state == "completed" and finished.scorers == stopped.scorers
    assert statuses(results) == {"first": ("failed", NO_RECORD), "second": ("passed", ""), "third": ("error", GONE)}
    assert (finished.counts.passed, finished.counts.failed, finished.counts.errors) == (1, 1, 1)


async def test_run_again_admits_the_new_run_with_the_installed_checks_of_the_one_it_repeats(tmp_path):
    store, dataset = await published([case("works", "Done.", "yes"), case("no_change", "Done.", "no")])
    records = records_at(tmp_path)
    worker = worker_for(store, records)
    try:
        checks = [required("effect_seen"), installed_check("advisory")]
        original = await admitted(store, records, worker, dataset, checks)
        await worker.drain()
        again = await run_again(
            uuid.UUID(original.id), RunAgainRequest(), SINGLE_USER, COMMUNITY, store, records, worker
        )
        await worker.drain()
        repeated = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, str(again.id))
        results = await results_of(records, str(again.id))
    finally:
        records.close()
    assert repeated is not None and [s for s in repeated.scorers if is_installed(s)] == checks
    # The repeat is decided by the same required check: it did not lose it on the way.
    assert statuses(results) == {"works": ("passed", ""), "no_change": ("failed", NO_RECORD)}


# --- what is refused --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["yes", "true", 1, 0, None, [True]])
async def test_admission_refuses_a_required_option_that_is_not_true_or_false(tmp_path, value):
    store, dataset = await published([case("works", "Done.", "yes")])
    records = records_at(tmp_path)
    worker = worker_for(store, records)
    try:
        with pytest.raises(ValueError, match="the option required of an installed check is true or false"):
            await admitted(store, records, worker, dataset, [installed_check("effect_seen", {"required": value})])
        assert (await records.list_runs(SINGLE_USER, DEFAULT_PROJECT)).items == []
    finally:
        records.close()


async def test_a_worker_fails_a_run_whose_required_option_is_malformed_rather_than_leave_the_check_advisory(tmp_path):
    store, dataset = await published([case("no_change", "Done.", "no")])
    records = records_at(tmp_path)
    worker = worker_for(store, records)
    holding = Holding(worker)
    try:
        run = await admitted(store, records, holding, dataset, [required("effect_seen")])
        # A record that skipped admission, such as one an edition wrote itself.
        broken: Run = run.model_copy(
            update={"scorers": [*run.scorers[:1], installed_check("effect_seen", {"required": "yes"})]}
        )
        await records.update_run(SINGLE_USER, broken, expected_state="queued")
        await worker.run(holding.works[0])
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        results = await results_of(records, run.id)
    finally:
        records.close()
    assert finished is not None and finished.state == "failed"
    assert finished.error == "the option required of an installed check is true or false" and results == {}


# --- the suite file ---------------------------------------------------------------------------


def write_suite(folder: Any, checks: list[dict[str, Any]]) -> Any:
    (folder / "cases.jsonl").write_text(
        '{"id": "one", "input": "Do one", "output": "Done.", "expectations": {}}\n', encoding="utf-8"
    )
    path = folder / "evals.yaml"
    path.write_text(yaml.safe_dump({"data": "cases.jsonl", "checks": checks}, sort_keys=False), encoding="utf-8")
    return path


async def test_a_suite_file_writes_a_required_installed_check_and_a_run_it_names_is_decided_by_it(tmp_path):
    written = [
        {"type": "installed", "name": "effect_seen", "options": {"kind": "refund", "required": True}},
        {"type": "installed", "name": "advisory"},
    ]
    suite, _ = load_suite_file(write_suite(tmp_path, written))
    # Both ends read and write the option as an option, and the model is unchanged: no new field.
    assert suite.scorers == [
        installed_check("effect_seen", {"kind": "refund", "required": True}),
        installed_check("advisory"),
    ]
    assert installed_check("effect_seen", {"kind": "refund"}, required=True) == suite.scorers[0]
    assert suite.scorers[0].parameters == {"options": {"kind": "refund", "required": True}}
    text = suite_to_yaml(suite)
    assert yaml.safe_load(text)["checks"] == written
    assert suite_from_yaml(text, name=suite.name, cases=load_suite_file(write_suite(tmp_path, written))[1]) == suite
    # An edition lifts the file's installed checks into the run it admits, and they decide its cases.
    store, dataset = await published([case("works", "Done.", "yes"), case("no_change", "Done.", "no")])
    records = records_at(tmp_path)
    worker = worker_for(store, records)
    try:
        run = await admitted(store, records, worker, dataset, [s for s in suite.scorers if is_installed(s)][:1])
        await worker.drain()
        results = await results_of(records, run.id)
    finally:
        records.close()
    assert statuses(results) == {"works": ("passed", ""), "no_change": ("failed", NO_RECORD)}


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_a_suite_file_refuses_a_required_option_that_is_not_true_or_false(tmp_path, value):
    path = write_suite(tmp_path, [{"type": "installed", "name": "effect_seen", "options": {"required": value}}])
    with pytest.raises(ConfigError, match="the option required of an installed check is true or false"):
        load_suite_file(path)


def test_the_installed_check_helper_marks_it_required_and_otherwise_leaves_it_as_it_was():
    assert installed_check("effect_seen") == ScorerVersion(name="effect_seen", kind="code", type="installed")
    assert installed_check("effect_seen", {"kind": "refund"}).parameters == {"options": {"kind": "refund"}}
    assert installed_check("effect_seen", required=True).parameters == {"options": {"required": True}}
    assert installed_check("effect_seen", {"kind": "refund"}, required=True).parameters == {
        "options": {"kind": "refund", "required": True}
    }
