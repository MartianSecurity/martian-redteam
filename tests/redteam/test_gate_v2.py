"""The red team gate, version 2: thresholds by evidence level and severity, warnings, and explicit
handling of undetermined attempts and errors (martian-notes#284)."""

import itertools
import json

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from martian.cli import app
from martian.redteam.gate import SEVERITY_RANK, evaluate
from martian.redteam.model import Attempt, EvidenceLevel, Finding, Probe, RedteamRun
from martian.redteam.model.maps import counts_of
from martian.redteam.runfile import Gate
from tests.redteam.test_engine import write_run

PROBES = [
    Probe(
        id=f"prompt_leakage:t{number}:prompt_injection",
        vulnerability="prompt_leakage",
        vulnerability_type=f"t{number}",
        attack_method="prompt_injection",
        objective_class="disclosure",
    )
    for number in range(1, 6)
]


def previous_gate_failures(run: RedteamRun, gate: Gate) -> list[str]:
    """The gate of the first version of the run file, as it was before version 2, kept here as the
    reference an existing gate block must still match."""
    failures: list[str] = []
    attempts = [attempt for attempt in run.attempts if attempt.role == "attack"]
    if not attempts:
        failures.append("the run has no attempt")
    if run.expected_attempts is not None and len(attempts) < run.expected_attempts:
        failures.append(f"the run made {len(attempts)} of {run.expected_attempts} planned attempts")
    lowest = EvidenceLevel.TEXT if gate.fail_on == "confirmed" else EvidenceLevel.JUDGED
    floor = SEVERITY_RANK[gate.severity] if gate.severity is not None else None
    failing = [
        finding
        for finding in run.findings
        if finding.evidence_level >= lowest
        and (floor is None or finding.severity is None or SEVERITY_RANK[finding.severity] >= floor)
    ]
    for finding in failing:
        failures.append(f"{finding.vulnerability}/{finding.vulnerability_type}: {finding.label} ({finding.probe_id})")
    kinds = {"infrastructure": 0, "attacker": 0, "judge": 0, "target": 0}
    undetermined = 0
    for attempt in attempts:
        if attempt.outcome == "error":
            kinds[attempt.error_kind or "infrastructure"] += 1
        elif attempt.outcome in ("undetermined", "stopped"):
            undetermined += 1
    if gate.fail_on_infrastructure_error:
        for kind in ("attacker", "judge", "infrastructure"):
            if kinds[kind]:
                failures.append(f"{kinds[kind]} {kind} error(s)")
    if kinds["target"] and not gate.ignore_target_errors:
        failures.append(f"{kinds['target']} target error(s)")
    if undetermined and gate.undetermined == "fail":
        failures.append(f"{undetermined} undetermined or stopped attempt(s): nothing decided them")
    return failures


def attempt(probe: Probe, number: int, outcome: str, **fields) -> Attempt:
    if outcome == "error":
        fields.setdefault("error", "failed")
    if outcome == "stopped":
        fields.setdefault("stopped_by", "target_queries")
    return Attempt(id=f"{probe.id}#{number}", probe_id=probe.id, number=number, outcome=outcome, **fields)


def finding(probe: Probe, level: int, severity: str | None) -> Finding:
    return Finding(
        id=f"finding-{probe.id}",
        probe_id=probe.id,
        vulnerability=probe.vulnerability,
        vulnerability_type=probe.vulnerability_type,
        objective_class="disclosure",
        evidence_level=EvidenceLevel(level),
        label=f"label {level}",
        severity=severity,
    )


def run_of(attempts, findings=(), expected=None) -> RedteamRun:
    return RedteamRun(
        id="run",
        catalog_version="0.2.0",
        probes=PROBES,
        attempts=list(attempts),
        findings=list(findings),
        counts=counts_of(attempts, findings),
        expected_attempts=expected if expected is not None else (len(attempts) or None),
    )


def runs() -> list[RedteamRun]:
    """Runs that exercise every rule of the first gate."""
    p1, p2, p3, p4, p5 = PROBES
    found = []
    for severities in [(None, None, None), ("low", "high", "critical"), ("info", "medium", None)]:
        findings = [
            finding(p1, 1, severities[0]),
            finding(p2, 2, severities[1]),
            finding(p3, 3, severities[2]),
        ]
        hits = [attempt(p, 1, "hit", level=EvidenceLevel(level)) for p, level in ((p1, 1), (p2, 2), (p3, 3))]
        found.append(run_of(hits, findings))
        found.append(run_of([*hits, attempt(p4, 1, "undetermined"), attempt(p5, 1, "stopped")], findings))
    found.append(run_of([attempt(p4, 1, "resisted")]))
    found.append(
        run_of(
            [
                attempt(p1, 1, "error", error_kind="attacker"),
                attempt(p1, 2, "error", error_kind="judge"),
                attempt(p2, 1, "error", error_kind="infrastructure"),
                attempt(p2, 2, "error"),
                attempt(p3, 1, "error", error_kind="target"),
                attempt(p4, 1, "undetermined"),
            ]
        )
    )
    found.append(run_of([attempt(p5, 1, "error", error_kind="target")]))
    found.append(run_of([attempt(p1, 1, "resisted")], expected=3))
    found.append(run_of([]))
    found.append(run_of([attempt(p1, 1, "resisted", role="control")], findings=[finding(p1, 5, "high")]))
    found.append(run_of([attempt(p1, 1, "hit", level=EvidenceLevel.STATE)], findings=[finding(p1, 4, "low")]))
    return found


