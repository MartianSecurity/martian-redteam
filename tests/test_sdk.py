"""Shared SDK boundaries: credential isolation and the combined CI decision."""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from martian.cli import app
from martian.redteam import Client


@pytest.mark.asyncio
async def test_modern_client_uses_only_backend_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARTIAN_API_URL", "https://backend.test.example")
    monkeypatch.setenv("MARTIAN_API_TOKEN", "backend-token")
    monkeypatch.setenv("MARTIAN_API_KEY", "legacy-shared-key")
    monkeypatch.setenv("MARTIAN_REDTEAM_API_URL", "invalid-unused-legacy-origin")
    monkeypatch.setenv("MARTIAN_REDTEAM_API_KEY", "redteam-key")
    async with Client() as client:
        assert client.config.api_url == "https://backend.test.example"
        assert client.config.token.get_secret_value() == "backend-token"


def test_modern_client_refuses_legacy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from martian.config import ConfigError

    monkeypatch.delenv("MARTIAN_API_TOKEN", raising=False)
    monkeypatch.setenv("MARTIAN_API_URL", "https://backend.test.example")
    monkeypatch.setenv("MARTIAN_API_KEY", "legacy-shared-key")
    with pytest.raises(ConfigError, match="missing MARTIAN_API_TOKEN"):
        Client()
    with pytest.raises(ConfigError, match="uses token="):
        Client(api_url="https://agents.test.example", api_key="legacy-key")


@pytest.mark.asyncio
async def test_explicit_credentials_ignore_invalid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARTIAN_API_URL", "http://untrusted.example?credential=must-not-be-used")
    monkeypatch.setenv("MARTIAN_API_TOKEN", "environment-key")
    async with Client(api_url="https://backend.test.example", token="explicit-key") as client:
        assert client.config.api_url == "https://backend.test.example"
        assert client.config.token.get_secret_value() == "explicit-key"


@pytest.mark.parametrize("code", [0, 1, 2])
def test_combined_cli_calls_one_public_runner_and_preserves_its_exit_code(tmp_path: Path, code: int) -> None:
    from martian.result import SessionResult

    evals = tmp_path / "evals"
    evals.mkdir()
    config = tmp_path / "redteam.yaml"
    config.write_text("{}")
    output = tmp_path / "results"
    receipt = SessionResult(exit_code=code, state="passed" if code == 0 else "failed")
    with patch("martian.ci.run", return_value=receipt) as run:
        result = CliRunner().invoke(
            app, ["ci", "--evals-path", str(evals), "--redteam-config", str(config), "--output", str(output)]
        )
    assert result.exit_code == code, result.output
    run.assert_called_once()
    suite = run.call_args.args[0]
    assert suite.evals_paths == (evals,)
    assert suite.redteam_config == config


def test_sdk_cli_can_validate_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARTIAN_REDTEAM_API_KEY", raising=False)
    monkeypatch.setenv("MARTIAN_REDTEAM_API_URL", "invalid-unused-service-url")
    result = CliRunner().invoke(app, ["redteam", "validate", "examples/config.yaml"])
    assert result.exit_code == 0, result.output
