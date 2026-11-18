"""Users can report failures without exposing secrets or private response text."""

import json
import os
import subprocess
import sys
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from typer.testing import CliRunner

from martian import __version__
from martian.cli import app
from martian.evals.client import Backend, BackendError
from martian.redteam import Client, PromptInjection, PromptLeakage, RedteamOperationError, RiskAssessment, a_red_team
from martian_redteam.types import SimulatorContext


def test_evals_cold_import_has_no_unsolicited_agent_instruction():
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MARTIAN_", "MLFLOW_"))}
    result = subprocess.run(
        [sys.executable, "-c", "import martian.evals; print('READY')"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == "READY"
    assert "mlflow.agent.hint" not in result.stderr and "SKILL.md" not in result.stderr


@pytest.mark.asyncio
async def test_auth_error_keeps_actionable_code_and_references_in_default_reports(tmp_path):
    request_id = str(uuid4())
    private = "constructor-private-value"

    def response(request):
        return httpx.Response(403, headers={"x-request-id": request_id}, json={"detail": private})

    async with Client(
        api_url="https://backend.example", token=private, transport=httpx.MockTransport(response)
    ) as client:

        def target(prompt):
            raise AssertionError("An unauthorized operation must not reach the target")

        report = await a_red_team(
            target=target,
            client=client,
            purpose="Synthetic test",
            vulnerabilities=[PromptLeakage(types=["system_prompt"])],
            attacks=[PromptInjection()],
        )
    case = report.test_cases[0]
    assert case.error_code == "http_403" and case.simulation_operation_id
    assert "Permission denied" in case.error and request_id in case.error
    saved = json.loads(report.save(tmp_path).read_text())
    assert "http_403" in saved["test_cases"][0]["error"]
    assert "error_code" not in saved["test_cases"][0], "Keep existing report JSON compatible"
    assert case.simulation_operation_id in saved["test_cases"][0]["error"]
    assert private not in json.dumps(saved)
    reopened = RiskAssessment.model_validate(saved).for_report()
    assert reopened.test_cases[0].error_code == "http_403"
    assert case.simulation_operation_id in reopened.test_cases[0].error


@pytest.mark.parametrize("header", ["invalid", "reflected-credential"])
def test_evals_error_keeps_upload_request_id_but_rejects_untrusted_header_and_body(header):
    request_id = str(uuid4())
    private = "raw-private-provider-response"
    token = str(uuid4()).upper()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            403, headers={"x-request-id": private if header == "invalid" else token.lower()}, json={"detail": private}
        )

    backend = Backend("https://backend.example", token, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(BackendError) as failed:
            backend.upload_session({"request_id": request_id})
    finally:
        backend.close()
    assert failed.value.code == "http_403" and failed.value.request_id == request_id
    assert "Permission denied" in str(failed.value) and request_id in str(failed.value)
    assert private not in str(failed.value) and len(requests) == 1
    assert token.lower() not in str(failed.value).lower()


@pytest.mark.asyncio
async def test_redteam_request_reference_cannot_reflect_a_constructor_credential():
    token = str(uuid4()).upper()
    async with Client(
        api_url="https://backend.example",
        token=token,
        transport=httpx.MockTransport(lambda request: httpx.Response(403, headers={"x-request-id": token.lower()})),
    ) as client:
        with pytest.raises(RedteamOperationError) as failure:
            await client.simulate(
                SimulatorContext(
                    vulnerability="prompt_leakage",
                    vulnerability_type="system_prompt",
                    purpose="Synthetic check",
                    attack_method="prompt_injection",
                )
            )
    assert failure.value.request_id is None
    assert token.lower() not in str(failure.value).lower()


@pytest.mark.parametrize("scenario", ["missing-token", "callback-import", "denied"])
def test_cli_expected_failures_are_concise_reportable_and_private(tmp_path, monkeypatch, scenario):
    private = "untrusted-private-value"
    monkeypatch.setenv("MARTIAN_API_URL", "https://backend.example")
    monkeypatch.setenv("MARTIAN_API_TOKEN", private)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    callback = tmp_path / "callback.py"
    callback.write_text(
        f"raise type({private!r}, (RuntimeError,), {{}})({private!r})\n"
        if scenario == "callback-import"
        else "def answer(prompt): return 'safe'\n"
    )
    config = tmp_path / "redteam.yml"
    config.write_text(
        "target:\n  purpose: Safe assistant\n  callback:\n    file: callback.py\n    function: answer\n"
        "vulnerabilities:\n  - name: PromptLeakage\n    types: [system_prompt]\n"
        "attacks:\n  - name: PromptInjection\n"
    )
    output = tmp_path / "results"
    if scenario == "missing-token":
        monkeypatch.delenv("MARTIAN_API_TOKEN")
    with respx.mock as router:
        route = router.post("https://backend.example/v1/redteam/simulate").respond(403, json={"detail": private})
        result = CliRunner().invoke(app, ["redteam", "run", str(config), "-o", str(output), "-a", "3"])
    assert "Traceback" not in result.output and private not in result.output
    if scenario == "denied":
        assert result.exit_code == 1 and route.call_count == 3
        assert result.output.count("Permission denied") == 1
        report = json.loads((output / "risk-assessment.json").read_text())
        assert len({case["simulation_operation_id"] for case in report["test_cases"]}) == 3
        assert all("http_403" in case["error"] for case in report["test_cases"])
    else:
        assert result.exit_code == 2 and route.call_count == 0
        receipt = json.loads((output / "execution-error.json").read_text())
        assert receipt["sdk_version"] == __version__
        assert str(UUID(receipt["error_id"])) in result.output
        assert private not in json.dumps(receipt)