def first_version_gates() -> list[Gate]:
    """Every gate block the first version of the run file could hold."""
    return [
        Gate(
            fail_on=fail_on,
            severity=severity,
            fail_on_infrastructure_error=infrastructure,
            ignore_target_errors=targets,
            undetermined=undetermined,
        )
        for fail_on, severity, infrastructure, targets, undetermined in itertools.product(
            ("confirmed", "suspected"),
            (None, "info", "low", "medium", "high", "critical"),
            (True, False),
            (True, False),
            ("fail", "ignore"),
        )
    ]


def test_an_existing_gate_configuration_gives_the_same_result_as_before():
    checked = 0
    for run in runs():
        for gate in first_version_gates():
            result = evaluate(run, gate)
            assert result.failures == previous_gate_failures(run, gate), (run.findings, gate)
            assert result.warnings == []
            assert result.exit_code == (1 if result.failures else 0)
            checked += 1
    assert checked == len(runs()) * 96


def suspected_only() -> RedteamRun:
    p1 = PROBES[0]
    return run_of([attempt(p1, 1, "hit", level=EvidenceLevel.JUDGED)], [finding(p1, 1, None)])


def test_a_run_with_only_suspected_results_can_be_set_to_warn():
    run = suspected_only()
    assert evaluate(run, Gate()).warnings == []
    warned = evaluate(run, Gate(warn_on="suspected"))
    assert warned.passed and warned.exit_code == 0
    assert warned.warnings == [
        "prompt_leakage/t1: label 1 (prompt_leakage:t1:prompt_injection), below fail_on (level 2)"
    ]
    assert not evaluate(run, Gate(fail_on="suspected")).passed


def test_fail_on_takes_a_level_and_findings_below_it_or_below_the_severity_can_warn():
    p1, p2, p3 = PROBES[:3]
    run = run_of(
        [attempt(p, 1, "hit", level=EvidenceLevel(level)) for p, level in ((p1, 2), (p2, 3), (p3, 4))],
        [finding(p1, 2, "critical"), finding(p2, 3, "low"), finding(p3, 4, "critical")],
    )
    result = evaluate(run, Gate(fail_on=3, severity="high", warn_on="confirmed"))
    assert result.failures == ["prompt_leakage/t3: label 4 (prompt_leakage:t3:prompt_injection)"]
    assert result.warnings == [
        "prompt_leakage/t1: label 2 (prompt_leakage:t1:prompt_injection), below fail_on (level 3)",
        "prompt_leakage/t2: label 3 (prompt_leakage:t2:prompt_injection), below the gate's severity high",
    ]
    assert evaluate(run, Gate(fail_on=5)).failures == []


def test_errors_and_undetermined_attempts_fail_warn_or_are_ignored_as_the_gate_says():
    p1, p2, p3 = PROBES[:3]
    run = run_of(
        [
            attempt(p1, 1, "error", error_kind="target"),
            attempt(p2, 1, "error", error_kind="judge"),
            attempt(p3, 1, "undetermined"),
        ]
    )
    result = evaluate(run, Gate(errors={"target": "warn", "judge": "ignore"}, undetermined="warn"))
    assert result.passed
    assert result.warnings == ["1 target error(s)", "1 undetermined or stopped attempt(s): nothing decided them"]
    assert evaluate(run, Gate(errors={"target": "fail"}, undetermined="ignore")).failures == [
        "1 judge error(s)",
        "1 target error(s)",
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"warn_on": "confirmed", "fail_on": "suspected"},
        {"warn_on": 4, "fail_on": 3},
        {"fail_on_infrastructure_error": False, "errors": {"judge": "warn"}},
        {"ignore_target_errors": True, "errors": {"target": "ignore"}},
        {"fail_on": 6},
        {"undetermined": "pass"},
    ],
)
def test_a_contradictory_or_unknown_gate_is_refused(fields):
    with pytest.raises(ValidationError):
        Gate(**fields)


def test_martian_redteam_run_warns_on_a_suspected_only_run_and_exits_0(tmp_path, models):
    judged = [{"name": "PromptLeakage", "types": ["system_prompt"], "checks": [{"type": "judge"}]}]
    path = write_run(tmp_path, vulnerabilities=judged, gate={"warn_on": "suspected"})
    result = CliRunner().invoke(app, ["redteam", "run", str(path), "--no-store"])
    assert result.exit_code == 0, result.output
    assert "Gate warning: prompt_leakage/system_prompt: Suspected" in result.output
    gate = json.loads((tmp_path / "results" / "redteam-report.json").read_text())["gate"]
    assert gate["passed"] is True and len(gate["warnings"]) == 1 and gate["config"]["warn_on"] == "suspected"
    failing = write_run(tmp_path, vulnerabilities=judged, gate={"fail_on": "suspected"})
    assert CliRunner().invoke(app, ["redteam", "run", str(failing), "--no-store"]).exit_code == 1
