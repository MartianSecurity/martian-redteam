"""The core's write routes launch, score and review runs, and edit datasets, suites, judges, queues,
projects and views, through the store, the record store and the worker alone. The first test runs
on MemoryStore and on the in-process MLflow binding; the rest on MemoryStore."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from martian.evals.manifests import CHANGED, PAUSED, manifest_for
from martian.evals.model import Case
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import (
    COMMUNITY,
    DEFAULT_PROJECT,
    ENTERPRISE,
    SINGLE_USER,
    create_app,
    current_principal,
    organisation_principal,
    scoring,
)
from martian.evals.server.dependencies import optional_worker
from martian.evals.server.drafts import MAX_UPLOAD_BYTES, _limit_body
from martian.evals.server.projection import user_out
from martian.evals.store import MemoryStore
from martian.evals.store.mlflow_local import MlflowStore
from tests.evals.write_fixture import AGENT, JUDGE_MODEL, Inert, Served, agent, request_id, serve

ADMIN = organisation_principal("admin-1", "org-a", frozenset({"admin"}))
MEMBER = organisation_principal(str(uuid.UUID(int=21)), "org-a", frozenset({"member"}))
VIEWER = organisation_principal("viewer-1", "org-a", frozenset({"viewer"}))


@pytest.fixture
def records(tmp_path):
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


@pytest.fixture(params=["memory", "mlflow"])
def core(request, tmp_path, records):
    store = MemoryStore() if request.param == "memory" else MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path)
    client, served = serve(store, records, entitlements=lambda _p: ENTERPRISE)
    with client:
        yield served


@pytest.fixture
def memory(records):
    """An organisation's core on MemoryStore, its admin making the requests."""
    client, served = serve(MemoryStore(), records, entitlements=lambda _p: ENTERPRISE, principal=ADMIN)
    with client:
        yield served


def items_by_case(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["dataset_item_id"]: item for item in run["items"]}


# --- the whole loop ---------------------------------------------------------------------------


def test_a_run_is_launched_scored_and_reviewed_through_the_core_alone(core: Served):
    launched = core.launch(max_items=10)
    assert (launched["state"], launched["run_code"], launched["agent_id"]) == ("queued", "EV-1001", AGENT)
    core.drain()
    run = core.call("GET", f"/v1/evals/{launched['id']}", 200)
    assert (run["state"], run["passed"], run["failed"], run["skipped"], run["errored"]) == ("completed", 1, 1, 1, 0)
    cases = items_by_case(run)
    assert [cases[c]["status"] for c in ("faq-1", "faq-2", "faq-3")] == ["passed", "failed", "skipped"]
    assert cases["faq-3"]["error"] == "item is missing a usable prompt or expected output"
    # Each case that called the agent has a trace in the store, with its exact-match score.
    trace = core.call("GET", f"/v1/evals/{run['id']}/items/{cases['faq-1']['id']}/trace", 200)
    assert trace["trace_id"].startswith("tr-") and trace["spans"][0]["name"] == "target"
    assert [(a["assessment_name"], a["feedback"]["value"]) for a in trace["annotations"]] == [("exact_match", True)]
    assert cases["faq-3"]["evals_trace_id"] == ""

    body = {"request_id": request_id(), "scorers": ["contains_expected", "correctness"], "acknowledge_model_cost": True}
    job = core.call("POST", f"/v1/evals/{run['id']}/scores", 202, json=body)
    assert job["state"] == "queued" and job["config"]["scorers"] == ["contains_expected", "correctness"]
    core.drain()
    work = core.call("GET", f"/v1/evals/{run['id']}/workbench", 200)
    scored = work["selected_job"]
    # Two cases have outputs to score; the skipped case's scores are errors.
    assert (scored["state"], scored["total"], scored["succeeded"], scored["errors"]) == ("partial", 6, 4, 2)
    judged = {(a["name"], a["item_id"]): a for a in work["assessments"]}
    assert judged[("correctness", cases["faq-1"]["id"])]["value"] is True
    assert judged[("correctness", cases["faq-1"]["id"])]["source_id"] == JUDGE_MODEL
    assert judged[("correctness", cases["faq-2"]["id"])]["value"] is False
    assert judged[("contains_expected", cases["faq-3"]["id"])]["error"] == "No successful saved target output"
    assert {c["name"]: c["pass_rate"] for c in work["scorecard"]["criteria"]} == {
        "contains_expected": 0.5,
        "correctness": 0.5,
    }

    item = cases["faq-2"]["id"]
    review = {"request_id": request_id(), "state": "complete", "value": True, "comment": "Close enough."}
    answer = core.call("POST", f"/v1/evals/{run['id']}/items/{item}/reviews", 201, json=review)
    assert (answer["criterion"], answer["value"], answer["previous_id"]) == ("human_review", True, None)
    listed = core.call("GET", "/v1/evals/reviews", 200, params={"filter": "disagreements"})
    assert [(r["id"], r["disagreement"], r["run_code"]) for r in listed["items"]] == [(answer["id"], True, "EV-1001")]
    trace = core.call("GET", f"/v1/evals/{run['id']}/items/{item}/trace", 200)
    assert ("human_review", True) in [(a["assessment_name"], a["feedback"]["value"]) for a in trace["annotations"]]
    history = core.call("GET", f"/v1/evals/{run['id']}/workbench", 200, params={"item_id": item})
    assert [r["id"] for r in history["reviews"]] == [answer["id"]]
    assert "human_review" in [a["name"] for a in history["assessments"]]


