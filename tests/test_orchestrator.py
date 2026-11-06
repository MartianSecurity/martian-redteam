import asyncio
import json
import time

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
    assert assessment.catalog_version == "0.2.0"
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
@pytest.mark.parametrize("mode", ["sync", "async", "swallow"])
async def test_hung_target_times_out_as_target_error(simulator_payload: dict, mode: str) -> None:
    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))
    cancelled = asyncio.Event()

    async def target(prompt: str) -> str:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            if mode == "swallow":
                return "late reply after cancellation"
            raise
        return "unreachable"

    def sync_target(prompt: str) -> str:
        time.sleep(0.15)
        return "late reply"

    async with Client(settings=settings) as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            route = router.post("/runs/wait").mock(return_value=httpx.Response(200, json=simulator_payload))
            assessment = await asyncio.wait_for(
                a_red_team(
                    target=sync_target if mode == "sync" else target,
                    vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                    attacks=[PromptInjection()],
                    client=client,
                    purpose="assistant",
                    target_timeout_seconds=0.05,
                ),
                timeout=5,
            )

    case = assessment.test_cases[0]
    if mode != "sync":
        assert cancelled.is_set()
    assert assessment.target_errors == 1
    assert assessment.infrastructure_errors == 0
    assert case.error_kind == "target"
    assert case.error is not None
    assert "timed out after 0.05 seconds" in case.error
    assert case.input == simulator_payload["attack"]
    assert case.score is None
    # Only the simulator was called; a timed-out reply is never sent to the evaluator.
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_timeout_raised_by_target_is_not_reported_as_sdk_deadline(simulator_payload: dict) -> None:
    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))

    async def target(prompt: str) -> str:
        raise TimeoutError("upstream model timed out")

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

    assert assessment.test_cases[0].error_kind == "target"
    assert assessment.test_cases[0].error == "target callback failed: upstream model timed out"


@pytest.mark.asyncio
async def test_sdk_internal_error_is_not_labelled_target_failure(
    simulator_payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))

    def broken_encoding(payload: str, method: str) -> str:
        raise ValueError("unknown encoding")

    monkeypatch.setattr("martian_redteam.orchestrator.apply_encoding", broken_encoding)

    async def target(prompt: str) -> str:
        return "no"

    async with Client(settings=settings) as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(return_value=httpx.Response(200, json=simulator_payload))
            with pytest.raises(ValueError, match="unknown encoding"):
                await a_red_team(
                    target=target,
                    vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                    attacks=[ROT13()],
                    client=client,
                    purpose="assistant",
                )


@pytest.mark.asyncio
async def test_evaluator_failure_keeps_target_reply(simulator_payload: dict) -> None:
    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))

    async def target(prompt: str) -> str:
        return "no"

    async with Client(settings=settings) as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            router.post("/runs/wait").mock(
                side_effect=[
                    httpx.Response(200, json=simulator_payload),
                    httpx.Response(400, json={"detail": "bad request"}),
                ]
            )
            assessment = await a_red_team(
                target=target,
                vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                attacks=[PromptInjection()],
                client=client,
                purpose="assistant",
            )

    case = assessment.test_cases[0]
    assert case.error_kind == "infrastructure"
    assert case.actual_output == "no"
    assert case.input == simulator_payload["attack"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, float("nan"), float("inf")])
async def test_non_positive_target_timeout_rejected(timeout: float) -> None:
    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))
    async with Client(settings=settings) as client:
        with pytest.raises(ValueError, match="target_timeout_seconds"):
            await a_red_team(
                target=lambda prompt: "no",
                vulnerabilities=[PromptLeakage()],
                attacks=[PromptInjection()],
                client=client,
                purpose="assistant",
                target_timeout_seconds=timeout,
            )


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

    asyncio.run(inside())


@pytest.mark.asyncio
async def test_timed_out_sync_callbacks_do_not_exceed_the_configured_concurrency(simulator_payload: dict) -> None:
    import threading

    release, started = threading.Event(), threading.Event()
    calls = []
    completed = []

    def target(prompt: str) -> str:
        calls.append(prompt)
        started.set()
        release.wait(timeout=2)
        return "late reply"

    def on_probe(case) -> None:
        completed.append(case)
        if len(completed) == 3:
            release.set()

    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))
    try:
        async with Client(settings=settings) as client:
            with respx.mock(base_url="https://agents.test.example") as router:
                router.post("/runs/wait").mock(return_value=httpx.Response(200, json=simulator_payload))
                assessment = await asyncio.wait_for(
                    a_red_team(
                        target=target,
                        vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                        attacks=[PromptInjection()],
                        client=client,
                        purpose="assistant",
                        max_concurrent=1,
                        attacks_per_vulnerability_type=3,
                        target_timeout_seconds=0.05,
                        on_probe=on_probe,
                    ),
                    timeout=5,
                )
        assert started.is_set()
        assert len(calls) == 1
        assert assessment.target_errors == 3
        assert all(case.score is None for case in assessment.test_cases)
    finally:
        release.set()
