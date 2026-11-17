"""A run file that names a Martian environment (martian-notes#308).

The open package holds no environment: a run file names one, and a run in it needs a connection to a
Martian server. Without one, ``martian redteam run`` stops before anything is called."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from martian.cli import app
from martian.redteam.engine.service import EnvironmentNeedsServer, ServiceSelectionError, select_service
from martian.redteam.runfile import RunFile

PURPOSE = "A refund assistant. It looks up orders and issues refunds up to 50 USD."
CONNECTED = {"MARTIAN_API_URL": "https://martian.example", "MARTIAN_API_TOKEN": "mce_test"}


def run_file_data(tmp_path: Path, **fields: Any) -> dict[str, Any]:
    target = tmp_path / "agent.py"
    target.write_text("def target(message, session_id=None):\n    raise SystemExit('the target was called')\n")
    return {
        "version": 2,
        "target": {"purpose": PURPOSE, "file": str(target), "function": "target"},
        "models": {"attacker": "openai:/attacker-model", "judge": "openai:/judge-model"},
        "vulnerabilities": [
            {"name": "ExcessiveAgency", "types": ["functionality"], "checks": [{"type": "state", "name": "business"}]}
        ],
        "attacks": [{"name": "PromptInjection"}],
        "environment": "refund-desk",
        "output_folder": str(tmp_path / "results"),
        **fields,
    }


def write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "redteam.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_a_local_run_that_names_an_environment_exits_2_before_any_call(tmp_path, models):
    path = write(tmp_path, run_file_data(tmp_path))
    result = CliRunner().invoke(app, ["redteam", "run", str(path), "--no-store"])
    assert result.exit_code == 2, result.output
    message = " ".join(result.output.split())
    assert "environment 'refund-desk' is a Martian environment" in message
    assert "Set MARTIAN_API_URL and MARTIAN_API_TOKEN" in message
    assert "Nothing was called" in message
    assert models.requests == []
    assert not (tmp_path / "results").exists()


def test_the_file_validates_offline(tmp_path):
    path = write(tmp_path, run_file_data(tmp_path))
    result = CliRunner().invoke(app, ["redteam", "validate", str(path)])
    assert result.exit_code == 0, result.output
    assert "OK: version 2" in result.output


def test_without_a_connection_the_service_is_not_chosen(tmp_path):
    loaded = RunFile.model_validate(run_file_data(tmp_path))
    with pytest.raises(EnvironmentNeedsServer, match="MARTIAN_API_URL"):
        select_service(loaded, {})


def test_with_a_connection_the_models_still_choose_the_service(tmp_path):
    local = RunFile.model_validate(run_file_data(tmp_path))
    assert select_service(local, CONNECTED) == "local"
    hosted = RunFile.model_validate(run_file_data(tmp_path, models={}))
    assert select_service(hosted, CONNECTED) == "hosted"
    judge_only = RunFile.model_validate(run_file_data(tmp_path, models={"judge": "openai:/judge-model"}))
    with pytest.raises(ServiceSelectionError, match="needs models.attacker"):
        select_service(judge_only, CONNECTED)
