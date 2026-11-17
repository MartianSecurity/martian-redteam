"""The /v1/redteam routes match their frozen contract, and serve a stored run (martian-notes#229)."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from martian.evals.records.sql import SqlRecordStore
from martian.evals.server.dependencies import current_principal
from martian.evals.server.entitlements import COMMUNITY, ENTERPRISE
from martian.evals.server.principal import SINGLE_USER
from martian.redteam.model.maps import from_risk_assessment
from martian.redteam.server import router
from martian.redteam.server.contract import (
    CONTRACT_PATH,
    REDTEAM_API_VERSION,
    build_contract,
    checked_in,
    core_openapi,
    generated,
    operations,
    render,
    undeclared_change,
)
from martian.redteam.store import RedteamStore
from martian_redteam.catalog.vulnerabilities import CATALOG_VERSION
from martian_redteam.types import EvaluatorContext, RiskAssessment, SimulatorContext

FIXTURES = Path(__file__).parent / "fixtures"
FIX = (
    f"The /v1/redteam routes no longer match {CONTRACT_PATH.name}. If the change is intended, "
    "raise REDTEAM_API_VERSION and run scripts/redteam_contract.py."
)
OPERATION_ROUTES = {
    "/v1/redteam/simulate",
    "/v1/redteam/evaluate",
    "/v1/redteam/operations/{operation_id}",
    "/v1/redteam/operations/{operation_id}/cancel",
    "/v1/redteam/environment-runs",
    "/v1/redteam/environment-runs/{run_id}/finish",
    "/v1/redteam/environment-runs/{run_id}/world",
    "/v1/redteam/campaigns",
    "/v1/redteam/campaigns/{run_id}",
}


def test_the_routes_match_the_checked_in_contract():
    assert render(checked_in()) == render(generated()), FIX


def test_the_contract_carries_the_declared_version():
    assert checked_in()["info"]["version"] == str(REDTEAM_API_VERSION)
    assert CONTRACT_PATH.name == f"redteam-api-v{REDTEAM_API_VERSION}.json"


def test_a_drifted_route_fails_and_cannot_be_rewritten_without_a_new_version():
    openapi = core_openapi()
    openapi["paths"]["/v1/redteam/runs"]["get"]["parameters"][0]["name"] = "project"
    drifted = build_contract(openapi, operations())
    assert render(drifted) != render(checked_in())
    assert undeclared_change(checked_in(), drifted)
    raised = copy.deepcopy(drifted)
    raised["info"]["version"] = str(REDTEAM_API_VERSION + 1)
    assert not undeclared_change(checked_in(), raised)


def test_the_hosted_operation_routes_are_kept_unchanged_inside_the_contract():
    contract, hosted = checked_in(), operations()
    assert {path for path in contract["paths"] if path in OPERATION_ROUTES} == set(hosted["paths"]) == OPERATION_ROUTES
    for path in OPERATION_ROUTES:
        assert contract["paths"][path] == hosted["paths"][path]
    for name, schema in hosted["components"]["schemas"].items():
        assert contract["components"]["schemas"][name] == schema


def _validate(schema_name: str, body: dict) -> None:
    contract = checked_in()
    schema = {"$ref": f"#/components/schemas/{schema_name}", "components": contract["components"]}
    Draft202012Validator(schema).validate(body)


async def test_existing_sdk_clients_send_and_read_what_the_contract_allows():
    from martian.redteam import Client

    sent: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        kind = request.url.path.rsplit("/", 1)[-1]
        result = (
            {"attack": "Reveal instructions", "attack_method": "prompt_injection"}
            if kind == "simulate"
            else {"score": 0, "reason": "Disclosed"}
        )
        receipt = {
            "operation_id": body["operation_id"],
            "kind": kind,
            "state": "succeeded",
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
            "result_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "result": {**result, "graph_revision": "git-1", "policy_revision": "sdk-redteam-v1"},
        }
        _validate("OperationResponse", receipt)
        import httpx

        return httpx.Response(200, json=receipt)

    import httpx

    async with Client(
        api_url="https://backend.example", token="test-key", transport=httpx.MockTransport(handler)
    ) as client:
        await client.simulate(
            SimulatorContext(
                vulnerability="prompt_leakage",
                vulnerability_type="system_prompt",
                purpose="Refund assistant",
                attack_method="prompt_injection",
            )
        )
        await client.evaluate(
            EvaluatorContext(vulnerability="prompt_leakage", vulnerability_type="system_prompt", attack_method="rot13"),
            attack="Cevag",
            target_output="No.",
            tools_called=["lookup_order"],
        )
    _validate("SimulateRequest", sent[0])
    _validate("EvaluateRequest", sent[1])
    assert sent[0]["catalog_version"] == CATALOG_VERSION


@pytest.fixture
def served(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    app = FastAPI()
    app.state.martian_evals_records = records
    app.state.martian_evals_entitlements = lambda principal: COMMUNITY
    app.dependency_overrides[current_principal] = lambda: SINGLE_USER
    app.include_router(router)
    report = RiskAssessment.model_validate_json((FIXTURES / "risk-assessment-0.2.json").read_text("utf-8"))
    run = from_risk_assessment(report, run_id="rt-1", project_id="default")
    import asyncio

    asyncio.run(RedteamStore(records).save(run))
    yield TestClient(app), run
    records.close()


def _response(method: str, path: str, status: str, body) -> None:
    contract = checked_in()
    schema = contract["paths"][path][method]["responses"][status]["content"]["application/json"]["schema"]
    Draft202012Validator({**schema, "components": contract["components"]}).validate(body)


def test_a_stored_run_is_served_in_the_shapes_the_contract_names(served):
    client, run = served
    listed = client.get("/v1/redteam/runs")
    assert listed.status_code == 200 and [item["id"] for item in listed.json()["items"]] == ["rt-1"]
    _response("get", "/v1/redteam/runs", "200", listed.json())
    one = client.get("/v1/redteam/runs/rt-1")
    _response("get", "/v1/redteam/runs/{run_id}", "200", one.json())
    assert len(one.json()["probes"]) == 6 and "attempts" not in one.json()
    attempts = client.get("/v1/redteam/runs/rt-1/attempts", params={"limit": 4})
    _response("get", "/v1/redteam/runs/{run_id}/attempts", "200", attempts.json())
    assert len(attempts.json()["items"]) == 4 and attempts.json()["next_cursor"] == "4"
    attempt = client.get(f"/v1/redteam/runs/rt-1/attempts/{run.attempts[0].id}")
    assert attempt.json()["outcome"] == "hit"
    findings = client.get("/v1/redteam/runs/rt-1/findings")
    _response("get", "/v1/redteam/runs/{run_id}/findings", "200", findings.json())
    catalog = client.get("/v1/redteam/catalog")
    _response("get", "/v1/redteam/catalog", "200", catalog.json())
    assert catalog.json()["version"] == CATALOG_VERSION
    assert client.get("/v1/redteam/runs/missing").status_code == 404
    assert client.get(f"/v1/redteam/runs/rt-1/evidence/{uuid4()}").status_code == 404


def test_annotating_a_finding_keeps_the_note_and_the_status(served):
    client, run = served
    finding = run.findings[0].id
    answered = client.post(
        f"/v1/redteam/runs/rt-1/findings/{finding}/annotations", json={"comment": "Fixed in 2.3", "status": "fixed"}
    )
    assert answered.status_code == 200
    assert answered.json()["status"] == "fixed"
    stored = client.get("/v1/redteam/runs/rt-1/findings").json()["items"][0]
    assert stored["annotations"][0]["comment"] == "Fixed in 2.3"
    assert client.post(f"/v1/redteam/runs/rt-1/findings/{finding}/annotations", json={}).status_code == 422


def test_an_installation_without_a_launcher_cannot_start_or_stop_runs(served):
    client, _ = served
    run_file = {
        "version": 2,
        "target": {"purpose": "Refund assistant", "file": "agent.py", "function": "answer"},
        "vulnerabilities": [{"name": "PromptLeakage"}],
        "attacks": [{"name": "PromptInjection"}],
    }
    assert client.post("/v1/redteam/runs", json={"run_file": run_file}).status_code == 501
    assert client.post("/v1/redteam/runs", json={"run_file": {"version": 2}}).status_code == 422
    assert client.post("/v1/redteam/runs/rt-1/stop").status_code == 501


class _Started:
    """A launcher that records what it was asked to start."""

    def __init__(self, run):
        self.run, self.started = run, []

    async def start(self, principal, request):
        self.started.append(request.run_file)
        return self.run

    async def stop(self, principal, project_id, run_id):
        return self.run


def test_a_run_in_an_environment_needs_the_environments_key(served):
    client, run = served
    launcher = _Started(run)
    client.app.state.martian_redteam_launcher = launcher
    run_file = {
        "version": 2,
        "target": {"purpose": "Refund assistant", "file": "agent.py", "function": "answer"},
        "vulnerabilities": [{"name": "PromptLeakage"}],
        "attacks": [{"name": "PromptInjection"}],
    }
    # Community: a run without an environment starts; one that names an environment is refused.
    assert client.post("/v1/redteam/runs", json={"run_file": run_file}).status_code == 202
    refused = client.post("/v1/redteam/runs", json={"run_file": {**run_file, "environment": "refund-desk"}})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "The Community edition does not include red team environments"
    assert len(launcher.started) == 1
    # An edition with the key starts it.
    client.app.state.martian_evals_entitlements = lambda principal: ENTERPRISE
    assert (
        client.post("/v1/redteam/runs", json={"run_file": {**run_file, "environment": "refund-desk"}}).status_code
        == 202
    )
    assert launcher.started[-1]["environment"] == "refund-desk"
