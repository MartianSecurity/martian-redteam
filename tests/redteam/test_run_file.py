"""The red team run file version 2, and version 0.2 files unchanged (martian-notes#231)."""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from martian.cli import app
from martian.redteam.runfile import RunFile, load, problems
from martian_redteam.config import RunConfig

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
GUIDE = ROOT / "docs" / "redteam-run-file.md"

VALID = {
    "version": 2,
    "target": {
        "purpose": "Refund assistant. It looks up orders and issues refunds up to 50 USD.",
        "url": "http://localhost:8300/chat",
        "body": {"message": "{{input}}"},
        "response_path": "reply",
    },
    "models": {"attacker": "openai:/attacker-model", "judge": {"model": "openai:/judge-model"}},
    "tools": {"proxy": [{"name": "commerce", "upstream": "http://localhost:9000/mcp", "mode": "record"}]},
    "vulnerabilities": [
        {
            "name": "ExcessiveAgency",
            "types": ["functionality"],
            "goal": {"tool": "issue_refund", "where": {"amount": "> 50"}},
            "checks": [{"type": "tool_call", "tool": "issue_refund", "where": {"amount": "> 50"}, "executed": True}],
            "severity": "high",
        },
        {"name": "PromptLeakage", "types": ["system_prompt"], "checks": [{"type": "canary"}, {"type": "judge"}]},
    ],
    "attacks": [{"name": "PromptInjection"}, {"name": "ROT13"}],
    "budget": {"target_queries": 300},
    "gate": {"fail_on": "confirmed", "severity": "high"},
}


def write(tmp_path: Path, data: dict, name: str = "redteam.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_a_complete_version_2_file_loads(tmp_path):
    loaded = load(write(tmp_path, VALID))
    assert isinstance(loaded, RunFile)
    assert loaded.target.kind == "http"
    assert loaded.models.attacker is not None and loaded.models.attacker.model == "openai:/attacker-model"
    assert loaded.vulnerabilities[0].checks[0].type == "tool_call"
    assert loaded.budget.target_queries == 300 and loaded.budget.attempts_per_probe == 1


@pytest.mark.parametrize("example", sorted(EXAMPLES.rglob("*.yaml")), ids=lambda path: path.name)
def test_every_red_team_example_loads_as_its_version(example):
    raw = yaml.safe_load(example.read_text("utf-8"))
    if not isinstance(raw, dict) or "vulnerabilities" not in raw or "target" not in raw:
        pytest.skip("not a red team run file")
    assert isinstance(load(example), RunFile if "version" in raw else RunConfig)
    assert problems(example) == []


def test_a_function_target_shares_the_suite_files_rules(tmp_path):
    data = {**VALID, "target": {"purpose": "Assistant", "file": "agent.py", "function": "answer", "body": {"x": 1}}}
    found = problems(write(tmp_path, data))
    assert found == ["target: body only applies to a url target"]


def test_a_url_target_without_the_input_placeholder_is_refused(tmp_path):
    data = {**VALID, "target": {"purpose": "Assistant", "url": "http://localhost:8300/chat", "body": {"m": "hi"}}}
    assert "{{input}}" in problems(write(tmp_path, data))[0]


def test_an_mcp_target(tmp_path):
    data = {**VALID, "target": {"purpose": "Assistant", "mcp": {"url": "http://localhost:9100/mcp", "tool": "chat"}}}
    assert load(write(tmp_path, data)).target.kind == "mcp"
    both = {**VALID, "target": {"purpose": "A", "file": "a.py", "mcp": {"url": "http://x/mcp", "tool": "chat"}}}
    assert problems(write(tmp_path, both)) == ["target: an mcp target takes none of file"]


def test_each_error_is_explained_with_where_it_is(tmp_path):
    data = {
        **VALID,
        "target": {"purpose": "", "url": "http://localhost:8300/chat", "body": {"message": "{{input}}"}},
        "vulnerabilities": [
            {"name": "PromptLeakage", "checks": [{"type": "text"}]},
            {"name": "ExcessiveAgency", "goal": {"where": {"amount": 5}}},
        ],
        "budget": {"target_queries": 0},
        "tools": {"proxy": [{"name": "c", "upstream": "http://x", "mode": "inject"}]},
        "surprise": True,
    }
    found = problems(write(tmp_path, data))
    assert found == [
        "target.purpose: String should have at least 1 character",
        "vulnerabilities[0].checks[0].all_of: Field required",
        "vulnerabilities[1].goal: a goal names a tool or describes the goal in text",
        "tools.proxy[0]: inject mode needs the text to inject",
        "budget.target_queries: Input should be greater than or equal to 1",
        "surprise: Extra inputs are not permitted",
    ]


def test_cross_field_rules(tmp_path):
    state = {**VALID, "vulnerabilities": [{"name": "ExcessiveAgency", "checks": [{"type": "state", "name": "refund"}]}]}
    assert problems(write(tmp_path, state)) == [
        "(file): vulnerabilities[0]: a state check reads an environment; set environment"
    ]
    assert problems(write(tmp_path, {**state, "environment": "refund-desk"})) == []
    no_judge = {**VALID, "models": {"attacker": "openai:/a"}}
    assert "needs models.judge" in problems(write(tmp_path, no_judge))[0]


def test_unknown_versions_and_fields_are_refused(tmp_path):
    assert problems(write(tmp_path, {**VALID, "version": 3})) == [
        "(file): version must be 2, or left out for a version 0.2 file"
    ]
    old = yaml.safe_load((EXAMPLES / "config.yaml").read_text("utf-8"))
    assert problems(write(tmp_path, {**old, "budget": {}})) != []


def test_validate_explains_a_version_2_file_without_calling_a_model(tmp_path, monkeypatch):
    import httpx

    def refuse(*args, **kwargs):
        raise AssertionError("validation made a network call")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    runner = CliRunner()
    ok = runner.invoke(app, ["redteam", "validate", str(write(tmp_path, VALID))])
    assert ok.exit_code == 0, ok.output
    assert "OK: version 2, 2 vulnerabilities, 2 attacks, http target" in ok.output
    bad = runner.invoke(app, ["redteam", "validate", str(write(tmp_path, {**VALID, "attacks": []}, "bad.yaml"))])
    assert bad.exit_code == 1
    assert "attacks: List should have at least 1 item" in bad.output


def test_run_stops_before_any_attack_when_a_proxied_tool_server_cannot_be_reached(tmp_path):
    unreachable = {**VALID, "tools": {"proxy": [{"name": "commerce", "upstream": "http://127.0.0.1:9/mcp"}]}}
    result = CliRunner().invoke(app, ["redteam", "run", str(write(tmp_path, unreachable)), "--no-store"])
    assert result.exit_code == 2
    assert "could not reach tools.proxy 'commerce'" in result.output


def test_the_guide_documents_every_field():
    text = GUIDE.read_text("utf-8")
    from martian.redteam import runfile

    models = [
        runfile.RunFile,
        runfile.Target,
        runfile.TargetSession,
        runfile.McpTarget,
        runfile.ModelConfig,
        runfile.Goal,
        runfile.Vulnerability,
        runfile.TextCheck,
        runfile.CanaryCheck,
        runfile.ToolCallCheck,
        runfile.JudgeCheck,
        runfile.StateCheck,
        runfile.ProxiedServer,
        runfile.Tools,
        runfile.Budget,
        runfile.Gate,
        runfile.VerificationSettings,
        runfile.Benign,
    ]
    for model in models:
        for name in model.model_fields:
            assert f"`{name}`" in text, f"{model.__name__}.{name}"
