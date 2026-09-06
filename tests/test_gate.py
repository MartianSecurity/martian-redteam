from pathlib import Path

import pytest

from martian_redteam.gate import GateFailure, evaluate_gate
from martian_redteam.types import RiskAssessment, TestCase, VulnerabilityTypeResult


def _gated_result(**kwargs: object) -> VulnerabilityTypeResult:
    defaults: dict[str, object] = {
        "vulnerability": "prompt_leakage",
        "vulnerability_type": "system_prompt",
        "pass_rate": 1.0,
        "passing": 1,
        "failing": 0,
        "errored": 0,
        "gated": True,
    }
    defaults.update(kwargs)
    return VulnerabilityTypeResult(**defaults)  # type: ignore[arg-type]


def test_infrastructure_error_fails_gate() -> None:
    assessment = RiskAssessment(
        catalog_version="0.1.0",
        test_cases=[
            TestCase(
                vulnerability="prompt_leakage",
                vulnerability_type="system_prompt",
                attack_method="prompt_injection",
                error="HTTP 500",
                error_kind="infrastructure",
            )
        ],
        errored=1,
        infrastructure_errors=1,
        vulnerability_type_results=[_gated_result(pass_rate=0.0, passing=0, errored=1)],
    )
    with pytest.raises(GateFailure, match="infrastructure"):
        evaluate_gate(assessment)


def test_target_error_fails_unless_ignored() -> None:
    assessment = RiskAssessment(
        catalog_version="0.1.0",
        test_cases=[
            TestCase(
                vulnerability="toxicity",
                vulnerability_type="insults",
                attack_method="prompt_injection",
                error="target callback failed: boom",
                error_kind="target",
            )
        ],
        errored=1,
        target_errors=1,
        vulnerability_type_results=[
            VulnerabilityTypeResult(
                vulnerability="toxicity",
                vulnerability_type="insults",
                pass_rate=0.0,
                passing=0,
                failing=0,
                errored=1,
                gated=False,
            )
        ],
    )
    with pytest.raises(GateFailure, match="target callback"):
        evaluate_gate(assessment)
    evaluate_gate(assessment, ignore_target_errors=True)


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
        vulnerability_type_results=[_gated_result(pass_rate=0.0, passing=0, failing=1)],
    )
    with pytest.raises(GateFailure, match="pass rate"):
        evaluate_gate(assessment, pass_rate_threshold=1.0)


def test_passing_gated_assessment() -> None:
    evaluate_gate(
        RiskAssessment(
            catalog_version="0.1.0",
            test_cases=[
                TestCase(
                    vulnerability="prompt_leakage",
                    vulnerability_type="system_prompt",
                    attack_method="prompt_injection",
                    score=1,
                    reason="refused",
                )
            ],
            vulnerability_type_results=[_gated_result()],
        )
    )


def test_save_writes_json(tmp_path: Path) -> None:
    path = RiskAssessment(catalog_version="0.1.0").save(tmp_path)
    assert path.exists()
    assert "catalog_version" in path.read_text(encoding="utf-8")
