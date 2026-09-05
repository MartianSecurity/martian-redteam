import httpx
import pytest
import respx
from pydantic import SecretStr

from martian_redteam.catalog import ROT13, PromptInjection, PromptLeakage
from martian_redteam.client import Client
from martian_redteam.config import Settings
from martian_redteam.orchestrator import a_red_team


@pytest.mark.asyncio
async def test_orchestrator_single_turn_pass(simulator_payload: dict, evaluator_payload: dict) -> None:
    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))

    async def target(prompt: str) -> str:
        assert prompt
        return "I cannot disclose the system prompt."

    async with Client(settings=settings) as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(
                side_effect=[
                    httpx.Response(200, json=simulator_payload),
                    httpx.Response(200, json=evaluator_payload),
                    httpx.Response(200, json=simulator_payload),
                    httpx.Response(200, json=evaluator_payload),
                ]
            )
            assessment = await a_red_team(
                target=target,
                vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                attacks=[PromptInjection(), ROT13()],
                client=client,
                purpose="assistant",
            )

    assert assessment.errored == 0
    assert len(assessment.test_cases) == 2
    assert all(case.score == 1 for case in assessment.test_cases)
    assert assessment.catalog_version == "0.1.0"
    rot13_case = next(case for case in assessment.test_cases if case.attack_method == "rot13")
    assert rot13_case.input != simulator_payload["attack"]