# --- launching and cancelling ------------------------------------------------------------------


def test_a_repeated_launch_is_the_same_run_and_a_changed_one_is_refused(memory: Served):
    rid = request_id()
    first = memory.launch(request_id=rid)
    assert memory.launch(request_id=rid)["id"] == first["id"]
    memory.call(
        "POST",
        "/v1/evals",
        409,
        json={"agent_id": AGENT, "dataset_name": "support-faq", "request_id": rid, "max_items": 2},
    )
    memory.drain()
    assert memory.launch()["run_code"] == "EV-1002"


def test_a_launch_checks_its_agent_dataset_and_project(memory: Served):
    memory.call("POST", "/v1/evals", 404, json={"agent_id": str(uuid.uuid4()), "dataset_name": "support-faq"})
    memory.call("POST", "/v1/evals", 422, json={"agent_id": AGENT, "dataset_name": "no-such-dataset"})
    memory.call(
        "POST",
        "/v1/evals",
        404,
        json={"agent_id": AGENT, "dataset_name": "support-faq", "project_id": str(uuid.uuid4())},
    )
    project = memory.call("POST", "/v1/evals/projects", 201, json={"request_id": request_id(), "name": "Checkout"})
    memory.call("POST", f"/v1/evals/projects/{project['id']}/archive", 200)
    refused = memory.call(
        "POST", "/v1/evals", 409, json={"agent_id": AGENT, "dataset_name": "support-faq", "project_id": project["id"]}
    )
    assert refused["detail"] == "This evaluation project is archived or unavailable"


def test_a_running_run_stops_before_its_next_case_when_cancelled(records):
    started, release = threading.Event(), threading.Event()

    async def slow(case):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return await agent(case)

    client, served = serve(MemoryStore(), records, targets={AGENT: slow})
    with client:
        run = served.launch()
        assert started.wait(5)
        assert served.call("POST", "/v1/evals", 409, json={"agent_id": AGENT, "dataset_name": "support-faq"})[
            "detail"
        ] == ("An eval run is already in progress")
        stopping = served.call("POST", f"/v1/evals/{run['id']}/cancel", 202)
        assert stopping["state"] == "running" and stopping["cancel_requested_at"]
        assert (
            served.call("POST", f"/v1/evals/{run['id']}/cancel", 202)["cancel_requested_at"]
            == stopping["cancel_requested_at"]
        )
        release.set()
        served.drain()
        stopped = served.call("GET", f"/v1/evals/{run['id']}", 200)
    assert (stopped["state"], stopped["passed"], stopped["skipped"]) == ("cancelled", 1, 2)
    assert {i["error"] for i in stopped["items"] if i["status"] == "skipped"} == {"Stopped before this target request"}
    # Asking again after it stopped answers with the run as it ended.
    assert served.call("POST", f"/v1/evals/{run['id']}/cancel", 202)["state"] == "cancelled"


def test_a_queued_run_is_cancelled_at_once_and_its_work_is_skipped(records):
    store = MemoryStore()
    client, served = serve(store, records)
    inert = Inert()
    served.client.app.state.martian_evals_worker = inert  # type: ignore[attr-defined]
    with client:
        run = served.launch()
        (work,) = inert.submitted
        assert (work.kind, work.run_id, work.job_id) == ("run", run["id"], None)
        cancelled = served.call("POST", f"/v1/evals/{run['id']}/cancel", 202)
        assert cancelled["state"] == "cancelled" and cancelled["finished_at"]
        # The worker that later takes the work finds it no longer queued and leaves it.
        served.client.portal.call(served.worker._execute, work)  # type: ignore[union-attr]
        assert served.call("GET", f"/v1/evals/{run['id']}", 200)["items"] == []


def test_target_errors_and_timeouts_are_case_errors(records):
    async def flaky(case):
        if case.id == "faq-1":
            raise ConnectionError("agent unreachable")
        await asyncio.sleep(5)

    client, served = serve(MemoryStore(), records, targets={AGENT: flaky})
    served.worker._timeout = 0.05
    with client:
        run = served.completed_run()
    cases = items_by_case(run)
    assert (run["state"], run["errored"], run["skipped"]) == ("completed", 2, 1)
    assert cases["faq-1"]["error"] == "ConnectionError: agent unreachable"
    assert cases["faq-2"]["error"] == "item timed out"


