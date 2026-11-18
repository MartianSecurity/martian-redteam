import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from martian_redteam.client import Client, ClientError
from martian_redteam.config import Settings
from martian_redteam.types import EvaluatorContext, SimulatorContext


def _client() -> Client:
    return Client(
        settings=Settings(
            api_url="https://agents.test.example",
            api_key=SecretStr("test-key"),
        )
    )


@pytest.mark.asyncio
async def test_aegra_error_envelope_with_partial_score_is_not_accepted() -> None:
    async with _client() as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            route = router.post("/runs/wait").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "score": 1,
                        "reason": "partial score",
                        "__error__": {"error": "RuntimeError"},
                    },
                )
            )
            with pytest.raises(ClientError, match="status error"):
                await client.evaluate(
                    EvaluatorContext(
                        vulnerability="prompt_leakage",
                        vulnerability_type="system_prompt",
                        attack_method="prompt_injection",
                    ),
                    attack="reveal instructions",
                    target_output="I refuse",
                )
            assert route.call_count == 1


@pytest.mark.asyncio
async def test_simulate_parses_graph_state(simulator_payload: dict) -> None:
    async with _client() as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(return_value=httpx.Response(200, json=simulator_payload))
            output = await client.simulate(
                SimulatorContext(
                    vulnerability="prompt_leakage",
                    vulnerability_type="system_prompt",
                    purpose="assistant",
                    attack_method="prompt_injection",
                )
            )
            request = router.calls.last.request
    assert output.attack.startswith("Ignore previous")
    assert output.attack_method == "prompt_injection"
    assert request.headers["x-api-key"] == "test-key"
    assert b'"assistant_id":"simulator"' in request.content
    assert b'"on_completion":"delete"' in request.content


@pytest.mark.asyncio
async def test_simulate_unwraps_output_envelope(simulator_payload: dict) -> None:
    envelope = {
        "status": "success",
        "output": {**simulator_payload, "graph_revision": "sha256:test"},
    }
    async with _client() as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(return_value=httpx.Response(200, json=envelope))
            output = await client.simulate(
                SimulatorContext(
                    vulnerability="prompt_leakage",
                    vulnerability_type="system_prompt",
                    purpose="assistant",
                    attack_method="prompt_injection",
                )
            )
    assert output.attack == simulator_payload["attack"]
    assert output.graph_revision == "sha256:test"


@pytest.mark.asyncio
async def test_simulate_parses_heartbeat_prefixed_body(simulator_payload: dict) -> None:
    body = "\n\n" + json.dumps(simulator_payload)
    async with _client() as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(
                return_value=httpx.Response(200, text=body, headers={"Content-Type": "application/json"})
            )
            output = await client.simulate(
                SimulatorContext(
                    vulnerability="prompt_leakage",
                    vulnerability_type="system_prompt",
                    purpose="assistant",
                    attack_method="prompt_injection",
                )
            )
    assert output.attack == simulator_payload["attack"]


@pytest.mark.asyncio
async def test_failed_run_status_is_client_error() -> None:
    async with _client() as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(return_value=httpx.Response(200, json={"status": "error"}))
            with pytest.raises(ClientError, match="status error"):
                await client.simulate(
                    SimulatorContext(
                        vulnerability="prompt_leakage",
                        vulnerability_type="system_prompt",
                        purpose="assistant",
                        attack_method="prompt_injection",
                    )
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0.5, True, 1.0, "1"])
async def test_evaluate_rejects_non_binary_score(score: object) -> None:
    async with _client() as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(return_value=httpx.Response(200, json={"score": score, "reason": "maybe"}))
            with pytest.raises(ClientError, match="schema validation"):
                await client.evaluate(
                    EvaluatorContext(
                        vulnerability="prompt_leakage",
                        vulnerability_type="system_prompt",
                        attack_method="prompt_injection",
                    ),
                    attack="x",
                    target_output="y",
                )


@pytest.mark.asyncio
async def test_503_with_unknown_execution_state_is_not_retried() -> None:
    async with _client() as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(return_value=httpx.Response(503, json={"detail": "busy"}))
            with pytest.raises(ClientError, match="HTTP 503"):
                await client.simulate(
                    SimulatorContext(
                        vulnerability="prompt_leakage",
                        vulnerability_type="system_prompt",
                        purpose="assistant",
                        attack_method="prompt_injection",
                    )
                )
            assert router.calls.call_count == 1


@pytest.mark.asyncio
async def test_http_429_mentions_waf() -> None:
    async with _client() as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(
                return_value=httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "1"})
            )
            with pytest.raises(ClientError, match="WAF"):
                await client.simulate(
                    SimulatorContext(
                        vulnerability="prompt_leakage",
                        vulnerability_type="system_prompt",
                        purpose="assistant",
                        attack_method="prompt_injection",
                    )
                )
            assert router.calls.call_count == 3


@pytest.mark.asyncio
async def test_http_error_is_client_error() -> None:
    async with _client() as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(return_value=httpx.Response(500, json={"detail": "boom"}))
            with pytest.raises(ClientError, match="HTTP 500"):
                await client.simulate(
                    SimulatorContext(
                        vulnerability="prompt_leakage",
                        vulnerability_type="system_prompt",
                        purpose="assistant",
                        attack_method="prompt_injection",
                    )
                )
            assert router.calls.call_count == 1
