"""The core's write routes are the frozen evals contract's: each operation as the contract declares
it, the schemas it uses, and responses the contract allows. With the read routes, the core serves
every operation of the contract but the ones listed in ``NOT_SERVED``."""

from __future__ import annotations

import pytest

from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import ENTERPRISE, SINGLE_USER, create_app
from martian.evals.store import MemoryStore
from tests.evals.evals_contract import CONTRACT, operation, references, validate_request, validate_response
from tests.evals.test_read_contract import READ_OPERATIONS
from tests.evals.write_fixture import AGENT, Inert, request_id, serve

WRITE_OPERATIONS = [
    ("post", "/v1/evals"),
    ("post", "/v1/evals/{run_id}/cancel"),
    ("get", "/v1/evals/scorers"),
    ("post", "/v1/evals/{run_id}/scores"),
    ("post", "/v1/evals/{run_id}/scores/{job_id}/cancel"),
    ("post", "/v1/evals/{run_id}/assessments/{assessment_id}/sync"),
    ("get", "/v1/evals/monitoring"),
    ("get", "/v1/evals/suites"),
    ("post", "/v1/evals/suites"),
    ("get", "/v1/evals/judges"),
    ("post", "/v1/evals/judges"),
    ("get", "/v1/evals/judge-model"),
    ("put", "/v1/evals/judge-model"),
    ("post", "/v1/evals/{run_id}/items/{item_id}/reviews"),
    ("get", "/v1/evals/reviews"),
    ("get", "/v1/evals/review-queues"),
    ("post", "/v1/evals/review-queues"),
    ("post", "/v1/evals/review-queues/{queue_id}/sync"),
    ("get", "/v1/evals/review-questions"),
    ("post", "/v1/evals/review-questions"),
    ("post", "/v1/evals/traces/{trace_id}/annotations"),
    ("post", "/v1/evals/projects"),
    ("post", "/v1/evals/projects/{project_id}/archive"),
    ("get", "/v1/evals/views"),
    ("post", "/v1/evals/views"),
    ("put", "/v1/evals/views/{view_id}"),
    ("delete", "/v1/evals/views/{view_id}"),
    ("get", "/v1/evals/dataset-drafts"),
    ("post", "/v1/evals/dataset-drafts"),
    ("get", "/v1/evals/dataset-drafts/{draft_id}"),
    ("patch", "/v1/evals/dataset-drafts/{draft_id}"),
    ("delete", "/v1/evals/dataset-drafts/{draft_id}"),
    ("get", "/v1/evals/dataset-drafts/{draft_id}/validation"),
    ("get", "/v1/evals/dataset-drafts/{draft_id}/records"),
    ("post", "/v1/evals/dataset-drafts/{draft_id}/records"),
    ("patch", "/v1/evals/dataset-drafts/{draft_id}/records/{item_id}"),
    ("post", "/v1/evals/dataset-drafts/{draft_id}/records/merge"),
    ("post", "/v1/evals/dataset-drafts/{draft_id}/records/delete"),
    ("post", "/v1/evals/dataset-drafts/{draft_id}/records/from-eval-run"),
    ("post", "/v1/evals/dataset-drafts/{draft_id}/publish"),
    ("get", "/v1/evals/dataset-operations"),
    ("get", "/v1/evals/dataset-operations/{operation_id}"),
    ("post", "/v1/evals/dataset-operations/{operation_id}/resume"),
    ("post", "/v1/evals/dataset-operations/{operation_id}/abort"),
]

# The core's own status and bootstrap operations are in the contract's shape under the core's names
# (tests/evals/test_bootstrap.py), and OpenTelemetry's POST /v1/traces is OTLP's, not the contract's
# (tests/evals/test_otlp.py). CI gate sessions and tokens stay with the server's CI gate, and the
# explorer's overview, quality issues, trace export and capabilities are not ported yet.
OWN_NAMES = {("get", "/v1/evals/status"), ("get", "/v1/evals/bootstrap"), ("post", "/v1/traces")}
# The core's own routes, which the server's contract does not have: the attempts of repeated cases
# (MorningHQ/martian-notes#180), the turns of conversation cases (MorningHQ/martian-notes#191) and
# prompt versions (MorningHQ/martian-notes#207).
OWN_NAMES |= {("get", "/v1/evals/{run_id}/attempts"), ("get", "/v1/evals/{run_id}/turns")}
OWN_NAMES |= {
    ("get", "/v1/evals/prompts"),
    ("post", "/v1/evals/prompts"),
    ("get", "/v1/evals/prompts/{name}"),
    ("post", "/v1/evals/prompts/{name}/versions"),
}
NOT_SERVED = {
    ("get", "/v1/evals/capabilities"),
    ("get", "/v1/evals/ci-sessions"),
    ("get", "/v1/evals/ci-sessions/{session_id}"),
    ("post", "/v1/evals/ci-sessions/{session_id}/export"),
    ("get", "/v1/evals/ci/datasets/{dataset_name}"),
    ("post", "/v1/evals/ci/runs"),
    ("get", "/v1/evals/ci/runs/{run_id}"),
    ("post", "/v1/evals/ci/sessions"),
    ("get", "/v1/evals/ci/sessions/{session_id}"),
    ("get", "/v1/evals/ci/tokens"),
    ("post", "/v1/evals/ci/tokens"),
    ("delete", "/v1/evals/ci/tokens/{token_id}"),
    ("post", "/v1/evals/overview"),
    ("post", "/v1/evals/quality-issues/search"),
    ("post", "/v1/evals/quality-issues/triage"),
    ("post", "/v1/evals/traces/export"),
}