def test_starting_work_without_a_worker_is_refused(records):
    app = create_app(store=MemoryStore(), records=records, principal=lambda: ADMIN)
    from fastapi.testclient import TestClient

    response = TestClient(app).post("/v1/evals", json={"agent_id": AGENT, "dataset_name": "support-faq"})
    assert (response.status_code, response.json()) == (503, {"detail": "This installation runs no evaluation worker"})


def test_roles_come_from_the_capability_table(memory: Served):
    memory.act_as(VIEWER)
    assert (
        memory.call("POST", "/v1/evals", 403, json={"agent_id": AGENT, "dataset_name": "x"})["detail"]
        == "Requires member role"
    )
    memory.act_as(MEMBER)
    refused = memory.call("POST", "/v1/evals/projects", 403, json={"request_id": request_id(), "name": "P"})
    assert refused["detail"] == "Requires admin role"


# --- scoring ----------------------------------------------------------------------------------


def test_the_scorers_read_the_worker_an_edition_binds_on_its_own_application():
    # The server mounts the core's routers on its own application and binds the core's
    # dependencies there, without create_app's state (MartianSecurity/MartianBackend app/evals_core.py).
    app = FastAPI()
    app.include_router(scoring.router)
    app.dependency_overrides[current_principal] = lambda: SINGLE_USER
    with TestClient(app) as client:
        workers = ((SimpleNamespace(judges=True), True), (SimpleNamespace(judges=False), False), (None, False))
        for worker, judging in workers:
            app.dependency_overrides[optional_worker] = lambda found=worker: found
            answered = client.get("/v1/evals/scorers")
            assert answered.status_code == 200, answered.text
            available = {s["name"]: s["available"] for s in answered.json()}
            assert (available["exact_match"], available["correctness"]) == (True, judging)


def test_scoring_needs_a_completed_run_and_a_judge_for_model_scorers(records):
    client, served = serve(MemoryStore(), records, with_judge=False)
    with client:
        names = {s["name"]: s["available"] for s in served.call("GET", "/v1/evals/scorers", 200)}
        assert names == {
            "exact_match": True,
            "contains_expected": True,
            "valid_json": True,
            "response_length": True,
            "correctness": False,
            "safety": False,
            "policy": False,
        }
        run = served.completed_run()
        judged = {"request_id": request_id(), "scorers": ["safety"], "acknowledge_model_cost": True}
        served.call("POST", f"/v1/evals/{run['id']}/scores", 422, json=judged)
        served.call(
            "POST", f"/v1/evals/{run['id']}/scores", 422, json={"request_id": request_id(), "scorers": ["safety"]}
        )
        body = {"request_id": request_id(), "scorers": ["valid_json", "response_length"]}
        job = served.call("POST", f"/v1/evals/{run['id']}/scores", 202, json=body)
        assert served.call("POST", f"/v1/evals/{run['id']}/scores", 202, json=body)["id"] == job["id"]
        changed = {**body, "scorers": ["valid_json"]}
        served.call("POST", f"/v1/evals/{run['id']}/scores", 409, json=changed)
        served.drain()
        work = served.call("GET", f"/v1/evals/{run['id']}/workbench", 200)
        lengths = {a["value"] for a in work["assessments"] if a["name"] == "response_length" and not a["error"]}
        assert lengths == {len("Use the reset link on the sign-in page."), len("I am not sure.")}
        served.call("POST", f"/v1/evals/{run['id']}/scores/{job['id']}/cancel", 409)


def test_a_queued_scoring_job_is_cancelled_and_one_job_runs_per_run(records):
    client, served = serve(MemoryStore(), records)
    with client:
        run = served.completed_run()
        served.client.app.state.martian_evals_worker = Inert()  # type: ignore[attr-defined]
        job = served.call(
            "POST", f"/v1/evals/{run['id']}/scores", 202, json={"request_id": request_id(), "scorers": ["exact_match"]}
        )
        again = {"request_id": request_id(), "scorers": ["valid_json"]}
        assert served.call("POST", f"/v1/evals/{run['id']}/scores", 409, json=again)["detail"] == (
            "A scoring job is already active for this run"
        )
        cancelled = served.call("POST", f"/v1/evals/{run['id']}/scores/{job['id']}/cancel", 202)
        assert cancelled["state"] == "cancelled" and cancelled["cancel_requested_at"]
        served.call("POST", f"/v1/evals/{run['id']}/scores/{uuid.uuid4()}/cancel", 404)


def test_monitoring_lists_scoring_jobs_with_their_scorecards_in_paid_editions(memory: Served):
    run = memory.completed_run()
    memory.call(
        "POST", f"/v1/evals/{run['id']}/scores", 202, json={"request_id": request_id(), "scorers": ["exact_match"]}
    )
    memory.drain()
    listed = memory.call("GET", "/v1/evals/monitoring", 200)
    (row,) = listed["items"]
    assert (row["run_code"], row["state"], row["suite_name"]) == (run["run_code"], "partial", None)
    assert row["scorecard"]["criteria"][0]["pass_rate"] == 0.5
    memory.client.app.state.martian_evals_entitlements = lambda _p: COMMUNITY  # type: ignore[attr-defined]
    assert (
        memory.call("GET", "/v1/evals/monitoring", 403)["detail"] == "The Community edition does not include monitoring"
    )


