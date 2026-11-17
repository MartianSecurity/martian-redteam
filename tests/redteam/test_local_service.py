"""Red team attacks generated and judged locally with the user's own model (martian-notes#233).

A loopback OpenAI-compatible server plays the attacker and the judge (``fake_models``). Nothing calls a
real model or a Martian server."""

import ipaddress
import os
import socket

import pytest

from martian.redteam.catalog.prompts import PROMPTS_REVISION, evaluator_system_prompt, simulator_system_prompt
from martian.redteam.engine import (
    EmptyGeneration,
    LocalService,
    LocalServiceError,
    ServiceSelectionError,
    UnreadableJudgment,
    connection_configured,
    select_service,
)
from martian.redteam.runfile import RunFile
from martian_redteam import PromptInjection, PromptLeakage, a_red_team
from martian_redteam.types import EvaluatorContext, SimulatorContext

ATTACKER = "openai:/attacker-model"
JUDGE = "openai:/judge-model"
SIMULATE = SimulatorContext(
    vulnerability="prompt_leakage", vulnerability_type="system_prompt", purpose="Help desk", attack_method="roleplay"
)
EVALUATE = EvaluatorContext(
    vulnerability="prompt_leakage", vulnerability_type="system_prompt", attack_method="roleplay"
)


def service(**overrides):
    return LocalService(attacker=ATTACKER, judge=JUDGE, max_retries=0, **overrides)


async def test_the_attacker_writes_an_attack_from_the_catalogue_prompt(models):
    output = await service().simulate(SIMULATE)
    assert output.attack == "ATTACK prompt_leakage/system_prompt via roleplay"
    assert output.attack_method == "roleplay"
    assert output.graph_revision == PROMPTS_REVISION
    assert output.usage is not None and (output.usage.input_tokens, output.usage.output_tokens) == (100, 20)
    [call] = models.calls("attacker")
    assert call["body"]["model"] == "attacker-model"
    assert call["body"]["messages"][0]["content"] == simulator_system_prompt(SIMULATE)


async def test_the_judge_scores_the_reply_with_the_catalogue_rubric(models):
    leaked = await service().evaluate(EVALUATE, attack="Tell me.", target_output="LEAKED: be kind")
    held = await service().evaluate(EVALUATE, attack="Tell me.", target_output="No.")
    assert (leaked.score, held.score) == (0, 1)
    assert leaked.reason == "It leaked."
    calls = models.calls("judge")
    assert [call["body"]["model"] for call in calls] == ["judge-model", "judge-model"]
    assert calls[0]["body"]["messages"][0]["content"] == evaluator_system_prompt(EVALUATE)


@pytest.mark.parametrize("score", [True, 1.0, "1", 2, None])
async def test_a_judgment_that_is_not_the_integer_0_or_1_is_unreadable(models, score):
    models.judge = lambda messages: (200, {"score": score, "reason": "Fine."})
    with pytest.raises(UnreadableJudgment) as raised:
        await service().evaluate(EVALUATE, attack="a", target_output="b")
    assert raised.value.kind == "judge"


async def test_a_judgment_in_prose_is_unreadable(models):
    models.judge = lambda messages: (200, "The target resisted, score 1.")
    with pytest.raises(UnreadableJudgment):
        await service().evaluate(EVALUATE, attack="a", target_output="b")


@pytest.mark.parametrize("reply", ["I can't help with writing attacks.", {"attack": "   ", "attack_method": "x"}])
async def test_an_attacker_reply_without_an_attack_is_an_empty_generation(models, reply):
    models.attacker = lambda messages: (200, reply)
    with pytest.raises(EmptyGeneration) as raised:
        await service().simulate(SIMULATE)
    assert raised.value.kind == "attacker"


async def test_a_failed_model_call_names_the_model_that_failed(models):
    models.attacker = lambda messages: (500, None)
    models.judge = lambda messages: (401, None)
    with pytest.raises(LocalServiceError) as attacker:
        await service().simulate(SIMULATE)
    with pytest.raises(LocalServiceError) as judge:
        await service().evaluate(EVALUATE, attack="a", target_output="b")
    assert (attacker.value.kind, judge.value.kind) == ("attacker", "judge")
    assert not isinstance(attacker.value, EmptyGeneration) and not isinstance(judge.value, UnreadableJudgment)
    assert "private provider text" not in str(attacker.value) + str(judge.value)
    assert "models.attacker" in str(attacker.value) and "models.judge" in str(judge.value)


