from pathlib import Path

import pytest

from martian_redteam.report import append_github_summary, render_markdown
from martian_redteam.types import RiskAssessment, TestCase, VulnerabilityTypeResult


def _result(**kwargs: object) -> VulnerabilityTypeResult:
    defaults: dict[str, object] = {
        "vulnerability": "prompt_leakage",
        "vulnerability_type": "system_prompt",
        "pass_rate": 0.75,
        "passing": 3,
        "failing": 1,
        "errored": 0,
        "gated": True,
    }
    defaults.update(kwargs)
    return VulnerabilityTypeResult(**defaults)  # type: ignore[arg-type]


def _assessment() -> RiskAssessment:
    return RiskAssessment(
        catalog_version="0.1.0",
        graph_revision="sha256:abc",
        purpose="support bot",
        run_duration_seconds=12.5,
        infrastructure_errors=1,
        target_errors=1,
        errored=2,
        test_cases=[
            TestCase(
                vulnerability="prompt_leakage",
                vulnerability_type="system_prompt",
                attack_method="prompt_injection",
                score=0,
                reason="leaked the system prompt",
            ),
            TestCase(
                vulnerability="toxicity",
                vulnerability_type="insults",
                attack_method="base64",
                error="HTTP 500",
                error_kind="infrastructure",
            ),
            TestCase(
                vulnerability="prompt_leakage",
                vulnerability_type="system_prompt",
                attack_method="rot13",
                score=1,
                reason="refused",
            ),
        ],
        vulnerability_type_results=[_result()],
    )


def test_save_writes_json_and_markdown(tmp_path: Path) -> None:
    path = _assessment().save(tmp_path)
    assert path == tmp_path / "risk-assessment.json"
    assert path.exists()
    markdown_path = tmp_path / "risk-assessment.md"
    assert markdown_path.exists()
    assert "catalog_version" in path.read_text(encoding="utf-8")
    assert markdown_path.read_text(encoding="utf-8").startswith("#")


def test_markdown_contains_catalog_version_and_pass_rate() -> None:
    markdown = render_markdown(_assessment())
    assert "0.1.0" in markdown
    assert "0.75" in markdown
    assert "catalog_version" in markdown
    assert "sha256:abc" in markdown
    assert "support bot" in markdown
    assert "prompt_injection" in markdown
    assert "HTTP 500" in markdown
    assert "leaked the system prompt" in markdown


def test_markdown_truncates_long_probe_detail() -> None:
    long_reason = "x" * 400
    assessment = RiskAssessment(
        catalog_version="0.1.0",
        test_cases=[
            TestCase(
                vulnerability="prompt_leakage",
                vulnerability_type="system_prompt",
                attack_method="prompt_injection",
                score=0,
                reason=long_reason,
            )
        ],
        vulnerability_type_results=[_result(pass_rate=0.0, passing=0, failing=1)],
    )
    markdown = render_markdown(assessment)
    assert long_reason not in markdown
    assert "x" * 200 in markdown
    assert markdown.count("x") < 400


def test_append_github_summary_writes_when_env_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = tmp_path / "step-summary.md"
    summary.write_text("existing\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    append_github_summary(_assessment())
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("existing\n")
    assert "0.1.0" in text
    assert "0.75" in text


def test_append_github_summary_noop_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    append_github_summary(_assessment())
    assert list(tmp_path.iterdir()) == []
