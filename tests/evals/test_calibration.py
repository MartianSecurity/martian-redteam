"""Each judge version's calibration: saved from its agreement report on request, checked only while
the judge runs on the model it was measured on, and named by every judge score the core makes. A
fake judge answers; nothing calls a model."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from martian.evals.agreement import MINIMUM_LABELLED
from martian.evals.calibration import (
    CALIBRATION_KEY,
    NO_CALIBRATION,
    applied_calibration,
    calibration_state,
    newest,
    save_calibration,
)
from martian.evals.judge_settings import MemoryJudgeSettings
from martian.evals.local import open_stores
from martian.evals.model import CaseResult, Review, Run, Score, ScorerVersion, Suite, digest
from martian.evals.records import JudgeCalibration
from martian.evals.records.sql import SqlRecordStore
from martian.evals.run_record import GradedCase, RunRecorder
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER, create_app, organisation_principal
from martian.evals.store import MemoryStore, StoreError
from martian.evals.store.mlflow_local import MlflowStore
from martian.evals.worker.scorers import builtin_scorer
from tests.evals.test_agreement import seed
from tests.evals.write_fixture import Served, request_id, serve

T0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
JUDGED = [i % 4 != 0 for i in range(MINIMUM_LABELLED)]
LABELLED = [i % 5 != 0 for i in range(MINIMUM_LABELLED)]


def calibration(id: str, version: str = "2", *, model: str | None = "openai:/judge", minutes: int = 0):
    return JudgeCalibration(
        id=id,
        project_id=DEFAULT_PROJECT,
        judge="polite_tone",
        version=version,
        model=model,
        labelled=MINIMUM_LABELLED,
        created_at=T0.replace(minute=minutes),
    )


def test_a_judge_version_is_checked_only_by_a_calibration_measured_on_the_model_it_runs_on():
    saved = calibration("c1")
    assert calibration_state(None, "openai:/judge", None) == ("unchecked", "none")
    assert calibration_state(saved, "openai:/judge", None) == ("checked", None)
    assert calibration_state(saved, "openai:/other", None) == ("unchecked", "model_changed")
    assert calibration_state(saved, "openai:/judge", "http://localhost:4000/v1") == ("unchecked", "model_changed")
    assert applied_calibration(saved, "openai:/judge", None) == "c1"
    assert applied_calibration(saved, "openai:/other", None) == NO_CALIBRATION
    assert applied_calibration(None, None, None) == NO_CALIBRATION
    # Listed newest first, as the record store lists them: the first of each version is its newest.
    found = newest([calibration("c3", minutes=3), calibration("c2", "1", minutes=2), calibration("c1", minutes=1)])
    assert {key: c.id for key, c in found.items()} == {("polite_tone", "2"): "c3", ("polite_tone", "1"): "c2"}


# --- the routes ---------------------------------------------------------------------------------


@pytest.fixture(params=["memory", "mlflow"])
def stores(request, tmp_path):
    store = MemoryStore() if request.param == "memory" else MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path)
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store, records
    records.close()


def call(client: TestClient, method: str, path: str, status: int, **kwargs):
    response = client.request(method, path, **kwargs)
    assert response.status_code == status, response.text
    return response.json()


def statuses(client: TestClient, **params) -> dict[tuple[str, str], tuple[str, str | None, str | None]]:
    items = call(client, "GET", "/v1/evals/judge-calibrations", 200, params=params)["items"]
    return {
        (item["judge"], item["version"]): (item["status"], item["reason"], (item["calibration"] or {}).get("id"))
        for item in items
    }


def test_saving_a_calibration_keeps_the_versions_report_its_labelled_items_and_model(stores):
    store, records = stores
    asyncio.run(seed(store, records, MINIMUM_LABELLED, judged=JUDGED, labelled=LABELLED))
    client = TestClient(
        create_app(store=store, records=records, principal=lambda: SINGLE_USER, judge_settings=MemoryJudgeSettings())
    )
    # Every registered version and each built-in model judge, none of them checked yet.
    before = statuses(client)
    assert list(before) == [
        ("correctness", "v1"),
        ("policy", "v1"),
        ("polite_tone", "1"),
        ("polite_tone", "2"),
        ("safety", "v1"),
    ]
    assert set(before.values()) == {("unchecked", "none", None)}

    # Version 1 never judged anything, so it has too few labelled items to calibrate.
    refused = call(client, "POST", "/v1/evals/judge-calibrations", 422,
                   json={"request_id": request_id(), "name": "polite_tone", "version": "1"})  # fmt: skip
    assert refused["detail"] == f"Too few labelled items to calibrate: 0 of the {MINIMUM_LABELLED} needed"

    body = {"request_id": request_id(), "name": "polite_tone", "version": "2"}
    saved = call(client, "POST", "/v1/evals/judge-calibrations", 201, json=body)
    agreement = call(client, "GET", "/v1/evals/judge-agreement", 200, params={"name": "polite_tone"})
    assert saved["report"] == agreement["items"][1] and saved["report"]["status"] == "measured"
    assert (saved["judge"], saved["version"], saved["labelled"], saved["model"], saved["endpoint"]) == (
        "polite_tone",
        "2",
        MINIMUM_LABELLED,
        "openai:/judge",
        None,
    )
    assert saved["created_at"] and saved["actor_id"] and saved["project_id"] is None
    assert (saved["traces_examined"], saved["traces_truncated"]) == (MINIMUM_LABELLED, False)

    # The same request again is the same calibration; its request_id for another version is a conflict.
    assert call(client, "POST", "/v1/evals/judge-calibrations", 201, json=body) == saved
    other = {**body, "version": "1"}
    assert call(client, "POST", "/v1/evals/judge-calibrations", 409, json=other)["detail"] == (
        "request_id belongs to another calibration"
    )
    assert statuses(client, name="polite_tone") == {
        ("polite_tone", "1"): ("unchecked", "none", None),
        ("polite_tone", "2"): ("checked", None, saved["id"]),
    }
    assert call(client, "GET", f"/v1/evals/judge-calibrations/{saved['id']}", 200) == saved

    # A newer calibration takes its place; the earlier one stays readable for the scores that name it.
    newer = call(client, "POST", "/v1/evals/judge-calibrations", 201, json={**body, "request_id": request_id()})
    assert statuses(client, name="polite_tone")[("polite_tone", "2")] == ("checked", None, newer["id"])
    assert call(client, "GET", f"/v1/evals/judge-calibrations/{saved['id']}", 200)["id"] == saved["id"]

    unknown = {"request_id": request_id(), "name": "concise_reply", "version": "1"}
    missing = call(client, "POST", "/v1/evals/judge-calibrations", 404, json=unknown)
    assert missing["detail"] == "Judge version not found"
    elsewhere = {**body, "request_id": request_id(), "project_id": str(uuid.uuid4())}
    call(client, "POST", "/v1/evals/judge-calibrations", 404, json=elsewhere)
    call(client, "GET", "/v1/evals/judge-calibrations", 404, params={"project_id": str(uuid.uuid4())})
    call(client, "GET", f"/v1/evals/judge-calibrations/{uuid.uuid4()}", 404)
    call(client, "POST", "/v1/evals/judge-calibrations", 422, json={**body, "name": "Not A Name"})


def test_only_a_role_that_manages_judges_saves_a_calibration(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    member = organisation_principal("member-1", "org-a", frozenset({"member"}))
    client = TestClient(create_app(store=MemoryStore(), records=records, principal=lambda: member))
    body = {"request_id": request_id(), "name": "correctness", "version": "v1"}
    assert call(client, "POST", "/v1/evals/judge-calibrations", 403, json=body)
    # Reading is open to every role; with no project yet there are only the built-in judges.
    assert set(statuses(client).values()) == {("unchecked", "none", None)}
    records.close()


# --- the model it was measured on ---------------------------------------------------------------


async def labelled_run(served: Served, judge: str, version: str, *, model: str) -> None:
    """A completed run whose every case the judge version judged and a person labelled, enough to
    measure it."""
    principal, records = SINGLE_USER, served.records
    run_id = str(uuid.uuid4())
    run = Run(id=run_id, project_id=DEFAULT_PROJECT, code="EV-9", source="console", state="completed", created_at=T0)
    await records.create_run(principal, run)
    cases = [f"labelled-{i}" for i in range(MINIMUM_LABELLED)]
    results = [CaseResult(run_id=run_id, case_id=case, position=i, status="passed") for i, case in enumerate(cases)]
    await records.put_results(principal, DEFAULT_PROJECT, results)
    scores = [
        Score(run_id=run_id, case_id=case, scorer=judge, scorer_version=version, source="llm_judge",
              source_id=model, value=JUDGED[i], passed=JUDGED[i], scoring_job_id="job-0", created_at=T0)
        for i, case in enumerate(cases)
    ]  # fmt: skip
    await records.put_scores(principal, DEFAULT_PROJECT, scores)
    for i, case in enumerate(cases):
        review = Review(id=f"review-{run_id}-{i}", run_id=run_id, case_id=case, criterion=judge, state="complete",
                        value=LABELLED[i], created_at=T0)  # fmt: skip
        await records.add_review(principal, DEFAULT_PROJECT, review)


@pytest.fixture
def core(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    client, served = serve(MemoryStore(), records)
    with client:
        yield served
    records.close()


def judge_scores(core: Served, judges: list[dict[str, object]]) -> list[dict[str, object]]:
    run = core.completed_run()
    body = {"request_id": request_id(), "acknowledge_model_cost": True, "scorers": ["correctness"], "judges": judges}
    core.call("POST", f"/v1/evals/{run['id']}/scores", 202, json=body)
    core.drain()
    assessments = core.call("GET", f"/v1/evals/{run['id']}/workbench", 200)["assessments"]
    return [a for a in assessments if a["source"] == "LLM_JUDGE"]


def test_each_judge_score_names_the_calibration_that_applied_when_it_was_made(core: Served):
    registered = {"request_id": request_id(), "name": "tone", "instructions": "Is {{ outputs }} polite?"}
    core.call("POST", "/v1/evals/judges", 201, json=registered)
    core.call("PUT", "/v1/evals/judge-model", 200, json={"model": "openai:/alpha"})
    asyncio.run(labelled_run(core, "tone", "1", model="openai:/alpha@2026-11"))

    # Before any calibration, every judge score names none, judged or not.
    before = judge_scores(core, [{"name": "tone", "version": 1}])
    assert {a["name"] for a in before} == {"tone", "correctness"}
    assert {a["provenance"][CALIBRATION_KEY] for a in before} == {NO_CALIBRATION}

    # The judge names no model, so its calibration is measured on the default judge model.
    saved = core.call("POST", "/v1/evals/judge-calibrations", 201,
                      json={"request_id": request_id(), "name": "tone", "version": "1"})  # fmt: skip
    assert saved["model"] == "openai:/alpha"
    after = judge_scores(core, [{"name": "tone", "version": 1}])
    named = {a["name"]: a["provenance"][CALIBRATION_KEY] for a in after}
    assert named == {"tone": saved["id"], "correctness": NO_CALIBRATION}
    found = core.call("GET", f"/v1/evals/judge-calibrations/{named['tone']}", 200)
    assert (found["judge"], found["version"], found["labelled"]) == ("tone", "1", MINIMUM_LABELLED)

    # A new default judge model: the judge now runs on a model its calibration was not measured on.
    core.call("PUT", "/v1/evals/judge-model", 200, json={"model": "openai:/beta"})
    listed = core.call("GET", "/v1/evals/judge-calibrations", 200, params={"name": "tone"})["items"]
    assert [(i["version"], i["model"], i["status"], i["reason"]) for i in listed] == [
        ("1", "openai:/beta", "unchecked", "model_changed")
    ]
    assert listed[0]["calibration"]["id"] == saved["id"]
    changed = judge_scores(core, [{"name": "tone", "version": 1}])
    assert {a["provenance"][CALIBRATION_KEY] for a in changed} == {NO_CALIBRATION}
    # The earlier scores keep the calibration that applied when they were made.
    assert {a["provenance"][CALIBRATION_KEY] for a in after if a["name"] == "tone"} == {saved["id"]}


# --- the SDK and suite-file runs -----------------------------------------------------------------


def test_the_sdk_saves_a_calibration_in_the_local_data_folder(tmp_path):
    store, records = open_stores(tmp_path)
    asyncio.run(seed(store, records, MINIMUM_LABELLED, judged=JUDGED, labelled=LABELLED))
    records.close()

    saved = save_calibration("polite_tone", 2, data_dir=tmp_path, request_id="sdk-1")
    assert (saved.judge, saved.version, saved.model, saved.labelled) == ("polite_tone", "2", "openai:/judge", 30)
    assert saved.report["status"] == "measured" and saved.created_by == SINGLE_USER.user_id
    assert save_calibration("polite_tone", "2", data_dir=tmp_path, request_id="sdk-1") == saved
    with pytest.raises(StoreError) as caught:
        save_calibration("polite_tone", 1, data_dir=tmp_path)
    assert caught.value.kind == "invalid"
    with pytest.raises(StoreError) as caught:
        save_calibration("concise_reply", 1, data_dir=tmp_path)
    assert caught.value.kind == "not_found"

    _, records = open_stores(tmp_path)
    assert [c.id for c in asyncio.run(records.list_calibrations(SINGLE_USER, DEFAULT_PROJECT))] == [saved.id]
    records.close()


def test_a_suite_file_runs_judge_scores_name_no_calibration(tmp_path):
    rubric = ScorerVersion(name="rubric", kind="llm_judge", type="rubric", model="openai:/judge")
    suite = Suite(id="evals", name="evals", scorers=[rubric, builtin_scorer("exact_match")], digest=digest({"s": 1}))
    record = {"id": "c1", "passed": True, "checks": {"rubric": {"passed": True}, "exact_match": {"passed": True}}}
    with RunRecorder(tmp_path, suite) as recorder:
        run = recorder.start(planned=1, total=1, limit=None, reused=[])
        recorder.graded([GradedCase(position=0, record=record, expectations={})])
        recorder.finish()
    _, records = open_stores(tmp_path)
    scores = {s.scorer: s.metadata for s in asyncio.run(records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run.id))}
    records.close()
    assert scores == {"rubric": {CALIBRATION_KEY: NO_CALIBRATION}, "exact_match": {}}
