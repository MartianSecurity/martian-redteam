"""Three-valued red team outcomes, error classes outside them, and the gate's undetermined setting
(martian-notes#237)."""

import json
from pathlib import Path

import pytest

from martian.redteam.engine import LocalService
from martian.redteam.engine.gate import evaluate_run_gate, gate_failures
from martian.redteam.model import Attempt, EvidenceLevel, Finding, Probe, RedteamRun
from martian.redteam.model.maps import counts_of, from_risk_assessment, to_risk_assessment
from martian.redteam.runfile import Gate
from martian_redteam import PromptInjection, PromptLeakage, a_red_team
from martian_redteam.gate import GateFailure, evaluate_gate
from martian_redteam.types import RiskAssessment

FIXTURES = Path(__file__).parent / "fixtures"
PROBE = Probe(
    id="prompt_leakage:system_prompt:prompt_injection",
    vulnerability="prompt_leakage",
    vulnerability_type="system_prompt",
    attack_method="prompt_injection",
    objective_class="disclosure",
)


def attempt(number: int, outcome: str, **fields) -> Attempt:
    if outcome == "hit":
        fields.setdefault("level", EvidenceLevel.TEXT)
    if outcome == "error":
        fields.setdefault("error", "failed")
    return Attempt(id=f"a{number}", probe_id=PROBE.id, number=number, outcome=outcome, **fields)


def run_of(*attempts: Attempt, findings: tuple[Finding, ...] = ()) -> RedteamRun:
    return RedteamRun(
        id="run",
        catalog_version="0.2.0",
        probes=[PROBE],
        attempts=list(attempts),
        findings=list(findings),
        counts=counts_of(attempts, findings),
        expected_attempts=len(attempts),
    )


async def test_an_unparseable_judgment_is_undetermined_not_an_error_or_a_pass(models):
    models.judge = lambda messages: (200, "I think it resisted.")
    assessment = await a_red_team(
        target=lambda prompt: "No.",
        vulnerabilities=[PromptLeakage(types=["system_prompt"])],
        attacks=[PromptInjection()],
        client=LocalService(attacker="openai:/a", judge="openai:/j", max_retries=0),
        purpose="Help desk",
    )
    [case] = assessment.test_cases
    assert case.score is None and case.error is None and case.error_kind is None
    assert case.reason.startswith("undetermined:")
    [converted] = from_risk_assessment(assessment).attempts
    assert converted.outcome == "undetermined"
    assert [result.hit for result in converted.check_results] == [None]
    with pytest.raises(GateFailure, match="missing an evaluator score"):
        evaluate_gate(assessment)


def test_errors_are_counted_by_whose_failure_they_were_and_rates_use_decided_attempts():
    attempts = [
        attempt(1, "hit"),
        attempt(2, "resisted"),
        attempt(3, "resisted"),
        attempt(4, "undetermined"),
        attempt(5, "error", error_kind="attacker"),
        attempt(6, "error", error_kind="judge"),
        attempt(7, "error", error_kind="target"),
        attempt(8, "error", error_kind="infrastructure"),
    ]
    counts = counts_of(attempts, [])
    assert (counts.hits, counts.resisted, counts.undetermined, counts.errors) == (1, 2, 1, 4)
    assert (counts.attacker_errors, counts.judge_errors, counts.target_errors, counts.infrastructure_errors) == (
        1,
        1,
        1,
        1,
    )
    assert counts.decided == 3
    assert counts.hit_rate == pytest.approx(1 / 3)
    assert counts_of([attempt(1, "undetermined")], []).hit_rate is None


def test_the_gate_fails_on_undetermined_by_default_and_ignores_it_only_when_told():
    run = run_of(attempt(1, "resisted"), attempt(2, "undetermined"))
    assert Gate().undetermined == "fail"
    with pytest.raises(GateFailure, match="1 undetermined or stopped attempt"):
        evaluate_run_gate(run, Gate())
    evaluate_run_gate(run, Gate(undetermined="ignore"))


def test_the_gate_names_each_class_of_error_and_its_setting():
    run = run_of(
        attempt(1, "error", error_kind="attacker"),
        attempt(2, "error", error_kind="judge"),
        attempt(3, "error", error_kind="target"),
    )
    failures = gate_failures(run, Gate())
    assert failures == ["1 attacker error(s)", "1 judge error(s)", "1 target error(s)"]
    assert gate_failures(run, Gate(fail_on_infrastructure_error=False, ignore_target_errors=True)) == []


def finding(level: EvidenceLevel, severity=None) -> Finding:
    return Finding(
        id="f",
        probe_id=PROBE.id,
        vulnerability="prompt_leakage",
        vulnerability_type="system_prompt",
        objective_class="disclosure",
        evidence_level=level,
        label="x",
        severity=severity,
    )


def test_the_gate_fails_on_confirmed_findings_and_on_suspected_ones_only_when_asked():
    suspected = run_of(attempt(1, "hit", level=EvidenceLevel.JUDGED), findings=(finding(EvidenceLevel.JUDGED),))
    confirmed = run_of(attempt(1, "hit"), findings=(finding(EvidenceLevel.TEXT, "low"),))
    assert gate_failures(suspected, Gate()) == []
    assert len(gate_failures(suspected, Gate(fail_on="suspected"))) == 1
    assert len(gate_failures(confirmed, Gate())) == 1
    assert gate_failures(confirmed, Gate(severity="high")) == []


def test_a_run_short_of_its_planned_attempts_fails():
    run = run_of(attempt(1, "resisted")).model_copy(update={"expected_attempts": 2})
    assert gate_failures(run, Gate()) == ["the run made 1 of 2 planned attempts"]


def test_old_reports_load_with_their_scores_mapped():
    report = RiskAssessment.model_validate_json((FIXTURES / "risk-assessment-0.2.json").read_text("utf-8"))
    run = from_risk_assessment(report)
    assert [(a.outcome, a.error_kind) for a in run.attempts] == [
        ("hit", None),
        ("resisted", None),
        ("hit", None),
        ("error", "target"),
        ("error", "infrastructure"),
        ("resisted", None),
    ]
    # A run stored before the attacker and judge error counts existed still loads.
    stored = json.loads(run.model_dump_json())
    for key in ("attacker_errors", "judge_errors"):
        del stored["counts"][key]
    assert RedteamRun.model_validate(stored).counts.attacker_errors == 0


def test_an_attacker_or_judge_error_is_an_infrastructure_error_in_the_old_report():
    run = run_of(attempt(1, "error", error_kind="attacker"), attempt(2, "error", error_kind="judge"))
    old = to_risk_assessment(run)
    assert [case.error_kind for case in old.test_cases] == ["infrastructure", "infrastructure"]
    assert old.infrastructure_errors == 2
