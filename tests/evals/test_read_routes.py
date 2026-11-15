"""The core's read routes serve runs, results, comparisons, traces and datasets from the store and
the record store, as the server's routes serve them from its tables and MLflow. Each test runs on
MemoryStore and on the in-process MLflow binding."""

import asyncio
import base64
import json
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from martian.evals import local_runs
from martian.evals.local import local_entitlements
from martian.evals.model import CaseResult, Counts, Project, Run, StoreRef, Target, Trace
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, NO_AGENT, SINGLE_USER, create_app, organisation_principal, result_id
from martian.evals.server.launch import run_id_for
from martian.evals.stats import wilson
from martian.evals.store import MemoryStore
from martian.evals.store.mlflow_local import MlflowStore
from martian.evals.trace_conventions import MAPPING_KEY, MAPPING_VERSION
from tests.evals.read_fixture import T0, seed, trace_id
from tests.evals.trace_samples import ARGUMENTS, MODEL, QUESTION, RESULT, STYLES, sample_trace
from tests.evals.write_fixture import AGENT, Inert

ORG = organisation_principal("user-1", "org-a", frozenset({"member"}), display_name="Ada")


@pytest.fixture(params=["memory", "mlflow"])
def served(request, tmp_path):
    """The seeded routes on the reference store and on the in-process MLflow binding."""
    store = MemoryStore() if request.param == "memory" else MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path)
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    seeded = asyncio.run(seed(store, records, SINGLE_USER))
    yield TestClient(create_app(store=store, records=records, principal=lambda: SINGLE_USER)), seeded
    records.close()


