"""The red team quickstart works as written, with no Martian account (MartianSecurity/martian-notes#326).

The files and commands are read from ``docs/redteam-quickstart.md`` itself, so the test fails when the
guide stops working. A loopback OpenAI-compatible server plays the user's model (``fake_models``)."""

import os
import re
import shlex
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.redteam.model import EvidenceLevel, RedteamRun

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "redteam-quickstart.md"
BLOCK = r"<!-- quickstart {} -->\n```[a-z]*\n(.*?)```"


def blocks() -> tuple[dict[str, str], list[str]]:
    text = GUIDE.read_text(encoding="utf-8")
    files = dict(re.findall(BLOCK.format(r"file: (\S+)"), text, re.DOTALL))
    [commands] = re.findall(BLOCK.format("commands"), text, re.DOTALL)
    return files, [line for line in commands.splitlines() if line.strip()]


def test_the_quickstart_reaches_a_report_and_the_ui_with_no_martian_account(tmp_path, models, monkeypatch):
    assert not [name for name in os.environ if name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN")]
    files, commands = blocks()
    assert set(files) == {"agent.py", "redteam.yaml"}
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    codes = []
    for command in commands:
        words = shlex.split(command)
        assert words[0] == "martian"
        codes.append(runner.invoke(app, words[1:]).exit_code)
    # validate passes; the run finds the leaked code, so its gate fails with 1, as the guide says.
    assert codes == [0, 1]
    results = tmp_path / "results"
    for name in ("redteam-report.md", "redteam-report.html", "redteam-report.json", "redteam-run.json"):
        assert (results / name).exists(), name
    run = RedteamRun.model_validate_json((results / "redteam-run.json").read_text())
    found = {(item.vulnerability, run.probe(item.probe_id).attack_method, item.evidence_level) for item in run.findings}
    assert found == {("prompt_leakage", method, EvidenceLevel.TEXT) for method in ("prompt_injection", "roleplay")}
    assert models.requests  # the user's model wrote and judged the attacks

    from martian.evals.local import create_local_app, data_folder

    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>Martian</title>")
    with TestClient(create_local_app(data_folder(), ui), base_url="http://127.0.0.1:7600") as client:
        assert [item["id"] for item in client.get("/v1/redteam/runs").json()["items"]] == [run.id]
        assert client.get(f"/v1/redteam/runs/{run.id}/report").status_code == 200
