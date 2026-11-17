"""Maps from the red team formats that exist today onto :mod:`martian.redteam.model`, and back.

- :func:`from_risk_assessment` and :func:`to_risk_assessment`: the version 0.2 SDK report
  (``risk-assessment.json``, :class:`martian_redteam.types.RiskAssessment`). The round trip keeps
  every field.
- :func:`from_ci_report`: the red team rows an SDK CI session records
  (:class:`martian.evals.types.RedteamReport`).
- :func:`from_campaign`: the hosted engine's campaign summary, scored attempts and verification
  results, as JSON.

``docs/redteam-model.md`` lists each source field and where it goes, including those with no home in
the model, which are kept in ``RedteamRun.extra``.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from martian.redteam.model import (
    Attempt,
    AttemptOutcome,
    CheckResult,
    Counts,
    EvidenceLevel,
    EvidenceTier,
    Finding,
    ObjectiveClass,
    Probe,
    RedteamRun,
    RunSource,
    RunState,
    ToolCall,
    Turn,
    Verification,
    VerificationOutcome,
)
from martian.redteam.verify.rules import counted_as, label, supported
from martian_redteam.catalog.entries import ENTRIES

OBJECTIVES: dict[str, ObjectiveClass] = {item.id: item.objective for item in ENTRIES.values()}
"""The objective class of each built-in vulnerability, from its catalogue entry. A custom
vulnerability is a ``control`` objective: its criteria judge what the target said."""

JUDGE = "judge"
"""The check id of the hosted judge, the only check a version 0.2 probe has."""

_ROLES_OUT = {"human": "attacker", "ai": "target", "tool": "tool"}
_ROLES_IN = {value: key for key, value in _ROLES_OUT.items()}


def objective_of(vulnerability: str) -> ObjectiveClass:
    return OBJECTIVES.get(vulnerability, "control")


def probe_id(vulnerability: str, vulnerability_type: str, attack_method: str) -> str:
    return f"{vulnerability}:{vulnerability_type}:{attack_method}"


def counts_of(run_attempts: Sequence[Attempt], findings: Sequence[Finding]) -> Counts:
    """The run's counts. Attempts are its attacks; repetitions and controls are reported in its
    verifications."""
    run_attempts = [attempt for attempt in run_attempts if attempt.role == "attack"]
    outcomes = Counter(attempt.outcome for attempt in run_attempts)
    kinds = Counter(attempt.error_kind for attempt in run_attempts if attempt.outcome == "error")
    return Counts(
        attempts=len(run_attempts),
        hits=outcomes["hit"],
        resisted=outcomes["resisted"],
        undetermined=outcomes["undetermined"],
        errors=outcomes["error"],
        infrastructure_errors=kinds["infrastructure"],
        target_errors=kinds["target"],
        attacker_errors=kinds["attacker"],
        judge_errors=kinds["judge"],
        stopped=outcomes["stopped"],
        generations=sum(attempt.spend.attacker_calls for attempt in run_attempts if attempt.spend is not None),
        refusals=sum(attempt.attacker_refusals for attempt in run_attempts),
        confirmed=sum(finding.counted_as == "confirmed" for finding in findings),
        attempted=sum(finding.counted_as == "attempted" for finding in findings),
        suspected=sum(finding.counted_as == "suspected" for finding in findings),
    )


def findings_of(
    probes: Sequence[Probe], attempts: Sequence[Attempt], verifications: Sequence[Verification]
) -> list[Finding]:
    """One finding per probe whose attacks hit, at the highest level they reached. A probe whose
    negative control showed the same signal has no finding: the target did it without the attack."""
    outcome = {item.probe_id: item.outcome for item in verifications}
    reproduced = {
        item.probe_id: f"Reproduced {item.successful_reproductions} of {item.repetitions}."
        for item in verifications
        if item.repetitions
    }
    discarded = {item.probe_id for item in verifications if item.negative_control_passed is False}
    found: list[Finding] = []
    for probe in probes:
        hits = [
            attempt
            for attempt in attempts
            if attempt.probe_id == probe.id and attempt.outcome == "hit" and attempt.role == "attack"
        ]
        if not hits or probe.id in discarded:
            continue
        top = max(attempt.level for attempt in hits if attempt.level is not None)
        executed = any(
            result.executed
            for attempt in hits
            for result in attempt.check_results
            if result.hit and result.level == EvidenceLevel.TOOL_CALL
        )
        results = [result for attempt in hits for result in attempt.check_results]
        at_three = [result for result in results if result.hit and supported(result) == EvidenceLevel.TOOL_CALL]
        contact = top == EvidenceLevel.TOOL_CALL and bool(at_three) and all(r.kind == "receipt" for r in at_three)
        counted = counted_as(results, top) or "confirmed"
        found.append(
            Finding(
                id=f"finding-{probe.id}",
                probe_id=probe.id,
                vulnerability=probe.vulnerability,
                vulnerability_type=probe.vulnerability_type,
                objective_class=probe.objective_class,
                evidence_level=top,
                label=label(top, executed=executed, objective=probe.objective_class, contact=contact),
                counted_as=counted,
                outcome=outcome.get(probe.id),
                summary=reproduced.get(probe.id, ""),
                attempt_ids=[attempt.id for attempt in hits],
            )
        )
    return found


# --- version 0.2 SDK report --------------------------------------------------------------------


def _judged(score: int | None, reason: str | None) -> list[CheckResult]:
    """The hosted judge's verdict as a check result. Score 0 means the target was vulnerable."""
    if score is None and reason is None:
        return []
    hit = None if score is None else score == 0
    return [
        CheckResult(
            check_id=JUDGE,
            kind="judge",
            hit=hit,
            level=EvidenceLevel.JUDGED if hit else None,
            rationale=reason or "",
        )
    ]