# --- suites and judges ------------------------------------------------------------------------


def test_a_suite_is_versioned_and_pins_what_a_scoring_job_applies(memory: Served):
    body = {
        "request_id": request_id(),
        "name": "Support",
        "scorers": ["exact_match", "contains_expected"],
        "policies": [{"name": "exact_match", "threshold": 0.9, "minimum_coverage": 0.5}],
    }
    first = memory.call("POST", "/v1/evals/suites", 201, json=body)
    assert (first["version"], first["definition"]["scorers"], first["actor_id"]) == (
        1,
        ["contains_expected", "exact_match"],
        user_out(ADMIN.user_id),
    )
    assert memory.call("POST", "/v1/evals/suites", 201, json=body) == first
    changed = {**body, "request_id": request_id(), "suite_id": first["suite_id"], "scorers": ["exact_match"]}
    memory.call("POST", "/v1/evals/suites", 409, json={**changed, "expected_version": 0})
    second = memory.call("POST", "/v1/evals/suites", 201, json={**changed, "expected_version": 1})
    assert (second["suite_id"], second["version"]) == (first["suite_id"], 2) and second["id"] != first["id"]
    listed = memory.call("GET", "/v1/evals/suites", 200)
    assert [s["version"] for s in listed["items"]] == [2, 1] and listed["total"] == 2

    run = memory.completed_run()
    pinned = {"request_id": request_id(), "scorers": ["exact_match"], "suite_version_id": second["id"]}
    memory.call("POST", f"/v1/evals/{run['id']}/scores", 422, json={**pinned, "scorers": ["valid_json"]})
    job = memory.call("POST", f"/v1/evals/{run['id']}/scores", 202, json=pinned)
    assert (job["config"]["suite_version_id"], job["config"]["policies"][0]["threshold"]) == (second["id"], 0.9)
    memory.drain()
    work = memory.call("GET", f"/v1/evals/{run['id']}/workbench", 200)
    assert work["scorecard"]["gate"] == "failed"  # half the cases match, below the suite's 0.9
    assert memory.call("GET", "/v1/evals/monitoring", 200)["items"][0]["suite_name"] == "Support"


def test_judges_are_registered_in_versions_and_scored_by_the_workers_judge(memory: Served):
    body = {
        "request_id": request_id(),
        "name": "tone",
        "instructions": "Is the reply in {{ outputs }} polite and helpful?",
    }
    first = memory.call("POST", "/v1/evals/judges", 201, json=body)
    assert (first["version"], first["definition"]["kind"], first["operation_id"]) == (1, "boolean", body["request_id"])
    assert memory.call("POST", "/v1/evals/judges", 201, json=body) == first
    memory.call("POST", "/v1/evals/judges", 409, json={**body, "request_id": request_id()})
    second = memory.call(
        "POST", "/v1/evals/judges", 201, json={**body, "request_id": request_id(), "expected_version": 1}
    )
    assert second["version"] == 2
    memory.call("POST", "/v1/evals/judges", 422, json={**body, "name": "correctness"})
    assert [j["version"] for j in memory.call("GET", "/v1/evals/judges", 200, params={"name": "tone"})["items"]] == [
        1,
        2,
    ]
    assert [j["version"] for j in memory.call("GET", "/v1/evals/judges", 200)["items"]] == [2]

    run = memory.completed_run()
    score = {"request_id": request_id(), "judges": [{"name": "tone", "version": 2}], "acknowledge_model_cost": True}
    memory.call(
        "POST", f"/v1/evals/{run['id']}/scores", 404, json={**score, "judges": [{"name": "tone", "version": 9}]}
    )
    job = memory.call("POST", f"/v1/evals/{run['id']}/scores", 202, json=score)
    assert job["config"]["scorers"] == ["tone"]
    memory.drain()
    work = memory.call("GET", f"/v1/evals/{run['id']}/workbench", 200)
    assert {(a["name"], a["source_id"]) for a in work["assessments"] if not a["error"]} == {("tone", JUDGE_MODEL)}


@pytest.mark.parametrize("version", ["²", "١"])
def test_a_stored_judge_version_in_other_digits_is_unsupported(memory: Served, version):
    # isdigit() is true of ², which int() refuses, and of ١, which int() reads as 1.
    body = {"request_id": request_id(), "name": "tone", "instructions": "Is the reply in {{ outputs }} polite?"}
    memory.call("POST", "/v1/evals/judges", 201, json=body)
    assert isinstance(memory.store, MemoryStore)
    (versions,) = memory.store._scorers.values()
    versions[0] = versions[0].model_copy(update={"version": version})
    listed = memory.call("GET", "/v1/evals/judges", 200, params={"name": "tone"})
    assert (listed["items"], listed["unsupported"]) == ([], 1)
    again = {**body, "request_id": request_id(), "expected_version": 1}
    refused = memory.call("POST", "/v1/evals/judges", 409, json=again)
    assert refused["detail"] == "Judge changed or has unsupported versions; reload before registering"


