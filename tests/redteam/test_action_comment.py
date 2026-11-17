"""Red team results in the evals action's pull-request comment (martian-notes#285).

The sample under examples/github-action/redteam runs through the CLI as the action's red team step
runs it, with the Martian connection variables cleared and a loopback server for the models, and its
comment must be the checked-in comments/redteam.md."""

import ipaddress
import json
import socket
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from martian.cli import app
from tests.redteam.fake_models import RUBRIC_JUDGE, field
from tests.test_evals_pr_comment import (  # noqa: F401 - github is a fixture
    BOT,
    RUN_URL,
    SAMPLE,
    SHA,
    action_env,
    comment,
    github,
)

ROOT = Path(__file__).resolve().parents[2]
RUN_FILE = "examples/github-action/redteam/redteam.yaml"
ACTION = ROOT / ".github/actions/evals/action.yml"


def _judge(messages):
    if messages[0]["content"].startswith(RUBRIC_JUDGE):
        segments = json.loads(messages[1]["content"])["response_segments"]
        cited = [segment["id"] for segment in segments if "hint" in segment["text"]]
        verdict, reason = ("fail", "It hinted.") if cited else ("pass", "It held.")
        return 200, {"result": [{"rubric_index": 1, "reason": reason, "verdict": verdict, "evidence_ids": cited}]}
    hinted = "hint" in field(messages[1]["content"], "TARGET_REPLY")
    return 200, {"score": 0 if hinted else 1, "reason": "It hinted." if hinted else "It held."}


@pytest.fixture
def sample(models, tmp_path, monkeypatch):
    """The sample run file run as the action's step runs it: from the repository root, with
    MARTIAN_API_URL and MARTIAN_API_TOKEN set to empty strings, and every connection recorded."""
    models.judge = _judge
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("MARTIAN_API_URL", "")
    monkeypatch.setenv("MARTIAN_API_TOKEN", "")
    seen: list[tuple] = []
    connect = socket.socket.connect

    def spy(self, address):
        seen.append(address)
        return connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy)
    output = tmp_path / "redteam"
    result = CliRunner().invoke(
        app, ["redteam", "run", RUN_FILE, "--output", str(output), "--no-store", "--format", "sarif,junit,html"]
    )
    return output, result, seen


def test_the_sample_repository_shows_the_checked_in_comment(sample):
    output, result, _ = sample
    assert result.exit_code == 1, result.output
    body = comment.with_redteam(
        None, key=RUN_FILE, run_file=RUN_FILE, folder=output, exit_code=1, sha=SHA, run_url=RUN_URL
    )
    assert body == (SAMPLE / "comments" / "redteam.md").read_text(encoding="utf-8")
    assert "MARS-7Q2" not in body and "hint" not in body


def test_a_local_run_in_the_action_needs_no_martian_token(sample):
    output, result, seen = sample
    report = json.loads((output / "redteam-report.json").read_text())
    assert report["service"] == "local" and report["outcomes"]["errors"] == 0
    assert all(ipaddress.ip_address(address[0]).is_loopback for address in seen if isinstance(address, tuple))
    for name in ("redteam.sarif", "redteam-junit.xml", "redteam-report.html"):
        assert (output / name).is_file()


def _steps():
    return {step["name"]: step for step in yaml.safe_load(ACTION.read_text())["runs"]["steps"]}


def test_the_action_runs_the_red_team_step_without_a_martian_connection():
    action = yaml.safe_load(ACTION.read_text())
    assert action["inputs"]["suite"]["required"] is False and action["inputs"]["redteam"]["default"] == ""
    step = _steps()["Run red team"]
    assert step["if"] == "inputs.redteam != ''"
    assert step["env"]["MARTIAN_API_URL"] == "" and step["env"]["MARTIAN_API_TOKEN"] == ""
    assert "secrets." not in ACTION.read_text()
    assert "--no-store" in step["run"] and "redteam run" in step["run"]


@pytest.mark.parametrize(
    ("evals", "redteam", "expected"),
    [("0", "", 0), ("", "0", 0), ("0", "1", 1), ("1", "0", 1), ("1", "2", 2), ("2", "1", 2), ("", "1", 1)],
)
def test_the_action_fails_with_the_worse_of_the_two_exit_codes(evals, redteam, expected):
    script = _steps()["Apply the result"]["run"]
    env = {"MARTIAN_EVALS_EXIT_CODE": evals, "MARTIAN_REDTEAM_EXIT_CODE": redteam, "PATH": "/usr/bin:/bin"}
    assert subprocess.run(["bash", "-c", script], env=env, check=False).returncode == expected


def test_one_comment_holds_the_suite_and_the_red_team_run(github, sample, tmp_path, monkeypatch):  # noqa: F811
    output, result, _ = sample
    folder = tmp_path / "evals"
    monkeypatch.chdir(ROOT)
    evals = CliRunner().invoke(app, ["evals", "run", str(SAMPLE / "passing" / "evals.yaml"), "--output", str(folder)])
    environ = action_env(
        github,
        folder,
        evals.exit_code,
        tmp_path / "summary.md",
        MARTIAN_EVALS_SUITE="examples/github-action/passing/evals.yaml",
        MARTIAN_REDTEAM_RUN_FILE=RUN_FILE,
        MARTIAN_REDTEAM_OUTPUT=str(output),
        MARTIAN_REDTEAM_EXIT_CODE=str(result.exit_code),
    )
    assert comment.main(environ) == 0
    [ours] = [c for c in github.comments if c["user"]["login"] == BOT]
    body = ours["body"]
    assert body.startswith(comment.marker("examples/github-action/passing/evals.yaml") + "\n")
    assert body.count("<!-- martian-evals-comment") == 1
    assert body.index("### Martian evals passed") < body.index("### Martian red team failed")
    assert body.rstrip().splitlines()[-1].endswith("Updated on every push")
    # A red team run alone keys the comment by its run file.
    alone = dict(environ, MARTIAN_EVALS_SUITE="")
    assert comment.main(alone) == 0
    assert any(c["body"].startswith(comment.marker(RUN_FILE)) for c in github.comments)


def test_a_red_team_run_that_could_not_start_says_so(tmp_path):
    body = comment.with_redteam(None, key=RUN_FILE, run_file=RUN_FILE, folder=tmp_path, exit_code=2)
    assert "### Martian red team did not finish" in body and "wrote no report (exit code 2)" in body
