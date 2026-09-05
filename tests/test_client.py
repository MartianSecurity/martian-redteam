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


@pytest.mark.asyncio
async def test_evaluate_rejects_non_binary_score() -> None:
    async with _client() as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(return_value=httpx.Response(200, json={"score": 0.5, "reason": "maybe"}))
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
async def test_retryable_http_error_becomes_client_error() -> None:
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