def from_risk_assessment(
    assessment: Any,
    *,
    run_id: str | None = None,
    project_id: str = "default",
    source: RunSource = "local",
) -> RedteamRun:
    """A version 0.2 ``RiskAssessment`` as a red team run. Each test case is one attempt of the probe
    named by its vulnerability, type and attack method; the hosted judge's score is its one check."""
    from martian_redteam.orchestrator import _summarize
    from martian_redteam.types import RiskAssessment

    if not isinstance(assessment, RiskAssessment):
        assessment = RiskAssessment.model_validate(assessment)
    gated = {
        (group.vulnerability, group.vulnerability_type): group.gated for group in assessment.vulnerability_type_results
    }
    planned: Counter[str] = Counter()
    probes: dict[str, Probe] = {}
    attempts: list[Attempt] = []
    for index, case in enumerate(assessment.test_cases, start=1):
        key = probe_id(case.vulnerability, case.vulnerability_type, case.attack_method)
        planned[key] += 1
        if key not in probes:
            probes[key] = Probe(
                id=key,
                vulnerability=case.vulnerability,
                vulnerability_type=case.vulnerability_type,
                attack_method=case.attack_method,
                objective_class=objective_of(case.vulnerability),
                gated=gated.get((case.vulnerability, case.vulnerability_type), False),
            )
        results = _judged(case.score, case.reason)
        outcome: AttemptOutcome
        if case.error is not None or case.error_kind is not None:
            outcome = "error"
        elif case.score == 0:
            outcome = "hit"
        elif case.score == 1:
            outcome = "resisted"
        else:
            outcome = "undetermined"
        operations = {
            name: value
            for name, value in (
                ("simulate", case.simulation_operation_id),
                ("evaluate", case.evaluation_operation_id),
            )
            if value is not None
        }
        attempts.append(
            Attempt(
                id=f"attempt-{index:04d}",
                probe_id=key,
                number=planned[key],
                outcome=outcome,
                level=EvidenceLevel.JUDGED if outcome == "hit" else None,
                input=case.input,
                output=case.actual_output,
                turns=[
                    Turn(
                        role=_ROLES_OUT[turn.role],  # type: ignore[arg-type]
                        content=turn.content,
                        tool_calls=[ToolCall.model_validate(call.model_dump()) for call in turn.tool_calls]
                        or [ToolCall(name=name) for name in turn.tools_called],
                    )
                    for turn in case.turns
                ],
                check_results=results,
                error=case.error or "",
                error_kind=case.error_kind,
                error_code=case._error_code,
                graph_revision=case.graph_revision,
                policy_revision=case.policy_revision,
                operations=operations,
            )
        )
    probe_list = [probe.model_copy(update={"planned_attempts": planned[probe.id]}) for probe in probes.values()]
    findings = findings_of(probe_list, attempts, [])
    extra: dict[str, Any] = {"report_version": "0.2"}
    # The summaries and error counts follow from the test cases. A report whose own differ keeps them.
    vulnerabilities = [
        _Gated(id=group.vulnerability, gated=group.gated) for group in assessment.vulnerability_type_results
    ]
    recomputed = _summarize(
        list(assessment.test_cases),
        purpose=assessment.purpose or "",
        duration=assessment.run_duration_seconds,
        graph_revision=assessment.graph_revision,
        vulnerabilities=vulnerabilities,  # type: ignore[arg-type]
        started_at=assessment.started_at,
        expected_probe_count=assessment.expected_probe_count,
    )
    for field in ("vulnerability_type_results", "errored", "infrastructure_errors", "target_errors"):
        mine, theirs = getattr(assessment, field), getattr(recomputed, field)
        if mine != theirs:
            extra[field] = [item.model_dump(mode="json") for item in mine] if isinstance(mine, list) else mine
    return RedteamRun(
        id=run_id or f"redteam-{uuid.uuid4().hex[:12]}",
        project_id=project_id,
        source=source,
        state="completed",
        catalog_version=assessment.catalog_version,
        purpose=assessment.purpose,
        graph_revision=assessment.graph_revision,
        policy_revision=assessment.policy_revision,
        started_at=assessment.started_at,
        duration_seconds=assessment.run_duration_seconds,
        expected_attempts=assessment.expected_probe_count,
        probes=probe_list,
        attempts=attempts,
        findings=findings,
        counts=counts_of(attempts, findings),
        extra=extra,
    )


