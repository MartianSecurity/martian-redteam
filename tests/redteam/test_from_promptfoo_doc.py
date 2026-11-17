"""The guide for teams moving from promptfoo red team works against its sample project
(MartianSecurity/martian-notes#327).

The commands are read from ``docs/redteam-from-promptfoo.md`` and run in a copy of
``examples/promptfoo-redteam``, with a loopback model (``fake_models``)."""

import re
import shlex
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.redteam.interop.promptfoo import PLUGINS
from martian.redteam.model import EvidenceLevel, RedteamRun

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "redteam-from-promptfoo.md"


def commands() -> list[list[str]]:
    [block] = re.findall(r"<!-- from-promptfoo commands -->\n```bash\n(.*?)```", GUIDE.read_text(), re.DOTALL)
    return [shlex.split(line) for line in block.splitlines() if line.strip()]


def test_the_guides_commands_work_on_the_sample_project(tmp_path, models, monkeypatch):
    shutil.copytree(ROOT / "examples" / "promptfoo-redteam", tmp_path / "sample")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "data"))  # its own local store
    monkeypatch.chdir(tmp_path / "sample")
    runner = CliRunner()
    outputs = []
    for words in commands():
        assert words[0] == "martian"
        result = runner.invoke(app, words[1:])
        assert result.exit_code == 0, f"{' '.join(words)}\n{result.output}"
        outputs.append(result.output)
    for listed in ("plugin bola", "strategy basic", "strategy jailbreak:composite", "strategy crescendo"):
        assert listed in outputs[0]
    run = RedteamRun.model_validate_json((tmp_path / "sample" / "results" / "redteam-run.json").read_text())
    assert run.findings and {finding.evidence_level for finding in run.findings} == {EvidenceLevel.JUDGED}
    assert {request["body"]["model"] for request in models.requests} == {"gpt-4.1-mini"}

    from martian.evals.local import create_local_app, data_folder

    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>Martian</title>")
    with TestClient(create_local_app(data_folder(), ui), base_url="http://127.0.0.1:7600") as client:
        listed = {item["id"] for item in client.get("/v1/redteam/runs").json()["items"]}
    assert run.id in listed and "promptfoo-eval-rt7-2026-11-14T10-00-00-help-desk" in listed


def test_the_guides_counts_match_the_maps():
    assert "27\n  plugins onto the catalogue's 12 vulnerabilities" in GUIDE.read_text()
    assert len(PLUGINS) == 27 and len({name for name, _ in PLUGINS.values()}) == 12