def search(client, **query):
    response = client.post("/v1/evals/traces/search", json=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_core_needs_a_record_store(tmp_path):
    with pytest.raises(TypeError, match="RecordStore"):
        create_app(store=MemoryStore(), records=object(), principal=lambda: SINGLE_USER)  # type: ignore[arg-type]


def test_projects_list_the_default_project_first(served):
    client, seeded = served
    items = client.get("/v1/evals/projects").json()["items"]
    assert [(p["id"], p["name"], p["state"]) for p in items] == [
        (None, "Default", "ready"),
        (seeded.project_id, "Checkout agent", "ready"),
    ]


def test_runs_are_listed_newest_first_by_project(served):
    client, seeded = served
    codes = lambda **params: [r["run_code"] for r in client.get("/v1/evals", params=params).json()]  # noqa: E731
    assert codes(default_only=True) == ["EVAL-2", "EVAL-1"]
    assert codes(project_id=seeded.project_id) == ["EVAL-3"]
    assert codes() == ["EVAL-3", "EVAL-2", "EVAL-1"]
    assert codes(q="eval-1") == ["EVAL-1"] and codes(q="support") == ["EVAL-3", "EVAL-2", "EVAL-1"]
    assert codes(page=2, limit=2) == ["EVAL-1"]
    assert client.get("/v1/evals", params={"project_id": str(uuid.uuid4())}).status_code == 404


def test_an_editions_agent_names_name_its_runs(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    agent = str(uuid.uuid4())
    run = Run(
        id=str(uuid.uuid4()),
        project_id=DEFAULT_PROJECT,
        code="EV-1",
        source="ci",
        state="completed",
        target=Target(kind="agent", agent_id=agent),
        provenance={"ci": {"git_sha": "abc123", "repo": "acme/app"}, "dataset": {"id": "d-1"}},
        created_at=T0,
    )
    asyncio.run(records.create_run(ORG, run))
    asked = []

    async def names(principal, ids):
        asked.append((principal.organisation_id, ids))
        return {agent: "Support bot"}

    named = TestClient(create_app(store=MemoryStore(), records=records, principal=lambda: ORG, agent_names=names))
    listed = named.get("/v1/evals", params={"default_only": True}).json()
    assert [(r["agent_id"], r["agent_name"], r["ci"]["git_sha"], r["ci"]["repo"]) for r in listed] == [
        (agent, "Support bot", "abc123", "acme/app")
    ]
    assert named.get(f"/v1/evals/{run.id}").json()["agent_name"] == "Support bot"
    assert asked == [("org-a", [agent])] * 2
    # Without a lookup the run carries no agent name.
    plain = TestClient(create_app(store=MemoryStore(), records=records, principal=lambda: ORG))
    assert plain.get(f"/v1/evals/{run.id}").json()["agent_name"] is None
    records.close()


def test_a_run_in_the_default_project_reports_no_project_and_no_agent(served):
    client, seeded = served
    run = client.get(f"/v1/evals/{seeded.candidate.id}").json()
    assert (run["project_id"], run["agent_id"], run["agent_name"], run["source"]) == (
        None,
        str(NO_AGENT),
        None,
        "local",
    )
    assert (run["passed"], run["failed"], run["errored"], run["planned_count"], run["max_items"]) == (2, 0, 1, 3, 3)
    assert run["dataset_id"] == seeded.candidate.dataset_digest
    assert client.get(f"/v1/evals/{seeded.running.id}").json()["project_id"] == seeded.project_id
    assert client.get(f"/v1/evals/{uuid.uuid4()}").status_code == 404
    # Only a UUID is a run id, so another path under /v1/evals is not taken for a run.
    assert client.get("/v1/evals/not-a-run").status_code == 404


def test_run_detail_shows_each_case_result_as_the_server_does(served):
    client, seeded = served
    items = client.get(f"/v1/evals/{seeded.candidate.id}").json()["items"]
    assert [i["dataset_item_id"] for i in items] == ["faq-1", "faq-2", "faq-3"]
    first, _, last = items
    assert first["id"] == str(result_id(seeded.candidate.id, "faq-1"))
    assert (first["prompt"], first["expected"], first["output"]) == (
        "How do I reset my password?",
        "Use the reset link on the sign-in page.",
        "Use the reset link on the sign-in page.",
    )
    # faq-3's inputs hold its text under "question"; the error result has no output.
    assert (last["prompt"], last["status"], last["output"], last["error"]) == (
        "Where is my invoice?",
        "error",
        "",
        "target timed out",
    )
    assert last["evals_trace_id"] == seeded.traces[(seeded.candidate.id, "faq-3")]


def test_another_organisation_sees_none_of_it(served, tmp_path):
    client, seeded = served
    other = TestClient(create_app(store=seeded.store, records=seeded.records, principal=lambda: ORG))
    assert other.get("/v1/evals").json() == []
    assert other.get(f"/v1/evals/{seeded.candidate.id}").status_code == 404
    assert other.get("/v1/evals/datasets").json() == []
    assert search(other)["items"] == []
    trace_id = seeded.traces[(seeded.candidate.id, "faq-1")]
    assert other.get(f"/v1/evals/traces/{trace_id}").status_code == 404


def test_the_workbench_shows_the_newest_scoring_job_and_its_scores(served):
    client, seeded = served
    work = client.get(f"/v1/evals/{seeded.candidate.id}/workbench").json()
    assert work["selected_job"]["id"] == seeded.candidate_job.id
    assert work["selected_job"]["config"]["scorers"] == ["correctness"]
    assert sorted(str(a["value"]) for a in work["assessments"]) == ["None", "True", "True"]
    assert {a["source"] for a in work["assessments"]} == {"LLM_JUDGE"}
    card = work["scorecard"]["criteria"][0]
    assert (card["name"], card["kind"], card["evaluated"], card["errors"], card["pass_rate"]) == (
        "correctness",
        "boolean",
        2,
        1,
        1.0,
    )
    # The Wilson interval of 2 of 2; a boolean scorer has no mean to give an interval for.
    assert card["confidence_95"] == list(wilson(2, 2)) and card["mean_confidence_95"] is None
    assert [r["id"] for r in work["latest_reviews"]] == ["review-1"]
    assert work["calibration"][0]["paired"] == 1 and work["calibration"][0]["agreement"] == 1.0
    assert work["members"] == [{"id": "bf0808be-f788-5dd7-8fc2-2d28ed350045", "name": "local", "role": "owner"}]


def test_the_workbench_filters_by_result_and_refuses_another_runs_job(served):
    client, seeded = served
    item = str(result_id(seeded.candidate.id, "faq-1"))
    work = client.get(f"/v1/evals/{seeded.candidate.id}/workbench", params={"item_id": item}).json()
    # Every score of that result: the run's own exact-match check and the judge's.
    assert sorted(a["name"] for a in work["assessments"]) == ["correctness", "exact_match"]
    assert all(a["item_id"] == item for a in work["assessments"])
    assert work["assessments"][0]["telemetry_state"] in ("persisted", "unavailable")
    refused = client.get(f"/v1/evals/{seeded.candidate.id}/workbench", params={"job_id": seeded.baseline_job.id})
    assert refused.status_code == 404


def test_comparing_two_runs_pairs_their_cases(served):
    client, seeded = served
    pair = {"baseline_id": seeded.baseline.id, "candidate_id": seeded.candidate.id}
    result = client.get("/v1/evals/compare", params=pair).json()
    assert result["compatible"] and result["coverage"]["paired"] == 3
    assert [i["change"] for i in result["items"]] == ["unchanged", "improved", "not_comparable"]
    assert result["changes"] == {"improved": 1, "regressed": 0, "unchanged": 1, "not_comparable": 1}
    # Two comparable pairs and one change: far too few to tell from noise.
    found = result["significance"]
    assert (found["label"], found["test"], found["compared"], found["improved"], found["p_value"]) == (
        "within_noise",
        "mcnemar_exact",
        2,
        1,
        1.0,
    )
    assert found["minimum_detectable_effect"] is None
    with_jobs = {**pair, "baseline_job_id": seeded.baseline_job.id, "candidate_job_id": seeded.candidate_job.id}
    scored = client.get("/v1/evals/compare", params=with_jobs).json()
    assert scored["criteria"] == {"correctness": {"unchanged": 1, "improved": 1, "not_comparable": 1}}
    assert scored["items"][1]["criteria"]["correctness"]["delta"] == 1.0
    judged = scored["criteria_significance"]["correctness"]
    assert (judged["label"], judged["test"], judged["compared"], judged["improved"]) == (
        "within_noise",
        "mcnemar_exact",
        2,
        1,
    )
    one_job = {**pair, "baseline_job_id": seeded.baseline_job.id}
    assert client.get("/v1/evals/compare", params=one_job).status_code == 422


def test_runs_of_different_datasets_are_not_comparable(served):
    client, seeded = served
    other = seeded.baseline.model_copy(update={"id": str(uuid.uuid4()), "dataset_digest": "0" * 64})
    asyncio.run(seeded.records.create_run(SINGLE_USER, other))
    result = client.get(
        "/v1/evals/compare", params={"baseline_id": other.id, "candidate_id": seeded.candidate.id}
    ).json()
    assert not result["compatible"] and "different dataset identities" in result["reason"]
    assert result["significance"] is None
    running = client.get(
        "/v1/evals/compare", params={"baseline_id": seeded.running.id, "candidate_id": seeded.candidate.id}
    )
    assert running.json()["reason"] == "Both runs must be completed."


def test_a_case_results_trace_shows_its_spans_and_assessments(served):
    client, seeded = served
    item = result_id(seeded.candidate.id, "faq-2")
    trace = client.get(f"/v1/evals/{seeded.candidate.id}/items/{item}/trace").json()
    assert trace["trace_id"] == seeded.traces[(seeded.candidate.id, "faq-2")]
    assert [(s["name"], s["depth"], s["duration_ms"], s["offset_ms"]) for s in trace["spans"]] == [
        ("answer", 0, 130, 0),
        ("model call", 1, 90, 10),
    ]
    assert trace["spans"][1]["model"] == "gpt-test" and trace["spans"][0]["status"] == {"code": "STATUS_CODE_OK"}
    assert trace["token_usage"] == {"total_tokens": 42}
    (annotation,) = trace["annotations"]
    assert annotation["assessment_name"] == "exact_match" and annotation["source"]["source_type"] == "CODE"
    elsewhere = result_id(seeded.baseline.id, "faq-2")
    assert client.get(f"/v1/evals/{seeded.candidate.id}/items/{elsewhere}/trace").status_code == 404


def test_provenance_names_the_runs_record_in_the_store(served):
    client, seeded = served
    found = client.get(f"/v1/evals/{seeded.candidate.id}/provenance").json()
    assert found["native"]["status"] == "FINISHED"
    assert found["native"]["datasets"] == [{"name": "support-faq", "digest": seeded.candidate.dataset_digest}]
    assert {m["key"]: m["value"] for m in found["native"]["metrics"]}["errors"] == 1.0
    unrecorded = client.get(f"/v1/evals/{seeded.running.id}/provenance").json()
    assert unrecorded["native"] is None and "has not been persisted" in unrecorded["note"]


# A CI run's attribution as the server's CI gate records it: what the console shows, then the token
# that uploaded the run and the digests of the request and its items, which only the server reads.
CI = {
    "git_sha": "0123456789abcdef0123456789abcdef01234567",
    "ref": "refs/heads/main",
    "repo": "acme/app",
    "run_url": "https://github.com/acme/app/actions/runs/1",
    "kit_version": "1",
    "threshold": 0.9,
    "minimum_coverage": 1.0,
}
CI_TOKEN = {
    "token_id": str(uuid.uuid4()),
    "token_name": "canary-ci-token-name",
    "request_digest": "ca" * 32,
    "items_digest": "fe" * 32,
}
# A console run as the server admits it, pinned with what its executor keeps of the agent's approval.
ADMISSION = {
    "dataset": {"id": "ds-1", "digest": "d1" * 32, "content_sha256": None},
    "agent": {"id": AGENT, "artifact_revision": "v7", "revision_source": "customer_declared"},
    "model_version": None,
    "prompt_version": None,
}
APPROVAL = {"approval_id": "canary-connection-approval"}
NEVER_SHOWN = (*CI_TOKEN, *CI_TOKEN.values(), *APPROVAL, *APPROVAL.values(), "canary-server-only")
NO_RUN = {
    # The routes under a run that answer with a scoring job, an assessment or a review, never a run.
    ("post", "/v1/evals/{run_id}/scores"),
    ("post", "/v1/evals/{run_id}/scores/{job_id}/cancel"),
    ("post", "/v1/evals/{run_id}/assessments/{assessment_id}/sync"),
    ("post", "/v1/evals/{run_id}/items/{item_id}/reviews"),
}


def test_no_run_route_shows_the_ci_token_or_what_else_only_the_server_keeps(tmp_path):
    # MartianSecurity/martian-notes#352. The CI run, a console run whose stop was asked and a run of it
    # again, read through every route that answers with a run or what a run holds, the core's and
    # the local edition's. The record store keeps all of their provenance.
    store, records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    launched, again = uuid.uuid4(), uuid.uuid4()
    ci = Run(
        id=run_id_for(launched),
        project_id=DEFAULT_PROJECT,
        code="EV-1001",
        source="ci",
        state="completed",
        dataset_name="support-faq",
        dataset_digest="d1" * 32,
        target=Target(kind="agent", agent_id=AGENT),
        counts=Counts(cases=1, passed=1),
        max_cases=1,
        provenance={"ci": {**CI, **CI_TOKEN}, "server": "canary-server-only"},
        requested_by=SINGLE_USER.user_id,
        created_at=T0,
        started_at=T0,
        finished_at=T0,
    )
    pinned = {**ADMISSION, "target": {"kind": "agent", "agent_id": AGENT, **APPROVAL}}
    stopping = ci.model_copy(
        update={
            "id": str(uuid.uuid4()),
            "code": "EV-1002",
            "source": "console",
            "state": "running",
            "finished_at": None,
            "cancel_requested_at": T0,
            "provenance": pinned,
        }
    )
    repeated = stopping.model_copy(
        update={
            "id": run_id_for(again),
            "code": "EV-1003",
            "state": "completed",
            "finished_at": T0,
            "cancel_requested_at": None,
            "provenance": {**pinned, "repeat_of": ci.id},
        }
    )
    # A console run that stopped at one of its limits, which the local edition resumes.
    resumable = stopping.model_copy(
        update={"id": str(uuid.uuid4()), "code": "EV-1004", "state": "stopped", "cancel_requested_at": None}
    )
    traced = trace_id()

    async def record() -> None:
        await store.create_project(SINGLE_USER, Project(id=DEFAULT_PROJECT, name="Default"))
        for run in (ci, stopping, repeated, resumable):
            await records.create_run(SINGLE_USER, run)
        await store.start_run(SINGLE_USER, ci)
        await store.record_trace(SINGLE_USER, Trace(id=traced, project_id=DEFAULT_PROJECT, state="ok", run_id=ci.id))
        result = CaseResult(
            run_id=ci.id, case_id="faq-1", status="passed", output="Hi", store=StoreRef(trace_id=traced)
        )
        await records.put_results(SINGLE_USER, DEFAULT_PROJECT, [result])

    asyncio.run(record())
    # Room for the resumed run beside the one still running.
    entitlements = local_entitlements(concurrency=5)
    app = create_app(
        store=store,
        records=records,
        principal=lambda: SINGLE_USER,
        entitlements=lambda _p: entitlements,
        worker=Inert(),
    )
    app.include_router(local_runs.router)
    client = TestClient(app)
    replay = {"agent_id": AGENT, "dataset_name": "support-faq", "max_items": 1}
    calls = [
        ("get", "/v1/evals", "/v1/evals", {}),
        ("post", "/v1/evals", "/v1/evals", {"json": {**replay, "request_id": str(launched)}}),
        ("get", "/v1/evals/compare", "/v1/evals/compare", {"params": {"baseline_id": ci.id, "candidate_id": ci.id}}),
        ("get", "/v1/evals/{run_id}", f"/v1/evals/{ci.id}", {}),
        ("get", "/v1/evals/{run_id}/attempts", f"/v1/evals/{ci.id}/attempts", {}),
        ("get", "/v1/evals/{run_id}/turns", f"/v1/evals/{ci.id}/turns", {}),
        ("get", "/v1/evals/{run_id}/workbench", f"/v1/evals/{ci.id}/workbench", {}),
        ("get", "/v1/evals/{run_id}/provenance", f"/v1/evals/{ci.id}/provenance", {}),
        (
            "get",
            "/v1/evals/{run_id}/items/{item_id}/trace",
            f"/v1/evals/{ci.id}/items/{result_id(ci.id, 'faq-1')}/trace",
            {},
        ),
        ("post", "/v1/evals/{run_id}/cancel", f"/v1/evals/{stopping.id}/cancel", {}),
        ("post", "/v1/local/runs", "/v1/local/runs", {"json": {**replay, "request_id": str(launched)}}),
        (
            "post",
            "/v1/local/runs/{run_id}/again",
            f"/v1/local/runs/{ci.id}/again",
            {"json": {"request_id": str(again)}},
        ),
        ("post", "/v1/local/runs/{run_id}/resume", f"/v1/local/runs/{resumable.id}/resume", {"json": {}}),
    ]
    shown = {}
    for method, route, path, options in calls:
        response = client.request(method, path, **options)
        assert response.status_code in (200, 202), (route, response.text)
        assert [value for value in NEVER_SHOWN if value in response.text] == [], route
        shown[(method, route)] = response.json()
    # Every route that names a run, lists runs or compares them is above, or answers with no run.
    routes = {
        (method, path)
        for path, operations in app.openapi()["paths"].items()
        if path in ("/v1/evals", "/v1/evals/compare") or path.startswith(("/v1/evals/{run_id}", "/v1/local/runs"))
        for method in operations
    }
    assert routes == set(shown) | NO_RUN

    # What the console shows of each is unchanged: the CI run's public attribution as `ci`, and the
    # console run's admission with the target it ran.
    detail = shown[("get", "/v1/evals/{run_id}")]
    assert (detail["source"], detail["ci"], detail["provenance"]) == ("ci", CI, {})
    assert shown[("get", "/v1/evals/{run_id}/provenance")]["admission"] == {}
    target = {"kind": "agent", "agent_id": AGENT}
    assert shown[("post", "/v1/evals/{run_id}/cancel")]["provenance"] == {**ADMISSION, "target": target}
    assert shown[("post", "/v1/local/runs/{run_id}/again")]["provenance"] == {
        **ADMISSION,
        "target": target,
        "repeat_of": ci.id,
    }
    kept = asyncio.run(records.get_run(SINGLE_USER, DEFAULT_PROJECT, ci.id))
    assert kept is not None and kept.provenance == ci.provenance
    records.close()


def test_trace_search_filters_and_pages(served):
    client, seeded = served
    everything = search(client, limit=100)["items"]
    assert len(everything) == 7 and everything[0]["id"] == seeded.live_trace
    assert [t["name"] for t in search(client, q="opening hours")["items"]] == ["chat turn"]
    assert {t["state"] for t in search(client, state="ERROR")["items"]} == {"ERROR"}
    assert [t["id"] for t in search(client, session_id="session-7")["items"]] == [seeded.live_trace]
    assert [t["id"] for t in search(client, user_id="customer-3")["items"]] == [seeded.live_trace]
    assert [t["latency_ms"] for t in search(client, min_latency_ms=500)["items"]] == [900.0]
    failing = search(client, assessment="exact_match", assessment_value="false")["items"]
    assert len(failing) == 3 and all(t["assessments"][0]["value"] is False for t in failing)
    assert search(client, order="oldest", limit=1)["items"][0]["name"] == "EVAL-1 faq-1"
    assert [t["latency_ms"] for t in search(client, order="slowest", limit=3)["items"]] == [900.0, 140.0, 140.0]
    start = everything[-1]["timestamp_ms"]
    assert len(search(client, start_time_ms=start, end_time_ms=start)["items"]) == 1


def test_sessions_group_the_traces_a_search_matches(served):
    client, seeded = served
    for minute, (state, tokens) in enumerate((("ok", 10), ("error", None))):
        turn = Trace(
            id="tr-" + uuid.uuid4().hex,
            project_id=DEFAULT_PROJECT,
            name="support chat",
            state=state,
            request_time=T0 + timedelta(days=2, minutes=minute),
            latency_ms=300.0,
            request=f"turn {minute + 1}",
            session_id="session-9",
            user_id="customer-5",
            tokens=tokens,
        )
        asyncio.run(seeded.store.record_trace(SINGLE_USER, turn))
    sessions = lambda **query: client.post("/v1/evals/sessions/search", json=query).json()  # noqa: E731
    found = sessions()
    assert (found["traces_examined"], found["ungrouped"], found["has_more"]) == (9, 6, False)
    newest, older = found["items"]
    assert (newest["id"], newest["turns"], newest["errors"], newest["tokens"]) == ("session-9", 2, 1, 10)
    assert newest["preview"] == "turn 1" and newest["started_at"] < newest["last_at"]
    assert (older["id"], older["turns"], older["preview"]) == ("session-7", 1, "What are your opening hours?")
    assert [s["id"] for s in sessions(user_id="customer-3")["items"]] == ["session-7"]
    assert [s["id"] for s in sessions(order="oldest")["items"]] == ["session-7", "session-9"]
    # Filters the store does not have are applied by the core before grouping.
    assert [s["id"] for s in sessions(min_latency_ms=500)["items"]] == ["session-7"]
    assert [s["id"] for s in sessions(order="slowest")["items"]] == ["session-7", "session-9"]
    refused = client.post("/v1/evals/sessions/search", json={"cursor": "page-2"})
    assert refused.status_code == 422 and refused.json()["detail"] == "Start a fresh search for an aggregate or export"


@pytest.mark.parametrize(
    "query", [{}, {"user_id": ""}, {"assessment": "exact_match", "assessment_value": "true"}, {"order": "fastest"}]
)
def test_trace_pages_follow_their_cursor_to_the_end(served, query):
    client, _ = served
    expected = [t["id"] for t in search(client, **query, limit=100)["items"]]
    seen, cursor = [], None
    while True:
        page = search(client, **query, limit=2, cursor=cursor)
        assert len(page["items"]) <= 2
        seen += [t["id"] for t in page["items"]]
        if (cursor := page["next_cursor"]) is None:
            break
    assert seen == expected


def test_a_cursor_belongs_to_its_query(served):
    client, _ = served
    cursor = search(client, limit=2)["next_cursor"]
    assert cursor
    refused = client.post("/v1/evals/traces/search", json={"limit": 2, "q": "faq", "cursor": cursor})
    assert refused.status_code == 409 and "refresh the search" in refused.json()["detail"]
    assert client.post("/v1/evals/traces/search", json={"cursor": "not a cursor"}).status_code == 409


@pytest.mark.parametrize("page", ["²", "١"])
def test_a_cursor_naming_a_store_page_in_other_digits_is_refused(served, page):
    # The cursor's scope is a digest anyone can compute, so its store page is the caller's to set.
    client, _ = served
    position = json.loads(base64.urlsafe_b64decode(search(client, limit=2)["next_cursor"]))
    forged = base64.urlsafe_b64encode(json.dumps({**position, "page": page}).encode()).decode()
    refused = client.post("/v1/evals/traces/search", json={"limit": 2, "cursor": forged})
    assert (refused.status_code, refused.json()) == (422, {"detail": "cursor is not a next_cursor this store returned"})


def test_trace_search_in_a_project_that_does_not_exist(served, tmp_path):
    client, _ = served
    assert client.post("/v1/evals/traces/search", json={"project_id": str(uuid.uuid4())}).status_code == 404
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'empty.db'}")
    empty = TestClient(create_app(store=MemoryStore(), records=records, principal=lambda: SINGLE_USER))
    assert search(empty) == {"items": [], "next_cursor": None, "limit": 25}
    assert empty.post("/v1/evals/sessions/search", json={}).json()["items"] == []
    assert empty.get("/v1/evals/datasets").json() == [] and empty.get("/v1/evals").json() == []
    records.close()


@pytest.mark.parametrize("style", STYLES)
def test_a_trace_in_each_convention_shows_its_model_tokens_tool_calls_and_inputs(served, style):
    client, seeded = served
    trace_id = "tr-" + uuid.uuid4().hex
    sample = sample_trace(style, trace_id).model_copy(update={"request_time": T0 + timedelta(days=3)})
    asyncio.run(seeded.store.record_trace(SINGLE_USER, sample))
    (row,) = search(client, session_id="conversation-1")["items"]
    assert (row["id"], row["model"], row["tokens"], row["user_id"]) == (trace_id, MODEL, 20, "alice")
    assert QUESTION in row["request"]
    detail = client.get(f"/v1/evals/traces/{trace_id}").json()
    assert detail["info"]["trace_metadata"][MAPPING_KEY] == MAPPING_VERSION
    assert detail["token_usage"] == {"total_tokens": 20}
    agent, model_call, tool = detail["spans"]
    assert (agent["type"], model_call["type"], tool["type"]) == (
        "AGENT",
        "LLM" if style == "openinference" else "CHAT_MODEL",
        "TOOL",
    )
    assert model_call["model"] == MODEL
    assert model_call["token_usage"] == {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
    assert QUESTION in json.dumps(model_call["inputs"])
    # The tool call the model asked for is in its output messages, and the call is a tool span.
    assert "call_1" in json.dumps(model_call["outputs"]) and "order_status" in json.dumps(model_call["outputs"])
    assert "order_status" in tool["name"] and (tool["inputs"], tool["outputs"]) == (ARGUMENTS, RESULT)
    assert agent["attributes"]["acme.region"] == "eu-west"


def test_a_trace_mapped_with_a_version_this_release_does_not_know_is_refused(served):
    client, seeded = served
    trace_id = "tr-" + uuid.uuid4().hex
    newer = sample_trace("otel_genai", trace_id).model_copy(
        update={"metadata": {MAPPING_KEY: "martian-trace-mapping-v99"}}
    )
    asyncio.run(seeded.store.record_trace(SINGLE_USER, newer))
    refused = client.get(f"/v1/evals/traces/{trace_id}")
    assert refused.status_code == 409 and "martian-trace-mapping-v99" in refused.json()["detail"]


def test_a_trace_shows_its_info_and_downloads(served):
    client, seeded = served
    detail = client.get(f"/v1/evals/traces/{seeded.live_trace}").json()
    info = detail["info"]
    assert (info["trace_id"], info["state"], info["execution_duration"]) == (seeded.live_trace, "OK", "0.9s")
    assert info["trace_metadata"]["mlflow.trace.session"] == "session-7"
    assert info["trace_metadata"]["mlflow.trace.user"] == "customer-3"
    assert info["tags"]["mlflow.traceName"] == "chat turn"
    assert info["assessments"][0]["feedback"] == {"value": True}
    assert detail["spans"] == [] and detail["estimated_cost"] == {"total_cost": 0.002}
    download = client.get(f"/v1/evals/traces/{seeded.live_trace}/payload")
    assert download.headers["content-disposition"] == 'attachment; filename="eval-trace.json"'
    assert download.headers["cache-control"] == "no-store"
    assert download.json()["trace_info"] == info
    assert client.get("/v1/evals/traces/tr-" + "0" * 32).status_code == 404
    assert (
        client.get(f"/v1/evals/traces/{seeded.live_trace}", params={"project_id": seeded.project_id}).status_code == 404
    )


def test_published_datasets_are_the_default_projects_newest_versions(served):
    client, _ = served
    listed = {d["name"]: d for d in client.get("/v1/evals/datasets").json()}
    assert (listed["support-faq"]["item_count"], listed["support-faq"]["scope"]) == (3, "org")
    assert listed["martian:jailbreak-basics"]["scope"] == "global"
    assert client.get("/v1/evals/datasets/support-faq").json() == listed["support-faq"]
    assert client.get("/v1/evals/datasets/missing").status_code == 404


def test_dataset_items_page_and_search(served):
    client, _ = served
    page = client.get("/v1/evals/datasets/support-faq/items", params={"page": 2, "limit": 2}).json()
    assert [i["id"] for i in page["items"]] == ["faq-3"]
    assert (page["total"], page["total_pages"], page["has_next"]) == (3, 2, False)
    found = client.get("/v1/evals/datasets/support-faq/items", params={"q": "USERNAME"}).json()
    assert [i["id"] for i in found["items"]] == ["faq-2"] and found["total"] is None
    archived = client.get("/v1/evals/datasets/martian:jailbreak-basics/items").json()["items"][0]
    assert (archived["status"], archived["usable"]) == ("ARCHIVED", False)
    too_deep = client.get("/v1/evals/datasets/support-faq/items", params={"page": 41, "limit": 100})
    assert too_deep.status_code == 422
