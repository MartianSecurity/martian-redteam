"""`martian redteam campaign` (martian-notes#314): start a planned campaign on a Martian server,
follow it, write its result and exit by its release decision. The server is a fake on loopback."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from martian.cli import app
from martian.redteam.campaign import Campaign, exit_code
from tests.redteam.martian_server import TOKEN, FakeMartianServer
from tests.redteam.target_servers import json_server
from tests.redteam.test_push import local_run

AGENT = "1b2c3d4e-0000-4000-8000-000000000001"


@pytest.fixture
def server(monkeypatch):
    fake = FakeMartianServer("http://127.0.0.1:9/mcp")
    with json_server(fake) as (url, _):
        monkeypatch.setenv("MARTIAN_API_URL", url.removesuffix("/chat"))
        monkeypatch.setenv("MARTIAN_API_TOKEN", TOKEN)
        yield fake


def campaign(tmp_path, *extra):
    return CliRunner().invoke(
        app, ["redteam", "campaign", "--agent", AGENT, "--depth", "quick", "--poll", "1", "-o", str(tmp_path), *extra]
    )


def test_a_blocked_campaign_exits_1_and_writes_its_result(server, tmp_path):
    server.decision = ("BLOCKED", ["blocking_severity_finding"])
    server.campaign_run = local_run(server.run_id, source="campaign").model_dump(mode="json")
    result = campaign(tmp_path)
    assert result.exit_code == 1, result.output
    [started] = server.campaigns
    assert (started["agent_id"], started["depth"]) == (AGENT, "quick")
    text = " ".join(result.output.split())
    assert f"Started campaign MR-2001 ({server.run_id}); the console shows it at /#/runs/{server.run_id}" in text
    assert "release decision BLOCKED (blocking_severity_finding)" in text
    written = json.loads((tmp_path / "redteam-run.json").read_text())
    assert (written["id"], written["source"]) == (server.run_id, "campaign")
    assert (tmp_path / "redteam-report.md").exists()


def test_an_approved_campaign_exits_0(server, tmp_path):
    assert campaign(tmp_path).exit_code == 0


def test_a_conditional_decision_fails_unless_allowed(server, tmp_path):
    server.decision = ("CONDITIONAL", ["coverage_or_evidence_gap"])
    assert campaign(tmp_path).exit_code == 1
    server.readings_to_finish = 1
    allowed = campaign(tmp_path, "--allow-conditional")
    assert allowed.exit_code == 0 and "CONDITIONAL" in allowed.output


def test_a_campaign_that_does_not_finish_in_time_exits_2(server, tmp_path):
    server.readings_to_finish = 100
    result = campaign(tmp_path, "--timeout", "2")
    assert result.exit_code == 2, result.output
    assert "did not finish within 2 seconds" in " ".join(result.output.split())


def test_a_refused_start_exits_2_with_the_servers_reason(server, tmp_path):
    server.refusal = (403, "The Community edition does not include red team campaigns")
    result = campaign(tmp_path)
    assert result.exit_code == 2
    assert "does not include red team campaigns" in " ".join(result.output.split())


@pytest.mark.parametrize(
    ("state", "finished", "decision", "code"),
    [
        ("completed", True, "APPROVED", 0),
        ("stopped", True, "BLOCKED", 1),
        ("completed", True, "PENDING", 2),
        ("failed", True, "APPROVED", 2),
        ("running", False, "PENDING", 2),
    ],
)
def test_the_exit_code_follows_the_release_decision(state, finished, decision, code):
    found = Campaign(
        id="x", run_code="MR-1", state=state, finished=finished, decision=decision, reasons=(), console_path=""
    )
    assert exit_code(found) == code