class _Gated:
    """Just enough of a catalogue vulnerability for ``_summarize`` to read its gate."""

    def __init__(self, *, id: str, gated: bool) -> None:
        self.id = id
        self.gated = gated


def _score_of(attempt: Attempt) -> Literal[0, 1] | None:
    judge = next((result for result in attempt.check_results if result.kind == "judge"), None)
    hit = judge.hit if judge is not None else {"hit": True, "resisted": False}.get(attempt.outcome)
    if hit is None:
        return None
    return 0 if hit else 1


def _legacy_calls(calls: Sequence[ToolCall]) -> list[Any]:
    """A turn's tool calls as a version 0.2 turn keeps them. Calls that are names only stay names
    (``tools_called``), as a version 0.2 report had them, so a report read in and written back is
    unchanged."""
    from martian_redteam.types import ToolCallRecord

    if all(call == ToolCall(name=call.name) for call in calls):
        return []
    return [ToolCallRecord.model_validate(call.model_dump()) for call in calls]


def _legacy_error(attempt: Attempt) -> str | None:
    if attempt.outcome == "stopped":
        return attempt.error or f"stopped: the budget's {attempt.stopped_by} ran out"
    return attempt.error or None if attempt.outcome == "error" or attempt.error else None


def _legacy_kind(attempt: Attempt) -> Literal["infrastructure", "target"] | None:
    """The version 0.2 report knows target and infrastructure errors only: a failure of the attacker
    or the judge model, or a stop by the budget, is not the target's, so it is an infrastructure error."""
    if attempt.outcome == "stopped":
        return "infrastructure"
    if attempt.error_kind is None:
        return None
    return "target" if attempt.error_kind == "target" else "infrastructure"


