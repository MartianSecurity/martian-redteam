"""The core's read routes are the frozen evals contract's: each operation as the contract declares
it, and each response one the contract allows. The contract is the server's, copied unchanged into
``tests/evals/contracts`` (see ``evals_contract.py``)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import SINGLE_USER, create_app
from martian.evals.store import MemoryStore
from tests.evals.evals_contract import CONTRACT, VERSION, operation, references, validate_request, validate_response
from tests.evals.read_fixture import seed

READ_OPERATIONS = [
    ("get", "/v1/evals"),
    ("get", "/v1/evals/{run_id}"),
    ("get", "/v1/evals/{run_id}/workbench"),
    ("get", "/v1/evals/{run_id}/items/{item_id}/trace"),
    ("get", "/v1/evals/{run_id}/provenance"),
    ("get", "/v1/evals/compare"),
    ("get", "/v1/evals/projects"),
    ("post", "/v1/evals/traces/search"),
    ("post", "/v1/evals/sessions/search"),
    ("get", "/v1/evals/traces/{trace_id}"),
    ("get", "/v1/evals/traces/{trace_id}/payload"),
    ("get", "/v1/evals/datasets"),
    ("get", "/v1/evals/datasets/{dataset_name}"),
    ("get", "/v1/evals/datasets/{dataset_name}/items"),
]

# The fields of the server's untyped responses, from the code that builds them in
# MorningHQ/MartianBackend: public_job, public_assessment, public_review (app/evals_workbench.py),
# project_out (app/api/evals_explorer.py), project_trace and sessions (app/evals_explorer.py), safe_trace and
# compare_runs (app/evals_workbench.py), compare_assessments and scorecard (app/evals_quality.py).
JOB = {"id", "run_id", "config", "state", "error", "created_at", "finished_at", "total", "succeeded", "errors"}
JOB |= {"cancel_requested_at"}
ASSESSMENT = {"id", "item_id", "job_id", "name", "value", "rationale", "source", "source_id", "provenance"}
ASSESSMENT |= {"error", "native_id", "telemetry_state", "created_at"}
REVIEW = {"id", "item_id", "queue_id", "criterion", "previous_id", "actor_id", "assignee_id", "assessment_id"}
REVIEW |= {"state", "value", "comment", "created_at"}
WORKBENCH = {"jobs", "assessments", "reviews", "latest_reviews", "latest_reviews_truncated", "members", "queues"}
WORKBENCH |= {"selected_job", "pagination", "truncated", "scorecard", "calibration"}
TRACE_ROW = {"id", "name", "state", "timestamp", "timestamp_ms", "latency_ms", "request", "response", "session_id"}
TRACE_ROW |= {"user_id", "model", "tokens", "cost", "assessments", "triage", "run_id"}
TRACE_VIEW = {"trace_id", "spans", "token_usage", "estimated_cost", "truncated", "usage_note"}
SPAN = {"id", "parent_id", "name", "duration_ms", "offset_ms", "type", "token_usage", "cost", "model"}
SPAN |= {"retrieved_documents", "inputs", "outputs", "status", "attributes", "events", "depth"}
COMPARISON = {"compatible", "reason", "baseline_id", "candidate_id", "coverage", "changes", "items"}
JOB_COMPARISON = {"criteria", "baseline_job_id", "candidate_job_id", "baseline_scorecard", "candidate_scorecard"}
SCORECARD = {"criteria", "total", "latency", "gate"}
CARD = {"name", "kind", "categories", "total", "evaluated", "missing", "errors", "coverage", "pass_rate", "passed"}
CARD |= {"confidence_95", "mean", "p50", "p95", "minimum", "maximum", "gate", "policy"}


@pytest.fixture
def served(tmp_path):
    store, records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    seeded = asyncio.run(seed(store, records, SINGLE_USER))
    yield TestClient(create_app(store=store, records=records, principal=lambda: SINGLE_USER)), seeded
    records.close()


def test_the_copy_is_version_3_of_the_servers_contract():
    assert CONTRACT["info"]["version"] == VERSION == "3"
    assert CONTRACT["info"]["title"] == "Martian Evals API"


@pytest.fixture
def openapi(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield create_app(store=MemoryStore(), records=records, principal=lambda: SINGLE_USER).openapi()
    records.close()


@pytest.mark.parametrize(("method", "path"), READ_OPERATIONS)
def test_each_read_operation_is_the_contracts(openapi, method, path):
    assert openapi["paths"][path][method] == operation(method, path)


def test_the_schemas_the_read_operations_use_are_the_contracts(openapi):
    ours = openapi["components"]["schemas"]
    used = references([operation(method, path) for method, path in READ_OPERATIONS])
    assert {"EvalRunOut", "EvalRunDetailOut", "EvalItemOut", "EvalDatasetItemsOut", "TraceQuery"} <= used
    for name in sorted(used):
        assert ours[name] == CONTRACT["components"]["schemas"][name], name


def test_typed_responses_are_ones_the_contract_allows(served):
    client, seeded = served
    runs = client.get("/v1/evals", params={"default_only": True}).json()
    assert len(runs) == 2
    validate_response("get", "/v1/evals", runs)
    validate_response("get", "/v1/evals", client.get("/v1/evals").json())
    validate_response("get", "/v1/evals", client.get("/v1/evals", params={"project_id": seeded.project_id}).json())
    detail = client.get(f"/v1/evals/{seeded.candidate.id}").json()
    assert len(detail["items"]) == 3
    validate_response("get", "/v1/evals/{run_id}", detail)
    datasets = client.get("/v1/evals/datasets").json()
    assert {d["name"] for d in datasets} == {"support-faq", "martian:jailbreak-basics"}
    validate_response("get", "/v1/evals/datasets", datasets)
    validate_response("get", "/v1/evals/datasets/{dataset_name}", client.get("/v1/evals/datasets/support-faq").json())
    for params in ({}, {"q": "invoice"}, {"page": 2, "limit": 2}):
        page = client.get("/v1/evals/datasets/support-faq/items", params=params).json()
        validate_response("get", "/v1/evals/datasets/{dataset_name}/items", page)


def test_a_refused_parameter_is_the_contracts_validation_error(served):
    client, seeded = served
    response = client.get("/v1/evals", params={"page": 0})
    assert response.status_code == 422
    validate_response("get", "/v1/evals", response.json(), 422)
    response = client.get(f"/v1/evals/{seeded.candidate.id}/workbench", params={"job_id": "not-a-job"})
    assert response.status_code == 422
    validate_response("get", "/v1/evals/{run_id}/workbench", response.json(), 422)
    response = client.get("/v1/evals/traces/not-a-trace")
    assert response.status_code == 422
    validate_response("get", "/v1/evals/traces/{trace_id}", response.json(), 422)


def test_the_ui_trace_query_is_the_contracts():
    # initialQuery in the Evals UI (packages/evals/src/lib/eval-explorer.js in MorningHQ/MartianSecurityFrontend).
    validate_request(
        "TraceQuery",
        {"project_id": None, "q": "", "state": "", "session_id": "", "user_id": "", "order": "newest", "limit": 25},
    )


def test_untyped_responses_have_the_servers_fields(served):
    client, seeded = served
    projects = client.get("/v1/evals/projects").json()
    assert set(projects) == {"items", "datasets_scope"}
    assert set(projects["items"][0]) == {"id", "name", "description", "state"}
    assert set(projects["items"][1]) == {"id", "name", "description", "state", "created_at", "archived_at"}

    work = client.get(f"/v1/evals/{seeded.candidate.id}/workbench").json()
    assert set(work) == WORKBENCH
    assert set(work["selected_job"]) == JOB and set(work["jobs"][0]) == JOB
    assert work["assessments"] and all(set(a) == ASSESSMENT for a in work["assessments"])
    assert work["reviews"] and all(set(r) == REVIEW for r in work["reviews"] + work["latest_reviews"])
    assert set(work["pagination"]) == {"jobs", "assessments", "reviews"}
    assert set(work["pagination"]["jobs"]) == {"page", "limit", "total", "has_more"}
    assert set(work["scorecard"]) == SCORECARD and set(work["scorecard"]["criteria"][0]) == CARD
    validate_response("get", "/v1/evals/{run_id}/workbench", work)

    pair = {"baseline_id": seeded.baseline.id, "candidate_id": seeded.candidate.id}
    comparison = client.get("/v1/evals/compare", params=pair).json()
    assert set(comparison) == COMPARISON
    assert set(comparison["items"][0]) == {"dataset_item_id", "prompt", "expected", "baseline", "candidate", "change"}
    assert set(comparison["items"][0]["baseline"]) == {"trace_id", "status", "output", "latency_ms"}
    jobs = {"baseline_job_id": seeded.baseline_job.id, "candidate_job_id": seeded.candidate_job.id}
    scored = client.get("/v1/evals/compare", params={**pair, **jobs}).json()
    assert set(scored) == COMPARISON | JOB_COMPARISON
    assert set(scored["baseline_scorecard"]) == SCORECARD

    search = client.post("/v1/evals/traces/search", json={"limit": 2}).json()
    assert set(search) == {"items", "next_cursor", "limit"}
    assert all(set(row) == TRACE_ROW for row in search["items"])
    assert set(search["items"][0]["assessments"][0]) == {"id", "name", "value", "rationale", "source", "error"}

    sessions = client.post("/v1/evals/sessions/search", json={}).json()
    assert set(sessions) == {"items", "has_more", "traces_examined", "ungrouped", "limit"}
    assert set(sessions["items"][0]) == {"id", "turns", "errors", "started_at", "last_at", "preview", "tokens"}
    filtered = client.post("/v1/evals/sessions/search", json={"min_latency_ms": 500}).json()
    assert set(filtered) == set(sessions) and filtered["items"]

    trace_id = seeded.traces[(seeded.candidate.id, "faq-1")]
    detail = client.get(f"/v1/evals/traces/{trace_id}").json()
    assert set(detail) == TRACE_VIEW | {"info"}
    assert all(set(span) == SPAN for span in detail["spans"])
    item_id = next(i["id"] for i in client.get(f"/v1/evals/{seeded.candidate.id}").json()["items"])
    item_trace = client.get(f"/v1/evals/{seeded.candidate.id}/items/{item_id}/trace").json()
    assert set(item_trace) == TRACE_VIEW | {"annotations"}

    provenance = client.get(f"/v1/evals/{seeded.candidate.id}/provenance").json()
    assert set(provenance) == {"admission", "native", "note"}
    assert set(provenance["native"]) == {"id", "status", "metrics", "datasets"}
