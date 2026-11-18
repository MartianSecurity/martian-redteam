import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from martian_redteam.cli import app

runner = CliRunner()
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def test_catalog_command_lists_builtins() -> None:
    result = runner.invoke(app, ["catalog"])
    assert result.exit_code == 0
    assert "PromptLeakage" in result.stdout
    assert "prompt_injection" in result.stdout
    assert "owasp_llm_2025" in result.stdout


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    from martian import __version__

    assert result.stdout.strip() == __version__


def test_run_help_mentions_concurrency_overrides() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    help_text = "".join(_ANSI_ESCAPE.sub("", result.stdout).split())
    assert "-c" in help_text
    assert "--max-concurrent" in help_text
    assert "-a" in help_text
    assert "--attacks-per-vuln" in help_text


def test_validate_example_config_succeeds() -> None:
    result = runner.invoke(app, ["validate", "examples/config.yaml"])
    assert result.exit_code == 0
    assert "OK" in result.stdout
    assert "2 vulnerabilities" in result.stdout
    assert "2 attacks" in result.stdout
    assert "callback.py" in result.stdout


def test_validate_missing_file_fails() -> None:
    result = runner.invoke(app, ["validate", "does-not-exist.yaml"])
    assert result.exit_code != 0


def test_validate_unknown_catalog_name_fails(tmp_path: Path) -> None:
    callback = tmp_path / "target.py"
    callback.write_text("def model_callback(prompt: str) -> str:\n    return 'no'\n", encoding="utf-8")
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
target:
  purpose: test
  callback:
    file: target.py
vulnerabilities:
  - name: NotARealVulnerability
attacks:
  - name: PromptInjection
""".strip()
        + "\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", str(config_path)])
    assert result.exit_code == 1
    assert "unknown vulnerability" in result.stdout


def _write_config(tmp_path: Path, vulnerabilities: str) -> Path:
    callback = tmp_path / "target.py"
    callback.write_text("def model_callback(prompt: str) -> str:\n    return 'no'\n", encoding="utf-8")
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "target:\n"
        "  purpose: test\n"
        "  callback:\n"
        "    file: target.py\n"
        f"vulnerabilities:\n{vulnerabilities}"
        "attacks:\n"
        "  - name: PromptInjection\n",
        encoding="utf-8",
    )
    return config_path


def test_validate_rejects_criteria_on_builtin(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "  - name: Toxicity\n    criteria: Must not insult the user\n")
    result = runner.invoke(app, ["validate", str(config_path)])
    assert result.exit_code == 1
    assert "criteria is only valid for custom vulnerabilities" in "".join(result.stdout.split("\n"))


def test_validate_accepts_gated_builtin(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "  - name: Toxicity\n    gated: true\n")
    result = runner.invoke(app, ["validate", str(config_path)])
    assert result.exit_code == 0
    assert "1 vulnerabilities" in result.stdout


def test_validate_rejects_insecure_api_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARTIAN_ALLOW_INSECURE_API_URL", raising=False)
    config_path = _write_config(tmp_path, "  - name: Toxicity\n")
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write("api:\n  url: http://evil.example\n")
    result = runner.invoke(app, ["validate", str(config_path)])
    assert result.exit_code == 1
    assert "api.url must use https://" in "".join(result.stdout.split("\n"))