def to_risk_assessment(run: RedteamRun) -> Any:
    """A red team run as a version 0.2 ``RiskAssessment``. Only attack attempts become test cases:
    the format has no place for repetitions or controls."""
    from martian_redteam.orchestrator import _summarize
    from martian_redteam.types import RiskAssessment, TestCase, VulnerabilityTypeResult
    from martian_redteam.types import Turn as LegacyTurn

    cases: list[TestCase] = []
    for attempt in run.attempts:
        if attempt.role != "attack":
            continue
        probe = run.probe(attempt.probe_id)
        judge = next((result for result in attempt.check_results if result.kind == "judge"), None)
        case = TestCase(
            vulnerability=probe.vulnerability,
            vulnerability_type=probe.vulnerability_type,
            attack_method=probe.attack_method,
            input=attempt.input,
            actual_output=attempt.output,
            turns=[
                LegacyTurn(
                    role=_ROLES_IN.get(turn.role, "tool"),  # type: ignore[arg-type]
                    content=turn.content,
                    tools_called=[call.name for call in turn.tool_calls],
                    tool_calls=_legacy_calls(turn.tool_calls),
                )
                for turn in attempt.turns
            ],
            score=_score_of(attempt),
            reason=(judge.rationale or None) if judge is not None else None,
            error=_legacy_error(attempt),
            error_kind=_legacy_kind(attempt),
            graph_revision=attempt.graph_revision,
            policy_revision=attempt.policy_revision,
            simulation_operation_id=attempt.operations.get("simulate"),
            evaluation_operation_id=attempt.operations.get("evaluate"),
        )
        if attempt.error_code is not None:
            case = case._bind_error_code(attempt.error_code)
        cases.append(case)
    vulnerabilities = [_Gated(id=probe.vulnerability, gated=probe.gated) for probe in run.probes]
    summary = _summarize(
        cases,
        purpose=run.purpose or "",
        duration=run.duration_seconds,
        graph_revision=run.graph_revision,
        vulnerabilities=vulnerabilities,  # type: ignore[arg-type]
        started_at=run.started_at,
        expected_probe_count=run.expected_attempts,
    )
    kept: dict[str, Any] = {
        field: run.extra[field]
        for field in ("vulnerability_type_results", "errored", "infrastructure_errors", "target_errors")
        if field in run.extra
    }
    if "vulnerability_type_results" in kept:
        kept["vulnerability_type_results"] = [
            VulnerabilityTypeResult.model_validate(item) for item in kept["vulnerability_type_results"]
        ]
    assessment: RiskAssessment = summary.model_copy(
        update={
            # The catalogue the run was made with, not the one this SDK has.
            "catalog_version": run.catalog_version,
            "purpose": run.purpose,
            "graph_revision": run.graph_revision,
            "policy_revision": run.policy_revision,
            **kept,
        }
    )
    return assessment


# --- CI session rows ---------------------------------------------------------------------------


def from_ci_report(report: Mapping[str, Any], *, run_id: str | None = None, project_id: str = "default") -> RedteamRun:
    """The red team report of an SDK CI session (``assert_redteam``) as a red team run. A row's
    ``value`` is ``True`` when the target resisted, ``False`` when the judge found it vulnerable and
    ``None`` with ``error`` when the probe failed."""
    expected: dict[tuple[str, str], int] = {}
    gated: dict[tuple[str, str], bool] = {}
    for group in report.get("groups", []):
        pair = (group["vulnerability"], group["vulnerability_type"])
        expected[pair], gated[pair] = int(group["expected"]), bool(group["gated"])
    probes: dict[str, Probe] = {}
    numbers: Counter[str] = Counter()
    attempts: list[Attempt] = []
    for index, row in enumerate(report.get("rows", []), start=1):
        key = probe_id(row["vulnerability"], row["vulnerability_type"], row["attack_method"])
        numbers[key] += 1
        if key not in probes:
            probes[key] = Probe(
                id=key,
                vulnerability=row["vulnerability"],
                vulnerability_type=row["vulnerability_type"],
                attack_method=row["attack_method"],
                objective_class=objective_of(row["vulnerability"]),
                gated=gated.get((row["vulnerability"], row["vulnerability_type"]), False),
                planned_attempts=max(1, expected.get((row["vulnerability"], row["vulnerability_type"]), 1)),
            )
        value = row.get("value")
        outcome: AttemptOutcome
        if row.get("error"):
            outcome = "error"
        elif value is False:
            outcome = "hit"
        elif value is True:
            outcome = "resisted"
        else:
            outcome = "undetermined"
        score = None if value is None else (1 if value else 0)
        operations = {
            name: row[field]
            for name, field in (("simulate", "simulation_operation_id"), ("evaluate", "evaluation_operation_id"))
            if row.get(field)
        }
        attempts.append(
            Attempt(
                id=f"attempt-{index:04d}",
                probe_id=key,
                number=numbers[key],
                outcome=outcome,
                level=EvidenceLevel.JUDGED if outcome == "hit" else None,
                input=row.get("input") or None,
                output=row.get("output"),
                check_results=_judged(score, row.get("rationale") or None),
                error=row.get("error") or "",
                graph_revision=row.get("graph_revision") or None,
                policy_revision=row.get("policy_revision") or None,
                operations=operations,
            )
        )
    probe_list = list(probes.values())
    findings = findings_of(probe_list, attempts, [])
    return RedteamRun(
        id=run_id or f"redteam-{uuid.uuid4().hex[:12]}",
        project_id=project_id,
        source="ci",
        catalog_version=report.get("catalog_version") or "unknown",
        graph_revision=report.get("graph_revision") or None,
        policy_revision=report.get("policy_revision") or None,
        expected_attempts=sum(expected.values()) or None,
        probes=probe_list,
        attempts=attempts,
        findings=findings,
        counts=counts_of(attempts, findings),
        extra={"threshold": report.get("threshold", 1.0)},
    )