@pytest.fixture
def records(tmp_path):
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


@pytest.fixture
def openapi(records):
    return create_app(store=MemoryStore(), records=records, principal=lambda: SINGLE_USER).openapi()


@pytest.mark.parametrize(("method", "path"), WRITE_OPERATIONS)
def test_each_write_operation_is_the_contracts(openapi, method, path):
    assert openapi["paths"][path][method] == operation(method, path)


def test_the_schemas_the_write_operations_use_are_the_contracts(openapi):
    ours = openapi["components"]["schemas"]
    used = references([operation(method, path) for method, path in WRITE_OPERATIONS])
    assert {"EvalRunRequest", "ScoreRequest", "ReviewRequest", "QueueRequest", "SuiteRequest", "DraftOut"} <= used
    for name in sorted(used):
        assert ours[name] == CONTRACT["components"]["schemas"][name], name


def test_the_core_serves_the_whole_contract_but_the_ci_gate_and_four_explorer_reads(openapi):
    served = {(method, path) for path, methods in openapi["paths"].items() for method in methods}
    contract = {(method, path) for path, methods in CONTRACT["paths"].items() for method in methods}
    assert served == set(WRITE_OPERATIONS) | set(READ_OPERATIONS) | OWN_NAMES
    assert contract - served == NOT_SERVED


def test_write_responses_are_ones_the_contract_allows(records):
    client, core = serve(MemoryStore(), records, entitlements=lambda _p: ENTERPRISE)
    with client:
        body = {"agent_id": AGENT, "dataset_name": "support-faq", "request_id": request_id()}
        validate_request("EvalRunRequest", body)
        # A worker that has not taken the run yet, so the run can still be stopped.
        core.client.app.state.martian_evals_worker = Inert()  # type: ignore[attr-defined]
        launched = core.call("POST", "/v1/evals", 202, json=body)
        validate_response("post", "/v1/evals", launched, 202)
        stopped = core.call("POST", f"/v1/evals/{launched['id']}/cancel", 202)
        validate_response("post", "/v1/evals/{run_id}/cancel", stopped, 202)
        refused = client.post("/v1/evals", json={"agent_id": "not-a-uuid", "dataset_name": "support-faq"})
        assert refused.status_code == 422
        validate_response("post", "/v1/evals", refused.json(), 422)

        draft = core.call("POST", "/v1/evals/dataset-drafts", 201, json={"name": "Refunds"})
        validate_response("post", "/v1/evals/dataset-drafts", draft, 201)
        record = {"base_revision": 1, "record": {"id": "r1", "input": "Refund?", "expected_output": "Yes."}}
        core.call("POST", f"/v1/evals/dataset-drafts/{draft['id']}/records", 201, json=record)
        renamed = core.call(
            "PATCH", f"/v1/evals/dataset-drafts/{draft['id']}", 200, json={"base_revision": 2, "name": "R"}
        )
        validate_response("patch", "/v1/evals/dataset-drafts/{draft_id}", renamed)
        validate_response("get", "/v1/evals/dataset-drafts", core.call("GET", "/v1/evals/dataset-drafts", 200))
        checked = core.call("GET", f"/v1/evals/dataset-drafts/{draft['id']}/validation", 200)
        publish = {"base_revision": 3, "expected_sha256": checked["sha256"], "rationale": "First version"}
        core.call("POST", f"/v1/evals/dataset-drafts/{draft['id']}/publish", 202, json=publish)
        published = core.call("GET", f"/v1/evals/dataset-drafts/{draft['id']}", 200)
        validate_response("get", "/v1/evals/dataset-drafts/{draft_id}", published)
        assert published["state"] == "published" and published["operation_id"]