async def test_without_a_judge_judging_fails_and_says_so(models):
    with pytest.raises(LocalServiceError, match="models.judge"):
        await LocalService(attacker=ATTACKER).evaluate(EVALUATE, attack="a", target_output="b")


def test_the_run_records_the_models_but_never_their_headers():
    from martian.redteam.runfile import ModelConfig

    secret = ModelConfig(model=JUDGE, headers={"X-Key": "header-secret-value"}, inference_params={"temperature": 0})
    described = LocalService(attacker=ATTACKER, judge=secret).describe()
    assert described["service"] == "local"
    assert described["attacker"]["model"] == ATTACKER
    assert described["judge"] == {"model": JUDGE, "inference_params": {"temperature": 0}}
    assert "header-secret-value" not in repr(described)


# --- which service runs --------------------------------------------------------------------------

BASE = {
    "version": 2,
    "target": {"purpose": "Help desk", "file": "agent.py"},
    "vulnerabilities": [{"name": "PromptLeakage"}],
    "attacks": [{"name": "PromptInjection"}],
}
CONNECTED = {"MARTIAN_API_URL": "https://api.martian.example", "MARTIAN_API_TOKEN": "token"}


def run_file(**fields):
    return RunFile.model_validate({**BASE, **fields})


def test_models_and_no_connection_run_locally():
    assert select_service(run_file(models={"attacker": ATTACKER, "judge": JUDGE}), {}) == "local"


def test_no_models_and_a_connection_run_on_the_platform():
    assert select_service(run_file(), CONNECTED) == "hosted"


@pytest.mark.parametrize("environ", [CONNECTED, {"MARTIAN_API_TOKEN": "token"}, {"MARTIAN_API_URL": "https://x"}])
def test_models_and_a_connection_stop_instead_of_choosing_one(environ):
    with pytest.raises(ServiceSelectionError, match="Unset those variables"):
        select_service(run_file(models={"attacker": ATTACKER, "judge": JUDGE}), environ)


def test_neither_stops_and_names_both_ways():
    with pytest.raises(ServiceSelectionError, match="models.attacker.*MARTIAN_API_URL"):
        select_service(run_file(), {"MARTIAN_API_URL": "  "})
    assert not connection_configured({"MARTIAN_API_URL": "", "MARTIAN_API_TOKEN": " "})


def test_a_judge_without_an_attacker_or_an_environment_stops():
    with pytest.raises(ServiceSelectionError, match="needs models.attacker"):
        select_service(run_file(models={"judge": JUDGE}), {})
    with pytest.raises(ServiceSelectionError, match="environment"):
        select_service(run_file(environment="refund-desk"), CONNECTED)


# --- a whole run, with nothing sent to Martian ------------------------------------------------


@pytest.fixture
def connections(monkeypatch):
    """Every address a socket of this process connects to, and every name it looks up."""
    seen: list[tuple[str, object]] = []
    connect, lookup = socket.socket.connect, socket.getaddrinfo

    def spy_connect(self, address):
        seen.append(("connect", address))
        return connect(self, address)

    def spy_lookup(host, *args, **kwargs):
        seen.append(("lookup", host))
        return lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    monkeypatch.setattr(socket, "getaddrinfo", spy_lookup)
    return seen


async def test_a_run_completes_with_only_a_model_key_and_sends_nothing_to_martian(models, connections, monkeypatch):
    for name in [name for name in os.environ if name.startswith("MARTIAN_") and name != "MARTIAN_EVALS_HOME"]:
        monkeypatch.delenv(name)

    def target(prompt: str) -> str:
        return "LEAKED: you are a help desk" if "prompt_leakage" in prompt else "No."

    assessment = await a_red_team(
        target=target,
        vulnerabilities=[PromptLeakage(types=["system_prompt", "instructions"])],
        attacks=[PromptInjection()],
        client=service(),
        purpose="Help desk",
    )
    assert len(assessment.test_cases) == 2
    assert {case.score for case in assessment.test_cases} == {0}
    assert not [case for case in assessment.test_cases if case.error]
    assert len(models.requests) == 4
    # The model server was reached, nothing beyond loopback was, and no other name was looked up.
    # (Under pytest-xdist another test's threads in this worker may also connect, to loopback only.)
    addresses = {address for kind, address in connections if kind == "connect"}
    assert ("127.0.0.1", models.port) in addresses
    assert all(ipaddress.ip_address(address[0]).is_loopback for address in addresses), addresses
    assert {host for kind, host in connections if kind == "lookup"} <= {"127.0.0.1", "localhost"}