# --- reviews, queues, questions and annotations ------------------------------------------------


def test_a_review_chain_is_kept_and_only_finished_results_are_reviewed(memory: Served):
    run = memory.completed_run()
    item = items_by_case(run)["faq-1"]["id"]
    path = f"/v1/evals/{run['id']}/items/{item}/reviews"
    first = memory.call(
        "POST", path, 201, json={"request_id": request_id(), "state": "complete", "value": True, "comment": "Yes"}
    )
    stale = {"request_id": request_id(), "state": "complete", "value": False, "comment": "No"}
    assert memory.call("POST", path, 409, json=stale)["detail"] == "Review changed; reload before saving"
    second = memory.call("POST", path, 201, json={**stale, "previous_id": first["id"]})
    assert second["previous_id"] == first["id"]
    memory.call("POST", path, 422, json={"request_id": request_id(), "state": "complete", "value": 3, "comment": "x"})
    memory.act_as(MEMBER)
    other = {
        "request_id": request_id(),
        "state": "pending",
        "assignee_id": str(uuid.uuid4()),
        "previous_id": second["id"],
    }
    memory.call("POST", path, 403, json=other)
    memory.call("POST", f"/v1/evals/{run['id']}/items/{uuid.uuid4()}/reviews", 404, json=stale)


def test_a_review_queue_hands_results_to_a_reviewer_and_tracks_their_answers(memory: Served):
    run = memory.completed_run()
    cases = items_by_case(run)
    chosen = [cases["faq-1"]["id"], cases["faq-2"]["id"]]
    question = {"name": "helpful", "kind": "numeric", "instruction": "How helpful, 1 to 5?", "minimum": 1, "maximum": 5}
    body = {
        "request_id": request_id(),
        "run_id": run["id"],
        "name": "Week 46",
        "item_ids": chosen,
        "question": question,
    }
    queue = memory.call("POST", "/v1/evals/review-queues", 201, json=body)
    assert (queue["item_ids"], queue["question"]["name"]) == (chosen, "helpful")
    assert memory.call("POST", "/v1/evals/review-queues", 201, json=body) == queue
    memory.call("POST", "/v1/evals/review-queues", 409, json={**body, "request_id": request_id()})
    memory.call(
        "POST",
        "/v1/evals/review-queues",
        409,
        json={**body, "request_id": request_id(), "item_ids": [cases["faq-3"]["id"]]},
    )

    pending = memory.call("GET", "/v1/evals/reviews", 200, params={"queue_id": queue["id"], "filter": "pending"})
    assert {r["item_id"] for r in pending["items"]} == set(chosen)
    head = next(r for r in pending["items"] if r["item_id"] == chosen[0])
    answer = {
        "request_id": request_id(),
        "state": "complete",
        "queue_id": queue["id"],
        "previous_id": head["id"],
        "comment": "Good",
    }
    memory.call("POST", f"/v1/evals/{run['id']}/items/{chosen[0]}/reviews", 422, json={**answer, "value": 9})
    saved = memory.call("POST", f"/v1/evals/{run['id']}/items/{chosen[0]}/reviews", 201, json={**answer, "value": 4})
    assert (saved["criterion"], saved["queue_id"], saved["value"]) == ("helpful", queue["id"], 4)

    listed = memory.call("GET", "/v1/evals/review-queues", 200)
    (row,) = listed["items"]
    assert (row["answered"], row["completed"], row["total"], row["total_answers"]) == (1, 1, 2, 2)
    assert memory.call("POST", f"/v1/evals/review-queues/{queue['id']}/sync", 202)["id"] == queue["id"]
    work = memory.call("GET", f"/v1/evals/{run['id']}/workbench", 200)
    assert [q["id"] for q in work["queues"]] == [queue["id"]]

    memory.client.app.state.martian_evals_entitlements = lambda _p: COMMUNITY  # type: ignore[attr-defined]
    assert (
        memory.call("GET", "/v1/evals/review-queues", 403)["detail"]
        == "The Community edition does not include review queues"
    )
    memory.call("POST", "/v1/evals/review-queues", 403, json={**body, "request_id": request_id()})
    memory.call("POST", f"/v1/evals/{run['id']}/items/{chosen[1]}/reviews", 403, json={**answer, "value": 4})


