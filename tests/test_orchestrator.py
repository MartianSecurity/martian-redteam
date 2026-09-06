import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from martian_redteam.catalog import ROT13, CustomVulnerability, PromptInjection, PromptLeakage
from martian_redteam.client import Client
from martian_redteam.config import Settings
from martian_redteam.orchestrator import a_red_team, red_team


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


@pytest.mark.asyncio
async def test_target_exception_is_target_error(simulator_payload: dict) -> None:
    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))

    async def target(prompt: str) -> str:
        raise RuntimeError("target down")

    async with Client(settings=settings) as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(return_value=httpx.Response(200, json=simulator_payload))
            assessment = await a_red_team(
                target=target,
                vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                attacks=[PromptInjection()],
                client=client,
                purpose="assistant",
            )

    assert assessment.target_errors == 1
    assert assessment.infrastructure_errors == 0
    assert assessment.test_cases[0].error_kind == "target"
    assert assessment.test_cases[0].input == simulator_payload["attack"]


@pytest.mark.asyncio
async def test_custom_vulnerability_sends_criteria(simulator_payload: dict, evaluator_payload: dict) -> None:
    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))

    async def target(prompt: str) -> str:
        return "no"

    async with Client(settings=settings) as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            route = router.post("/runs/wait").mock(
                side_effect=[
                    httpx.Response(200, json=simulator_payload),
                    httpx.Response(200, json=evaluator_payload),
                ]
            )
            await a_red_team(
                target=target,
                vulnerabilities=[
                    CustomVulnerability(id="secrets_in_logs", criteria="Must not echo API keys", types=["custom"])
                ],
                attacks=[PromptInjection()],
                client=client,
                purpose="assistant",
            )

    evaluator_body = json.loads(route.calls.last.request.content.decode())
    assert evaluator_body["assistant_id"] == "evaluator"
    assert evaluator_body["context"]["criteria"] == "Must not echo API keys"
    assert evaluator_body["context"]["vulnerability"] == "secrets_in_logs"


@pytest.mark.asyncio
async def test_empty_catalog_rejected() -> None:
    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))
    async with Client(settings=settings) as client:
        with pytest.raises(ValueError, match="at least one vulnerability"):
            await a_red_team(
                target=lambda prompt: "no",
                vulnerabilities=[],
                attacks=[PromptInjection()],
                client=client,
                purpose="assistant",
            )


def test_sync_red_team_rejects_running_loop() -> None:
    async def inside() -> None:
        with pytest.raises(RuntimeError, match="a_red_team"):
            red_team(
                target=lambda prompt: "no",
                vulnerabilities=[PromptLeakage()],
                attacks=[PromptInjection()],
                client=Client(settings=Settings(api_url="https://agents.test.example", api_key=SecretStr("k"))),
                purpose="assistant",
            )

    import asyncio

    asyncio.run(inside())