# --- hosted campaign ---------------------------------------------------------------------------

_RUN_STATES: dict[str, RunState] = {
    "created": "queued",
    "awaiting_authorization": "queued",
    "running": "running",
    "completed": "completed",
    "stopped": "stopped",
    "failed": "failed",
}

_TIER_LEVEL: dict[str, EvidenceLevel] = {
    "text": EvidenceLevel.TEXT,
    "tool": EvidenceLevel.TOOL_CALL,
    "state": EvidenceLevel.STATE,
}
_TIER_KIND = {"text": "text", "tool": "tool_call", "state": "state"}
_SCORER_HIT = {"potential_success": True, "verified": True, "consistent": True, "blocked": False}


def _campaign_role(role: str) -> str:
    if role in {"attacker", "user", "operator", "human"}:
        return "attacker"
    if role in {"target", "assistant", "ai"}:
        return "target"
    if role == "tool":
        return "tool"
    return "system"


def _campaign_attempt(item: Mapping[str, Any], index: int, probe: str, number: int) -> Attempt:
    results: list[CheckResult] = []
    for position, oracle in enumerate(item.get("oracle_results", [])):
        if not oracle.get("evaluated"):
            continue
        tier = oracle.get("tier", "text")
        hit = bool(oracle.get("success"))
        results.append(
            CheckResult(
                check_id=f"oracle-{position + 1}",
                kind=_TIER_KIND.get(tier, "text"),  # type: ignore[arg-type]
                hit=hit,
                level=_TIER_LEVEL.get(tier, EvidenceLevel.TEXT) if hit else None,
                executed=True if hit and tier == "tool" else None,
                rationale=oracle.get("summary", ""),
                evidence_ids=list(oracle.get("evidence_refs", [])),
                detail={"source": oracle.get("source", ""), "revision": oracle.get("revision", "")},
            )
        )
    status = item.get("status", "inconclusive")
    results.append(
        CheckResult(
            check_id=JUDGE,
            kind="judge",
            hit=_SCORER_HIT.get(status),
            level=EvidenceLevel.JUDGED if _SCORER_HIT.get(status) else None,
            rationale=item.get("summary", ""),
        )
    )
    from martian.redteam.verify.rules import attempt_outcome

    outcome, level = attempt_outcome(results)
    transcript = item.get("transcript", [])
    first_attack = next((m["content"] for m in transcript if _campaign_role(m.get("role", "")) == "attacker"), None)
    last_reply = next(
        (m["content"] for m in reversed(transcript) if _campaign_role(m.get("role", "")) == "target"), None
    )
    return Attempt(
        id=f"attempt-{index:04d}",
        probe_id=probe,
        number=number,
        outcome=outcome,
        level=level,
        input=first_attack,
        output=last_reply,
        turns=[Turn(role=_campaign_role(m.get("role", "")), content=m.get("content", "")) for m in transcript],  # type: ignore[arg-type]
        check_results=results,
        evidence_ids=list(item.get("evidence_refs", [])),
        target_calls=int(item.get("target_calls", 0)),
        operations={"strategy": item["strategy_id"], "conversation": item.get("conversation_id", "")},
    )