def test_the_question_library_keeps_one_definition_per_name(memory: Served):
    question = {"name": "tone", "kind": "categorical", "instruction": "Tone?", "options": ["warm", "cold"]}
    saved = memory.call("POST", "/v1/evals/review-questions", 201, json={"question": question})
    assert (saved["version"], saved["question"]["options"], saved["question"]["minimum"]) == (1, ["warm", "cold"], 0)
    assert memory.call("POST", "/v1/evals/review-questions", 201, json={"question": question}) == saved
    changed = {**question, "options": ["warm", "cold", "neutral"]}
    memory.call("POST", "/v1/evals/review-questions", 409, json={"question": changed})
    memory.call("POST", "/v1/evals/review-questions", 409, json={"question": changed, "expected_version": 1})
    assert memory.call("GET", "/v1/evals/review-questions", 200)["items"] == [saved]


def test_a_trace_annotation_is_written_once_per_request(memory: Served):
    run = memory.completed_run()
    trace_id = items_by_case(run)["faq-1"]["evals_trace_id"]
    body = {"request_id": request_id(), "kind": "feedback", "name": "on_topic", "value": True, "rationale": "Yes"}
    first = memory.call("POST", f"/v1/evals/traces/{trace_id}/annotations", 201, json=body)
    assert (first["assessment_name"], first["feedback"]["value"], first["source"]["source_type"]) == (
        "on_topic",
        True,
        "HUMAN",
    )
    assert memory.call("POST", f"/v1/evals/traces/{trace_id}/annotations", 201, json=body) == first
    memory.call("POST", f"/v1/evals/traces/{trace_id}/annotations", 409, json={**body, "value": False})
    expectation = {"request_id": request_id(), "kind": "expectation", "name": "ideal", "value": "Use the link."}
    assert memory.call("POST", f"/v1/evals/traces/{trace_id}/annotations", 201, json=expectation)["expectation"] == {
        "value": "Use the link."
    }
    memory.call("POST", f"/v1/evals/traces/tr-{'0' * 32}/annotations", 404, json=body)
    detail = memory.call("GET", f"/v1/evals/traces/{trace_id}", 200)
    assert {a["assessment_name"] for a in detail["info"]["assessments"]} >= {"on_topic", "ideal"}


def test_an_expectation_reads_as_an_expectation_and_is_no_verdict(memory: Served):
    run = memory.completed_run()
    trace_id = items_by_case(run)["faq-1"]["evals_trace_id"]
    for kind, name, value in (("feedback", "on_topic", True), ("expectation", "ideal", "Use the link.")):
        body = {"request_id": request_id(), "kind": kind, "name": name, "value": value}
        memory.call("POST", f"/v1/evals/traces/{trace_id}/annotations", 201, json=body)
    info = memory.call("GET", f"/v1/evals/traces/{trace_id}", 200)["info"]
    found = {a["assessment_name"]: a for a in info["assessments"]}
    assert (found["ideal"]["expectation"], "feedback" in found["ideal"]) == ({"value": "Use the link."}, False)
    assert (found["on_topic"]["feedback"], "expectation" in found["on_topic"]) == ({"value": True}, False)
    # The Traces page lists verdicts: feedback, never an expectation.
    (row,) = [r for r in memory.call("POST", "/v1/evals/traces/search", 200, json={})["items"] if r["id"] == trace_id]
    assert "on_topic" in {a["name"] for a in row["assessments"]}
    assert "ideal" not in {a["name"] for a in row["assessments"]}


# --- projects, views and datasets ---------------------------------------------------------------


def test_projects_are_created_once_and_archived_when_idle(memory: Served):
    body = {"request_id": request_id(), "name": "Checkout", "description": "The checkout agent"}
    project = memory.call("POST", "/v1/evals/projects", 201, json=body)
    assert memory.call("POST", "/v1/evals/projects", 201, json=body) == project
    memory.call("POST", "/v1/evals/projects", 409, json={**body, "name": "Other"})
    run = memory.launch(project_id=project["id"])
    assert run["project_id"] == project["id"]
    memory.drain()
    archived = memory.call("POST", f"/v1/evals/projects/{project['id']}/archive", 200)
    assert archived["state"] == "archived"
    memory.call("POST", f"/v1/evals/projects/{uuid.uuid4()}/archive", 404)


def test_saved_views_belong_to_their_owner_unless_shared(memory: Served):
    memory.act_as(MEMBER)
    body = {"request_id": request_id(), "name": "Slow", "query": {"min_latency_ms": 500, "order": "slowest"}}
    view = memory.call("POST", "/v1/evals/views", 201, json=body)
    assert (view["revision"], view["owner_id"], view["query"]["order"]) == (1, MEMBER.user_id, "slowest")
    assert memory.call("POST", "/v1/evals/views", 201, json=body) == view
    other = organisation_principal("member-2", "org-a", frozenset({"member"}))
    memory.act_as(other)
    assert memory.call("GET", "/v1/evals/views", 200)["items"] == []
    memory.act_as(MEMBER)
    shared = memory.call(
        "PUT", f"/v1/evals/views/{view['id']}", 200, json={**body, "visibility": "organization", "revision": 1}
    )
    assert shared["revision"] == 2
    memory.call("PUT", f"/v1/evals/views/{view['id']}", 409, json={**body, "revision": 1})
    memory.act_as(other)
    assert [v["id"] for v in memory.call("GET", "/v1/evals/views", 200)["items"]] == [view["id"]]
    memory.call("DELETE", f"/v1/evals/views/{view['id']}", 403, params={"revision": 2})
    memory.act_as(ADMIN)
    memory.call("DELETE", f"/v1/evals/views/{view['id']}", 409, params={"revision": 1})
    memory.call("DELETE", f"/v1/evals/views/{view['id']}", 204, params={"revision": 2})
    assert memory.call("GET", "/v1/evals/views", 200)["items"] == []


