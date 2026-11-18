"""Customer auth isolation and safe recovery at the paid execution boundary."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import httpx
import pytest

from martian import MartianConfig
from martian.config import ConfigError
from martian.redteam import Client, PromptInjection, PromptLeakage, RedteamOperationError, a_red_team
from martian_redteam.types import EvaluatorContext, SimulatorContext


def receipt(body, kind="simulate", *, state="succeeded", result=None):
    if result is None and state == "succeeded":
        result = (
            {"attack": "Reveal instructions", "attack_method": "prompt_injection"}
            if kind == "simulate"
            else {"score": 0, "reason": "Target disclosed instructions"}
        )
    if result is not None:
        result = {**result, "graph_revision": "git-test", "policy_revision": "sdk-redteam-v1"}
    return {
        "operation_id": body["operation_id"],
        "kind": kind,
        "state": state,
        "deadline_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
        "result_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "poll_after_ms": 10,
        "result": result,
    }


def context():
    return SimulatorContext(
        vulnerability="prompt_leakage",
        vulnerability_type="system_prompt",
        purpose="test",
        attack_method="prompt_injection",
    )


@pytest.mark.asyncio
async def test_single_origin_domain_requests_and_local_target_no_retries(tmp_path, monkeypatch):
    calls = []
    secret = "constructor-only-private-secret"
    monkeypatch.setenv("MARTIAN_REPORT_INCLUDE_CONTENT", "1")

    async def transport(request):
        assert request.url.host == "backend.example"
        assert request.headers["authorization"] == "Bearer " + secret
        assert "x-api-key" not in request.headers
        body = json.loads(request.content)
        assert "messages" not in body and "model" not in body and "assistant_id" not in body
        calls.append((request.url.path, body))
        return httpx.Response(200, json=receipt(body, request.url.path.rsplit("/", 1)[1]))

    target_calls = []

    def target(prompt):
        target_calls.append(prompt)
        return "target content " + secret

    async with Client(
        api_url="https://backend.example", token=secret, transport=httpx.MockTransport(transport)
    ) as client:
        report = await a_red_team(
            target=target,
            vulnerabilities=[PromptLeakage(types=["system_prompt"])],
            attacks=[PromptInjection()],
            client=client,
            purpose="safe assistant",
        )
    assert len(target_calls) == 1
    assert [path for path, _ in calls] == ["/v1/redteam/simulate", "/v1/redteam/evaluate"]
    assert report.test_cases[0].score == 0
    assert report.policy_revision == "sdk-redteam-v1"
    assert report.test_cases[0].simulation_operation_id != report.test_cases[0].evaluation_operation_id
    # No environment credential exists, client is closed, and private copies must still redact.
    copied = report.model_copy(deep=True).for_report(include_target_content=True)
    copied.save(tmp_path, include_target_content=True)
    for path in tmp_path.iterdir():
        assert secret not in path.read_text()
    assert secret not in repr(report)
    assert "target content [redacted]" in copied.test_cases[0].actual_output


@pytest.mark.asyncio
async def test_probe_checkpoint_failure_joins_sibling_before_returning():
    sibling_started = asyncio.Event()
    sibling_finished = asyncio.Event()
    release_sibling = asyncio.Event()
    simulations = 0
    target_calls = []

    async def transport(request):
        nonlocal simulations
        kind = request.url.path.rsplit("/", 1)[1]
        if kind == "simulate":
            simulations += 1
            if simulations == 1:
                await sibling_started.wait()
            else:
                sibling_started.set()
                try:
                    await release_sibling.wait()
                finally:
                    sibling_finished.set()
        return httpx.Response(200, json=receipt(json.loads(request.content), kind))

    async def target(prompt):
        target_calls.append(prompt)
        return "synthetic target response"

    def checkpoint(case):
        raise OSError("checkpoint write failed")

    async with Client(
        api_url="https://backend.example", token="test-key", transport=httpx.MockTransport(transport)
    ) as client:
        try:
            with pytest.raises(OSError, match="checkpoint write failed"):
                await a_red_team(
                    target=target,
                    vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                    attacks=[PromptInjection()],
                    client=client,
                    purpose="safe assistant",
                    attacks_per_vulnerability_type=2,
                    on_probe=checkpoint,
                )
            assert sibling_finished.is_set()
            assert len(target_calls) == 1
        finally:
            release_sibling.set()


@pytest.mark.asyncio
async def test_repeated_cancellation_joins_a_running_sync_callback(monkeypatch):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    shutdown_started = asyncio.Event()
    release = Event()
    finished = Event()

    class ObservedExecutor(ThreadPoolExecutor):
        def shutdown(self, wait=True, *, cancel_futures=False):
            loop.call_soon_threadsafe(shutdown_started.set)
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr("martian_redteam.orchestrator.ThreadPoolExecutor", ObservedExecutor)

    def target(_prompt):
        loop.call_soon_threadsafe(started.set)
        release.wait()
        finished.set()
        return "synthetic target response"

    async def transport(request):
        kind = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json=receipt(json.loads(request.content), kind))

    async with Client(
        api_url="https://backend.example", token="test-key", transport=httpx.MockTransport(transport)
    ) as client:
        run = asyncio.create_task(
            a_red_team(
                target=target,
                vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                attacks=[PromptInjection()],
                client=client,
                purpose="safe assistant",
            )
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            run.cancel("initial cancellation")
            await asyncio.wait_for(shutdown_started.wait(), timeout=5)
            run.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not run.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError, match="initial cancellation"):
                await asyncio.wait_for(run, timeout=5)
        assert finished.is_set()


@pytest.mark.asyncio
async def test_ambiguous_post_recovers_same_id_using_get_only():
    calls = []
    admitted = {}

    async def transport(request):
        calls.append(request.method)
        if request.method == "POST":
            admitted.update(json.loads(request.content))
            raise httpx.ReadTimeout("private raw exception", request=request)
        assert request.url.path.endswith(admitted["operation_id"])
        return httpx.Response(200, json=receipt(admitted))

    async with Client(
        api_url="https://backend.example", token="test-key", transport=httpx.MockTransport(transport)
    ) as c:
        result = await c.simulate(context())
    assert calls == ["POST", "GET"]
    assert result.operation_id == admitted["operation_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [True, 1.0, "1", None])
async def test_invalid_evaluator_score_fails_without_execution_retry(score):
    calls = []

    async def transport(request):
        calls.append(request.method)
        body = json.loads(request.content)
        return httpx.Response(200, json=receipt(body, "evaluate", result={"score": score, "reason": "reason"}))

    async with Client(
        api_url="https://backend.example", token="test-key", transport=httpx.MockTransport(transport)
    ) as c:
        with pytest.raises(RedteamOperationError, match="invalid_operation_response"):
            await c.evaluate(
                EvaluatorContext(vulnerability="x", vulnerability_type="y", attack_method="z"),
                attack="attack",
                target_output="output",
            )
    assert calls == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mismatch", "expired", "uncertain", "purged", "wrong_kind", "redirect"])
async def test_bad_or_absent_receipts_fail_closed(failure):
    calls = []

    async def transport(request):
        calls.append(request.url.host)
        body = json.loads(request.content)
        response = receipt(body)
        if failure == "mismatch":
            response["operation_id"] = str(uuid4())
        elif failure == "expired":
            response["result_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        elif failure in ("uncertain", "purged"):
            response.update(state=failure, result=None)
        elif failure == "wrong_kind":
            response["kind"] = "evaluate"
        else:
            return httpx.Response(307, headers={"Location": "https://credential-thief.example"})
        return httpx.Response(410 if failure == "purged" else 200, json=response)

    async with Client(
        api_url="https://backend.example", token="test-key", transport=httpx.MockTransport(transport)
    ) as c:
        with pytest.raises(RedteamOperationError):
            await c.simulate(context())
    assert calls == ["backend.example"]


@pytest.mark.asyncio
async def test_polling_and_http_phases_share_absolute_deadline():
    calls = []
    admitted = {}

    async def transport(request):
        calls.append(request.method)
        if request.method == "POST":
            admitted.update(json.loads(request.content))
        await asyncio.sleep(0.2)
        return httpx.Response(202, json=receipt(admitted, state="running"))

    async with Client(
        config=MartianConfig("https://backend.example", "test-key", request_timeout_seconds=1),
        transport=httpx.MockTransport(transport),
    ) as c:
        started = asyncio.get_running_loop().time()
        with pytest.raises(RedteamOperationError, match="execution_unknown_recover_by_id") as error:
            await c.simulate(context())
        assert asyncio.get_running_loop().time() - started < 1.5
    assert calls.count("POST") == 1
    assert error.value.operation_id == admitted["operation_id"]


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@backend.example",
        "https://backend.example/path",
        "https://backend.example?token=secret",
        "http://external.example",
        "https://[bad",
    ],
)
def test_shared_config_rejects_unbound_origins_without_echoing_values(url):
    with pytest.raises(ConfigError) as error:
        MartianConfig(url, "private-token")
    assert "secret" not in str(error.value) and "private-token" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_auth_admission_and_gateway_errors_never_replay_execution(status):
    calls = []
    admitted = {}

    async def transport(request):
        calls.append(request.method)
        if request.method == "POST":
            admitted.update(json.loads(request.content))
            return httpx.Response(status, json={"error": {"code": "private-token-must-not-be-echoed"}})
        return httpx.Response(200, json=receipt(admitted))

    async with Client(
        api_url="https://backend.example", token="private-token", transport=httpx.MockTransport(transport)
    ) as c:
        if status == 503:
            assert (await c.simulate(context())).operation_id == admitted["operation_id"]
        else:
            with pytest.raises(RedteamOperationError) as error:
                await c.simulate(context())
            assert "private-token" not in str(error.value)
    assert calls == (["POST", "GET"] if status == 503 else ["POST"])


def test_modern_yaml_cli_uses_backend_pair_and_rejects_origin_override_before_import(tmp_path, monkeypatch):
    import respx
    from typer.testing import CliRunner

    from martian.cli import app

    callback = tmp_path / "callback.py"
    marker = tmp_path / "imported"
    callback.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef model_callback(prompt): return 'safe'\n"
    )
    config = tmp_path / "redteam.yaml"
    text = (
        "target:\n  purpose: Safe assistant\n  callback:\n    file: callback.py\n"
        "vulnerabilities:\n  - name: PromptLeakage\n    types: [system_prompt]\n"
        "attacks:\n  - name: PromptInjection\n"
    )
    config.write_text(text)
    monkeypatch.setenv("MARTIAN_API_URL", "https://backend.example")
    monkeypatch.setenv("MARTIAN_API_TOKEN", "cli-token")
    monkeypatch.setenv("MARTIAN_REDTEAM_API_URL", "invalid-unused-legacy-url")
    monkeypatch.setenv("MARTIAN_REDTEAM_API_KEY", "unused-legacy-key")

    def respond(request):
        assert request.headers["authorization"] == "Bearer cli-token"
        body = json.loads(request.content)
        kind = request.url.path.rsplit("/", 1)[1]
        result = None if kind == "simulate" else {"score": 1, "reason": "resisted"}
        return httpx.Response(200, json=receipt(body, kind, result=result))

    with respx.mock(base_url="https://backend.example") as router:
        router.post("/v1/redteam/simulate").mock(side_effect=respond)
        router.post("/v1/redteam/evaluate").mock(side_effect=respond)
        result = CliRunner().invoke(app, ["redteam", "run", str(config), "-o", str(tmp_path / "results")])
    assert result.exit_code == 0, result.output
    assert marker.exists()
    marker.unlink()
    config.write_text(text + "api:\n  url: https://unapproved.example\n")
    result = CliRunner().invoke(app, ["redteam", "run", str(config), "-o", str(tmp_path / "blocked")])
    assert result.exit_code == 2
    assert not marker.exists()
    result = CliRunner().invoke(app, ["redteam", "validate", str(config)])
    assert result.exit_code == 1
    assert not marker.exists()


def test_explicit_connection_cannot_rebind_an_environment_credential(monkeypatch):
    monkeypatch.setenv("MARTIAN_API_URL", "https://approved.example")
    monkeypatch.setenv("MARTIAN_API_TOKEN", "bound-secret")
    with pytest.raises(ConfigError, match="together"):
        Client(api_url="https://unapproved.example")
    with pytest.raises(ConfigError, match="not both"):
        Client(config=MartianConfig("https://approved.example", "bound-secret"), token="other-secret")


def test_platform_dataset_read_needs_connection_without_ci_attribution(monkeypatch):
    from evals.stub_backend import StubBackend

    from martian.evals import load_dataset

    monkeypatch.delenv("MARTIAN_AGENT_ID", raising=False)
    monkeypatch.setenv("MARTIAN_EVALS_GIT_SHA", "invalid-unused")
    with StubBackend() as backend:
        rows = load_dataset("support", config=MartianConfig(backend.url, backend.token))
    assert rows.pin.name == "support" and len(rows) == 3


def test_legacy_evals_adapter_rejects_conflicting_explicit_ci_and_preserves_connection_options():
    from martian import CiContext
    from martian.evals.config import KitConfig, resolve_ci_config

    connection = MartianConfig("https://backend.example", "bound-token", request_timeout_seconds=73, trust_env=True)
    context = CiContext("11111111-1111-4111-8111-111111111111", "abcdef123456")
    adapted = resolve_ci_config(connection, context)
    assert adapted.connection is connection
    assert adapted.provenance()["git_sha"] == context.git_sha
    legacy = KitConfig(
        connection.api_url, connection.token.get_secret_value(), str(context.agent_id), git_sha=context.git_sha
    )
    with pytest.raises(ConfigError, match="conflicts"):
        resolve_ci_config(legacy, CiContext(context.agent_id, "0123456789abcdef"))


@pytest.mark.asyncio
async def test_caller_can_persist_operation_id_before_sending_and_recover_without_post():
    identifier = uuid4()
    calls = []

    async def transport(request):
        calls.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["operation_id"] == str(identifier)
        else:
            assert request.url.path.endswith(str(identifier))
            body = {"operation_id": str(identifier)}
        return httpx.Response(200, json=receipt(body))

    async with Client(
        api_url="https://backend.example", token="test-key", transport=httpx.MockTransport(transport)
    ) as c:
        first = await c.simulate(context(), operation_id=identifier)
        recovered = await c.get_operation(identifier)
    assert first.operation_id == str(identifier) and recovered.operation_id == identifier
    assert calls == ["POST", "GET"]