def from_campaign(
    summary: Mapping[str, Any],
    attempts: Sequence[Mapping[str, Any]] = (),
    verifications: Sequence[Mapping[str, Any]] = (),
    *,
    objective_class: ObjectiveClass,
    run_id: str | None = None,
    project_id: str = "default",
    purpose: str | None = None,
    started_at: datetime | None = None,
) -> RedteamRun:
    """A hosted campaign as a red team run: its ``CampaignSummary``, ``ScoredAttempt`` and
    ``VerificationResult`` objects as JSON. A campaign probe becomes a probe of vulnerability
    ``campaign`` whose type is the engine's probe id; an attempt that names no probe belongs to the
    probe ``campaign:discovery``. The engine's oracle results become check results of their tier,
    and its scorer's status a judge check."""
    probes: dict[str, Probe] = {}

    def probe_for(name: str | None, family: str) -> str:
        key = f"campaign:{name or 'discovery'}"
        if key not in probes:
            probes[key] = Probe(
                id=key,
                vulnerability="campaign",
                vulnerability_type=name or "discovery",
                attack_method=family or "general",
                objective_class=objective_class,
            )
        return key

    for result in summary.get("probe_results", []):
        key = probe_for(result["probe_id"], "general")
        probes[key] = probes[key].model_copy(
            update={
                "metadata": {
                    "coverage": result.get("coverage"),
                    "engine_outcome": result.get("outcome"),
                    "reason": result.get("reason"),
                }
            }
        )
    numbers: Counter[str] = Counter()
    run_attempts: list[Attempt] = []
    for index, item in enumerate(attempts, start=1):
        key = probe_for(item.get("probe_id"), item.get("attack_family", "general"))
        if probes[key].attack_method == "general" and item.get("attack_family"):
            probes[key] = probes[key].model_copy(update={"attack_method": item["attack_family"]})
        numbers[key] += 1
        run_attempts.append(_campaign_attempt(item, index, key, numbers[key]))
    by_strategy = {attempt.operations.get("strategy"): attempt for attempt in run_attempts}
    run_verifications: list[Verification] = []
    for item in verifications:
        attempt = by_strategy.get(item.get("attempt_strategy_id"))
        key = attempt.probe_id if attempt is not None else probe_for(item.get("probe_id"), "general")
        tier: EvidenceTier | None = item.get("evidence_tier")
        outcome: VerificationOutcome = (
            item["status"]
            if item["status"] in {"verified", "consistent", "not_reproducible", "unverifiable"}
            else "unverifiable"
        )
        run_verifications.append(
            Verification(
                probe_id=key,
                attempt_id=attempt.id if attempt is not None else None,
                outcome=outcome,
                reproduction_status=item.get("reproduction_status", "untested"),
                repetitions=int(item.get("repetitions", 0)),
                successful_reproductions=int(item.get("successful_reproductions", 0)),
                negative_control_passed=item.get("negative_control_passed"),
                evidence_tier=tier,
                evidence_level=_TIER_LEVEL.get(tier) if tier else None,
                ceiling_applied=bool(item.get("ceiling_applied", False)),
                summary=item.get("summary", ""),
                evidence_ids=list(item.get("evidence_refs", [])),
            )
        )
    probe_list = list(probes.values())
    findings = findings_of(probe_list, run_attempts, run_verifications)
    budget = {
        name: summary[name]
        for name in ("target_calls", "model_calls", "input_tokens", "output_tokens", "mcp_calls")
        if name in summary
    }
    extra = {
        name: summary[name]
        for name in (
            "iterations_completed",
            "attempts",
            "verified_results",
            "unverifiable_results",
            "strategy_branches",
            "exhaustion_reason",
            "authorization_gaps",
            "operator_coverage",
        )
        if name in summary
    }
    return RedteamRun(
        id=run_id or summary["campaign_id"],
        project_id=project_id,
        source="campaign",
        state=_RUN_STATES.get(summary.get("status", "completed"), "completed"),
        catalog_version=summary.get("catalog_revision") or "campaign",
        purpose=purpose,
        started_at=started_at or datetime.now().astimezone(),
        probes=probe_list,
        attempts=run_attempts,
        verifications=run_verifications,
        findings=findings,
        counts=counts_of(run_attempts, findings),
        budget=budget,
        limitations=list(summary.get("limitations", [])),
        provenance={"campaign_id": summary["campaign_id"]},
        extra=extra,
    )


def group_attempts(run: RedteamRun) -> dict[str, list[Attempt]]:
    grouped: dict[str, list[Attempt]] = defaultdict(list)
    for attempt in run.attempts:
        grouped[attempt.probe_id].append(attempt)
    return dict(grouped)


__all__ = [
    "JUDGE",
    "OBJECTIVES",
    "counts_of",
    "findings_of",
    "from_campaign",
    "from_ci_report",
    "from_risk_assessment",
    "group_attempts",
    "objective_of",
    "probe_id",
    "to_risk_assessment",
]
