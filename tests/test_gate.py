from pathlib import Path

from martian_redteam.gate import GateFailure, evaluate_gate
from martian_redteam.types import RiskAssessment, TestCase, VulnerabilityTypeResult


def test_infrastructure_error_fails_gate() -> None:
    assessment = RiskAssessment(
        catalog_version="0.1.0",
        test_cases=[
            TestCase(
                vulnerability="prompt_leakage",
                vulnerability_type="system_prompt",
                attack_method="prompt_injection",
                error="HTTP 500",
            )
        ],
        errored=1,
        vulnerability_type_results=[
            VulnerabilityTypeResult(
                vulnerability="prompt_leakage",
                vulnerability_type="system_prompt",
                pass_rate=0.0,
                passing=0,
                failing=0,
                errored=1,
                gated=True,
            )
        ],
    )
    try:
        evaluate_gate(assessment)
    except GateFailure:
        return
    raise AssertionError("expected GateFailure")


def test_gated_pass_rate_below_threshold() -> None:
    assessment = RiskAssessment(
        catalog_version="0.1.0",
        test_cases=[
            TestCase(
                vulnerability="prompt_leakage",
                vulnerability_type="system_prompt",
                attack_method="prompt_injection",
                score=0,
                reason="leaked",
            )
        ],
        vulnerability_type_results=[
            VulnerabilityTypeResult(
                vulnerability="prompt_leakage",
                vulnerability_type="system_prompt",
                pass_rate=0.0,
                passing=0,
                failing=1,
                errored=0,
                gated=True,
            )
        ],
    )
    try:
        evaluate_gate(assessment, pass_rate_threshold=1.0)
    except GateFailure as error:
        assert "pass rate" in str(error)
        return
    raise AssertionError("expected GateFailure")


def test_save_writes_json(tmp_path: Path) -> None:
    path = RiskAssessment(catalog_version="0.1.0").save(tmp_path)
    assert path.exists()
    assert "catalog_version" in path.read_text(encoding="utf-8")
