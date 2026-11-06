"""End-to-end tests for `martian-redteam run`, the command that is the CI gate.

The hosted API is replaced by respx, so the real Client, orchestrator, report
writer and gate all run. No network and no real key are needed.
"""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from martian_redteam.cli import app

API_URL = "https://agents.test.example"

runner = CliRunner()

Responder = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _cli_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARTIAN_REDTEAM_API_URL", API_URL)
    monkeypatch.setenv("MARTIAN_REDTEAM_API_KEY", "test-key")
    # save() appends to the job summary when this is set; keep CI's own summary clean.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    (tmp_path / "target.py").write_text(
        "async def model_callback(prompt: str) -> str:\n    return 'I cannot help with that.'\n",
        encoding="utf-8",
    )
    path = tmp_path / "run.yaml"
    path.write_text(
        "target:\n"
        "  purpose: test assistant\n"
        "  callback:\n"
        "    file: target.py\n"
        "system_config:\n"
        f"  output_folder: {tmp_path / 'from-yaml'}\n"
        "vulnerabilities:\n"
        "  - name: PromptLeakage\n"
        "    types: [system_prompt]\n"
        "attacks:\n"
        "  - name: PromptInjection\n",
        encoding="utf-8",
    )
    return path


def _responder(simulator: dict[str, Any], evaluator: httpx.Response) -> Responder:
    """Answer by assistant_id so the result does not depend on probe scheduling."""

    def respond(request: httpx.Request) -> httpx.Response:
        assistant_id = json.loads(request.content)["assistant_id"]
        if assistant_id == "simulator":
            return httpx.Response(200, json=simulator)
        return evaluator

    return respond


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API_URL) as router:
        yield router


def _load_report(directory: Path) -> dict[str, Any]:
    assert (directory / "risk-assessment.md").is_file()
    report: dict[str, Any] = json.loads((directory / "risk-assessment.json").read_text(encoding="utf-8"))
    return report


def test_run_passes_gate_and_writes_artifacts(
    api: respx.MockRouter,
    config_path: Path,
    tmp_path: Path,
    simulator_payload: dict[str, Any],
    evaluator_payload: dict[str, Any],
) -> None:
    api.post("/runs/wait").mock(side_effect=_responder(simulator_payload, httpx.Response(200, json=evaluator_payload)))

    result = runner.invoke(app, ["run", str(config_path)])

    assert result.exit_code == 0, result.output
    report = _load_report(tmp_path / "from-yaml")
    assert [case["score"] for case in report["test_cases"]] == [1]
    assert report["errored"] == 0
    assert "Gate failed" not in result.output


def test_run_exits_one_when_gated_probe_fails(
    api: respx.MockRouter,
    config_path: Path,
    tmp_path: Path,
    simulator_payload: dict[str, Any],
) -> None:
    verdict = {"score": 0, "reason": "The assistant disclosed the system prompt."}
    api.post("/runs/wait").mock(side_effect=_responder(simulator_payload, httpx.Response(200, json=verdict)))

    result = runner.invoke(app, ["run", str(config_path)])

    assert result.exit_code == 1
    assert "Gate failed" in result.output
    # Reports are written before the gate so a red job still has evidence.
    report = _load_report(tmp_path / "from-yaml")
    assert [case["score"] for case in report["test_cases"]] == [0]


def test_run_exits_one_on_infrastructure_error(
    api: respx.MockRouter,
    config_path: Path,
    tmp_path: Path,
    simulator_payload: dict[str, Any],
) -> None:
    api.post("/runs/wait").mock(side_effect=_responder(simulator_payload, httpx.Response(500, json={"detail": "boom"})))

    result = runner.invoke(app, ["run", str(config_path)])

    assert result.exit_code == 1
    assert "Gate failed" in result.output
    report = _load_report(tmp_path / "from-yaml")
    assert report["infrastructure_errors"] == 1
    assert report["test_cases"][0]["score"] is None
    assert report["test_cases"][0]["error_kind"] == "infrastructure"


def test_run_applies_cli_overrides(
    api: respx.MockRouter,
    config_path: Path,
    tmp_path: Path,
    simulator_payload: dict[str, Any],
    evaluator_payload: dict[str, Any],
) -> None:
    route = api.post("/runs/wait").mock(
        side_effect=_responder(simulator_payload, httpx.Response(200, json=evaluator_payload))
    )
    output = tmp_path / "from-flag"

    result = runner.invoke(app, ["run", str(config_path), "-o", str(output), "-c", "1", "-a", "2"])

    assert result.exit_code == 0, result.output
    report = _load_report(output)
    assert len(report["test_cases"]) == 2
    assert route.call_count == 4
    assert not (tmp_path / "from-yaml").exists()
    assert route.calls.last.request.headers["x-api-key"] == "test-key"
