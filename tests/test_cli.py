from typer.testing import CliRunner

from martian_redteam.cli import app

runner = CliRunner()


def test_catalog_command_lists_builtins() -> None:
    result = runner.invoke(app, ["catalog"])
    assert result.exit_code == 0
    assert "PromptLeakage" in result.stdout
    assert "prompt_injection" in result.stdout
    assert "owasp_llm_2025" in result.stdout


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout
