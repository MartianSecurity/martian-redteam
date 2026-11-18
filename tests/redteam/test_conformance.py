"""One conformance suite for every RedteamService, run on the local service and on the hosted client
replaying recorded operation receipts (martian-notes#240)."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator

from martian._privacy import RedactionPolicy
from martian.redteam.client import Client
from martian.redteam.engine import LocalService
from martian.redteam.engine.conformance import check_service, evaluator_contexts, main, simulator_contexts
from martian.redteam.server.contract import operations
from martian_redteam.catalog import VULNERABILITIES
from martian_redteam.types import EvaluatorOutput, SimulatorOutput

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "hosted-operations.json").read_text())


def failures(outcomes):
    return [f"{outcome.name}: {outcome.detail}" for outcome in outcomes if not outcome.passed]


def test_the_suite_covers_every_catalogue_vulnerability_and_type():
    simulated = {(c.vulnerability, c.vulnerability_type) for c in simulator_contexts()}
    for make in VULNERABILITIES.values():
        vulnerability = make()
        assert {(vulnerability.id, kind) for kind in vulnerability.allowed_types} <= simulated
    assert {c.attack_method for c in evaluator_contexts()} >= {"rot13", "base64", "leetspeak", "roleplay"}


async def test_the_local_service_passes(models):
    def failing():
        models.attacker = lambda messages: (500, None)
        models.judge = lambda messages: (503, None)
        return LocalService(attacker="openai:/a", judge="openai:/j", max_retries=0)

    outcomes = await check_service(lambda: LocalService(attacker="openai:/a", judge="openai:/j"), failing=failing)
    assert not failures(outcomes)
    assert len(outcomes) > 60


def replay(*, fail: bool = False):
    """A transport that answers as the hosted backend did: each operation first running, then its
    recorded receipt. With ``fail``, every request answers 503."""
    schema = operations()
    seen: dict[str, str] = {}

    def receipt(operation_id: str, kind: str, recorded: dict) -> dict:
        body = copy.deepcopy(recorded) | {"operation_id": operation_id, "kind": kind}
        body["deadline_at"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        body["result_expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat() if body["result"] else None
        Draft202012Validator(
            {"$ref": "#/components/schemas/OperationResponse", "components": schema["components"]}
        ).validate(body)
        return body

    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(503, json={"detail": "unavailable"})
        if request.method == "POST":
            sent = json.loads(request.content)
            kind = request.url.path.rsplit("/", 1)[1]
            seen[sent["operation_id"]] = kind
            return httpx.Response(202, json=receipt(sent["operation_id"], kind, RECORDED["running"]))
        operation_id = request.url.path.rsplit("/", 1)[1]
        kind = seen[operation_id]
        return httpx.Response(200, json=receipt(operation_id, kind, RECORDED[kind]))

    return httpx.MockTransport(handler)


async def test_the_hosted_client_passes_on_recorded_responses():
    def make(fail=False):
        return Client(api_url="https://backend.example", token="test-key", transport=replay(fail=fail))

    outcomes = await check_service(make, failing=lambda: make(fail=True))
    assert not failures(outcomes)


class Broken:
    """An implementation that gets the shapes wrong: a boolean score, and a bare exception."""

    redaction_policy = RedactionPolicy()

    async def simulate(self, context, *, prompt=None):
        return SimulatorOutput(attack="x", attack_method=context.attack_method)

    async def evaluate(self, context, *, attack, target_output, tools_called=None):
        output = EvaluatorOutput(score=1, reason="ok")
        object.__setattr__(output, "score", True)
        return output


class Failing(Broken):
    async def simulate(self, context, *, prompt=None):
        raise RuntimeError("provider down")


async def test_the_suite_fails_an_implementation_that_returns_the_wrong_shapes_or_errors():
    outcomes = await check_service(Broken, failing=Failing)
    found = failures(outcomes)
    assert any("scored True, not the integer 0 or 1" in item for item in found)
    assert any("raised RuntimeError, not a ClientError" in item for item in found)
    assert any("a failed evaluate is a ClientError: did not fail" in item for item in found)


def make_local():
    """The local service, for the one-command check below."""
    return LocalService(attacker="openai:/a", judge="openai:/j")


def test_one_command_checks_an_implementation(models, capsys):
    assert main(["tests.redteam.test_conformance:make_local"]) == 0
    printed = capsys.readouterr().out
    assert "PASS  simulate prompt_leakage/system_prompt" in printed and " 0 failed" in printed
    assert main(["tests.redteam.test_conformance:Broken"]) == 1


@pytest.mark.parametrize("name", ["no_colon", "tests.redteam.test_conformance:"])
def test_the_command_asks_for_module_and_callable(name):
    with pytest.raises(SystemExit):
        main([name])