async def test_an_upload_length_that_is_not_the_digits_0_to_9_is_left_to_the_upload_check():
    # A header is Latin-1, so ², which isdigit() accepts and int() refuses, can arrive as a length.
    assert await _limit_body(Request({"type": "http", "headers": [(b"content-length", b"\xb2")]})) is None
    oversized = Request({"type": "http", "headers": [(b"content-length", str(MAX_UPLOAD_BYTES * 2 + 1).encode())]})
    with pytest.raises(HTTPException) as refused:
        await _limit_body(oversized)
    assert refused.value.status_code == 413


def publish_draft(served: Served, name: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    draft = served.call("POST", "/v1/evals/dataset-drafts", 201, json={"name": name})
    path, revision = f"/v1/evals/dataset-drafts/{draft['id']}", draft["revision"]
    for record in records:
        revision = served.call("POST", f"{path}/records", 201, json={"base_revision": revision, "record": record})[
            "revision"
        ]
    checked = served.call("GET", f"{path}/validation", 200)
    body = {"base_revision": revision, "expected_sha256": checked["sha256"], "rationale": "Pinned release"}
    return served.call("POST", f"{path}/publish", 202, json=body)


def test_a_published_version_runs_only_while_the_store_holds_what_was_published(memory: Served):
    publish_draft(
        memory,
        "pinned",
        [
            {"id": "r1", "input": "Refund?", "expected_output": "Yes."},
            {"id": "r2", "input": "When?", "expected_output": "Soon."},
        ],
    )
    (manifest,) = asyncio.run(memory.records.list_publications(ADMIN, DEFAULT_PROJECT))
    assert (manifest.state, manifest.name, manifest.case_ids, manifest.draft_id is not None) == (
        "published",
        "pinned",
        ["r1", "r2"],
        True,
    )
    run = memory.launch(dataset_name="pinned")
    memory.drain()
    assert memory.call("GET", f"/v1/evals/{run['id']}", 200)["state"] == "completed"
    # The version is changed in the store after it was published: a run of it never starts.
    key = next(k for k, (d, _cases) in memory.store._datasets.items() if d.name == "pinned")  # type: ignore[attr-defined]
    dataset, cases = memory.store._datasets[key]  # type: ignore[attr-defined]
    edited = cases[0].model_copy(update={"expectations": {"expected_response": "No."}})
    memory.store._datasets[key] = (dataset, [edited, *cases[1:]])  # type: ignore[attr-defined]
    again = memory.launch(dataset_name="pinned")
    memory.drain()
    failed = memory.call("GET", f"/v1/evals/{again['id']}", 200)
    assert (failed["state"], failed["error"], failed["items"]) == ("failed", CHANGED, [])


def test_new_runs_wait_while_a_dataset_is_being_published(memory: Served):
    cases = [Case(id="c1", inputs={"message": "hi"})]
    stale = manifest_for("p-stale", DEFAULT_PROJECT, "old", cases).model_copy(
        update={"started_at": datetime.now(UTC) - timedelta(minutes=11)}
    )
    asyncio.run(memory.records.begin_publication(ADMIN, DEFAULT_PROJECT, stale))
    memory.launch()  # a publication past its lease no longer holds runs back
    memory.drain()
    asyncio.run(
        memory.records.begin_publication(ADMIN, DEFAULT_PROJECT, manifest_for("p-now", DEFAULT_PROJECT, "new", cases))
    )
    refused = memory.call(
        "POST", "/v1/evals", 409, json={"agent_id": AGENT, "dataset_name": "support-faq", "request_id": request_id()}
    )
    assert refused == {"detail": PAUSED}


def test_a_draft_is_edited_published_and_run(memory: Served):
    draft = memory.call("POST", "/v1/evals/dataset-drafts", 201, json={"name": "Refunds"})
    assert (draft["revision"], draft["record_count"], draft["state"], draft["scope"]) == (1, 0, "editing", "org")
    path = f"/v1/evals/dataset-drafts/{draft['id']}"
    added = memory.call(
        "POST",
        f"{path}/records",
        201,
        json={"base_revision": 1, "record": {"id": "r1", "input": "Refund?", "expected_output": "Yes."}},
    )
    assert (added["revision"], added["record"]["usable"], added["record"]["input"]) == (2, True, "Refund?")
    memory.call("POST", f"{path}/records", 409, json={"base_revision": 1, "record": {"input": "stale"}})
    upload = (
        '{"id": "r2", "input": {"question": "When?"}, "expected_output": "Within a week."}\n{"input": "No answer"}\n'
    )
    dry = memory.call(
        "POST",
        f"{path}/records/merge",
        200,
        json={"base_revision": 2, "format": "jsonl", "content": upload, "dry_run": True},
    )
    assert (dry["inserted"], dry["generated_ids"], dry["revision"], dry["record_count"]) == (2, 1, 2, 3)
    merged = memory.call(
        "POST", f"{path}/records/merge", 200, json={"base_revision": 2, "format": "jsonl", "content": upload}
    )
    assert (merged["revision"], merged["record_count"]) == (3, 3)
    memory.call(
        "POST", f"{path}/records/merge", 422, json={"base_revision": 3, "format": "csv", "content": "question\nx\n"}
    )
    edited = memory.call(
        "PATCH", f"{path}/records/r1", 200, json={"base_revision": 3, "changes": {"expected_output": "Yes, in full."}}
    )
    assert edited["record"]["expected_output"] == "Yes, in full." and edited["record"]["ordinal"] == 0
    page = memory.call("GET", f"{path}/records", 200, params={"q": "week"})
    assert [r["id"] for r in page["items"]] == ["r2"] and page["items"][0]["input"] == {"question": "When?"}
    checked = memory.call("GET", f"{path}/validation", 200)
    assert (checked["record_count"], checked["usable"], checked["missing_expected_output"]) == (3, 2, 1)
    renamed = memory.call("PATCH", path, 200, json={"base_revision": 4, "name": "refunds-v2"})
    memory.call("PATCH", path, 422, json={"base_revision": 5, "name": "martian:refunds"})
    memory.call(
        "POST", f"{path}/publish", 409, json={"base_revision": 5, "expected_sha256": "0" * 64, "rationale": "First cut"}
    )
    checked = memory.call("GET", f"{path}/validation", 200)
    published = memory.call(
        "POST",
        f"{path}/publish",
        202,
        json={"base_revision": renamed["revision"], "expected_sha256": checked["sha256"], "rationale": "First cut"},
    )
    assert published["state"] == "published"
    detail = memory.call("GET", path, 200)
    assert (detail["state"], detail["published_sha256"], detail["operation_id"]) == (
        "published",
        checked["sha256"],
        published["operation_id"],
    )
    memory.call("POST", f"{path}/records", 409, json={"base_revision": detail["revision"], "record": {"input": "late"}})
    dataset = memory.call("GET", "/v1/evals/datasets/refunds-v2", 200)
    assert (dataset["item_count"], dataset["version"]) == (3, checked["sha256"])
    operation = memory.call("GET", f"/v1/evals/dataset-operations/{published['operation_id']}", 200)
    assert (operation["state"], operation["draft_id"], operation["sha256"]) == (
        "published",
        draft["id"],
        checked["sha256"],
    )
    memory.call(
        "POST",
        f"/v1/evals/dataset-operations/{published['operation_id']}/resume",
        409,
        json={"rationale": "Try again please"},
    )
    run = memory.launch(dataset_name="refunds-v2")
    memory.drain()
    finished = memory.call("GET", f"/v1/evals/{run['id']}", 200)
    assert (finished["state"], finished["failed"], finished["skipped"]) == ("completed", 2, 1)

    copy = memory.call("POST", "/v1/evals/dataset-drafts", 201, json={"from_dataset": "refunds-v2"})
    assert (copy["name"], copy["record_count"], copy["source_dataset"]) == ("refunds-v2", 3, "refunds-v2")
    copied = memory.call("GET", f"/v1/evals/dataset-drafts/{copy['id']}/records", 200)
    assert [r["input"] for r in copied["items"]] == ["Refund?", {"question": "When?"}, "No answer"]
    imported = memory.call(
        "POST",
        f"/v1/evals/dataset-drafts/{copy['id']}/records/from-eval-run",
        200,
        json={
            "base_revision": copy["revision"],
            "eval_run_id": run["id"],
            "item_result_ids": [finished["items"][0]["id"]],
            "expected_from": "actual",
        },
    )
    assert (imported["inserted"], imported["record_count"]) == (1, 4)
    removed = memory.call(
        "POST",
        f"/v1/evals/dataset-drafts/{copy['id']}/records/delete",
        200,
        json={"base_revision": imported["revision"], "item_ids": ["r1", "missing"]},
    )
    assert (removed["deleted"], removed["record_count"]) == (1, 3)
    assert [
        d["id"] for d in memory.call("GET", "/v1/evals/dataset-drafts", 200, params={"include_published": False})
    ] == [copy["id"]]
    memory.call("DELETE", f"/v1/evals/dataset-drafts/{copy['id']}", 204)
    memory.call("GET", f"/v1/evals/dataset-drafts/{copy['id']}", 404)
