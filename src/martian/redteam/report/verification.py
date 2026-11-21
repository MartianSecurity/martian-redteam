"""Verify remediation from the coverage and verdicts of two saved scans."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from heapq import nlargest
from typing import Literal

from pydantic import BaseModel, ConfigDict

from martian.redteam.model import Attempt, Finding, Probe, RedteamRun, RunState
from martian.redteam.report.guidance import RISK_RANK, FindingKind, finding_kind
from martian.redteam.report.remediation import (
    ScanFindingSummary,
    describe_finding,
    finding_priority,
    summarize_findings,
)
from martian.redteam.targets.identity import recorded_target_keys
from martian_redteam.catalog.entries import Severity

VERIFICATION_FILENAME = "redteam-remediation-verification.json"
VerificationStatus = Literal["observed", "not_observed", "inconclusive"]


class VerificationError(ValueError):
    """The two scans cannot establish a follow-up on the same target."""


class FindingVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str
    probe_id: str
    status: VerificationStatus
    reason: str
    planned_attempts: int
    decided_attempts: int
    candidate_attempt_id: str | None = None


class ComparisonCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline_probes: int
    candidate_probes: int
    matched_probes: int
    reason: str | None = None


FindingChange = Literal["new", "newly_confirmed", "higher_severity"]


class FollowUpFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str
    probe_id: str
    title: str
    vulnerability_type: str
    severity: Severity | None
    counted_as: Literal["confirmed", "attempted", "suspected"]
    change: FindingChange
    candidate_attempt_id: str | None = None


class RemediationVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline_run: str
    candidate_run: str
    findings: list[FindingVerification]
    baseline_summary: ScanFindingSummary
    candidate_summary: ScanFindingSummary
    coverage: ComparisonCoverage
    follow_up_findings: list[FollowUpFinding]


class FollowUpScan(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    purpose: str | None
    state: RunState
    started_at: datetime
    finished_at: datetime | None


class RemediationFollowUps(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[FollowUpScan]
    has_more: bool = False
    unavailable_reason: str | None = None


def _utc(time: datetime) -> datetime:
    return time.astimezone(UTC) if time.tzinfo else time.replace(tzinfo=UTC)


def follow_up_unavailable(baseline: RedteamRun) -> str | None:
    if baseline.provenance.get("rejudged_from"):
        return "Open the original scan to find later observations for this target."
    if baseline.finished_at is None:
        return "This scan has no recorded finish time."
    if _utc(baseline.finished_at) < _utc(baseline.started_at):
        return "This scan has a finish time before it started."
    if not any(recorded_target_keys(baseline.provenance, baseline.target.kind if baseline.target else None)):
        return "This scan has no recorded target identity."
    return None


def _check_pair(baseline: RedteamRun, candidate: RedteamRun) -> None:
    if baseline.project_id != candidate.project_id or baseline.id == candidate.id:
        raise VerificationError("Choose two different scans in the same project.")
    if any(run.provenance.get("rejudged_from") for run in (baseline, candidate)):
        raise VerificationError("Choose original scans. A re-judged report has no new target observations.")
    if any(run.state in ("queued", "running") for run in (baseline, candidate)):
        raise VerificationError("Both scans must finish before verification.")
    if baseline.finished_at is None or _utc(candidate.started_at) <= _utc(baseline.finished_at):
        raise VerificationError("The follow-up must have started after the baseline finished.")
    if _utc(baseline.finished_at) < _utc(baseline.started_at):
        raise VerificationError("The baseline needs a recorded finish time after it started.")
    if candidate.finished_at is None or _utc(candidate.finished_at) < _utc(candidate.started_at):
        raise VerificationError("The follow-up needs a recorded finish time after it started.")
    shared_keys = [
        (before, after)
        for before, after in zip(
            recorded_target_keys(baseline.provenance, baseline.target.kind if baseline.target else None),
            recorded_target_keys(candidate.provenance, candidate.target.kind if candidate.target else None),
            strict=True,
        )
        if before and after
    ]
    if not shared_keys:
        raise VerificationError("Both scans need a recorded identity for the same saved target or adapter.")
    if any(before != after for before, after in shared_keys):
        raise VerificationError("The recorded targets differ. Choose a follow-up for the baseline target.")


def follow_up_scans(baseline: RedteamRun, candidates: Iterable[RedteamRun], *, limit: int) -> RemediationFollowUps:
    """Suggest eligible scans newest by start time, independently of coverage or verdicts."""

    def eligible() -> Iterable[RedteamRun]:
        for candidate in candidates:
            try:
                _check_pair(baseline, candidate)
            except VerificationError:
                continue
            yield candidate

    latest = nlargest(limit + 1, eligible(), key=lambda run: (_utc(run.started_at), run.id))
    return RemediationFollowUps(
        items=[
            FollowUpScan(
                id=run.id,
                purpose=run.purpose,
                state=run.state,
                started_at=run.started_at,
                finished_at=run.finished_at,
            )
            for run in latest[:limit]
        ],
        has_more=len(latest) > limit,
    )


def _settings(run: RedteamRun) -> tuple[object, ...]:
    return (
        run.catalog_version,
        run.graph_revision,
        run.policy_revision,
        run.budget.get("limits"),
        run.provenance.get("verifiers"),
    )


def _attacks(run: RedteamRun) -> dict[str, list[Attempt]]:
    found: dict[str, list[Attempt]] = defaultdict(list)
    for attempt in run.attempts:
        if attempt.role == "attack":
            found[attempt.probe_id].append(attempt)
    return found


def _complete(attempts: list[Attempt], planned: int) -> dict[int, Attempt] | None:
    """Require one attempt in each planned slot, including any undecided attempts."""
    if len(attempts) != planned:
        return None
    found = {attempt.number: attempt for attempt in attempts}
    return found if len(found) == planned and all(1 <= number <= planned for number in found) else None


def _evidence(
    finding: Finding,
    probe: Probe,
    before: list[Attempt],
    after: list[Attempt],
    *,
    scans_completed: bool,
    same_settings: bool,
) -> str | None:
    """Why absence is inconclusive, or None when the follow-up covered the recorded finding."""
    if not scans_completed:
        return "Both scans must complete to verify that a finding did not recur."
    if not same_settings:
        return "The scan budgets or recorded verification settings differ."
    first, second = _complete(before, probe.planned_attempts), _complete(after, probe.planned_attempts)
    if first is None:
        return "The baseline lacks a complete attempt schedule for this probe."
    if second is None or any(attempt.outcome != "resisted" for attempt in second.values()):
        return "The follow-up lacks a resisted verdict for every planned attempt."
    hits = {attempt.id for attempt in before if attempt.outcome == "hit"}
    if not hits.intersection(finding.attempt_ids):
        return "The baseline finding lacks a recorded hit for this probe."
    if any(attempt.fingerprint is None for attempt in [*before, *after]):
        return "Some attempts lack a recorded configuration fingerprint."
    if any(attempt.fingerprint != second[number].fingerprint for number, attempt in first.items()):
        return "The saved attempt configurations differ."
    if probe.task_checks and any(attempt.task_completed is not True for attempt in after):
        return "The follow-up did not verify the benign task on every attempt."
    return None


def _matched_attempts(probe: Probe, before: list[Attempt], after: list[Attempt]) -> bool:
    first, second = _complete(before, probe.planned_attempts), _complete(after, probe.planned_attempts)
    if first is None or second is None:
        return False

    def decided(attempt: Attempt) -> bool:
        return attempt.outcome == "hit" or (
            attempt.outcome == "resisted" and (not probe.task_checks or attempt.task_completed is True)
        )

    return all(
        left.fingerprint is not None
        and left.fingerprint == second[number].fingerprint
        and decided(left)
        and decided(second[number])
        for number, left in first.items()
    )


def _coverage(
    baseline: RedteamRun,
    candidate: RedteamRun,
    before: dict[str, list[Attempt]],
    after: dict[str, list[Attempt]],
    *,
    same_settings: bool,
) -> ComparisonCoverage:
    first = {probe.id: probe for probe in baseline.probes}
    second = {probe.id: probe for probe in candidate.probes}
    matched = sum(
        same_settings and second.get(key) == probe and _matched_attempts(probe, before.get(key, []), after.get(key, []))
        for key, probe in first.items()
    )
    if baseline.state != "completed" or candidate.state != "completed":
        reason = "Both scans must complete for a full comparison."
    elif any(
        run.expected_attempts is not None and len(run.attempts) < run.expected_attempts for run in (baseline, candidate)
    ):
        reason = "The saved scans record fewer attempts than planned."
    elif not same_settings:
        reason = "The recorded scan settings differ."
    elif first != second:
        reason = "The scans tested different probe sets or definitions."
    elif not first:
        reason = "The scans have no recorded probes to compare."
    elif matched != len(first):
        reason = "Some probes lack matching decided attempts or successful benign tasks."
    else:
        reason = None
    return ComparisonCoverage(
        baseline_probes=len(first), candidate_probes=len(second), matched_probes=matched, reason=reason
    )


def _follow_up_findings(baseline: RedteamRun, candidate: RedteamRun) -> list[FollowUpFinding]:
    known: set[FindingKind] = set()
    confirmed: dict[FindingKind, int] = {}
    for finding in baseline.findings:
        kind = finding_kind(finding)
        known.add(kind)
        if finding.counted_as == "confirmed":
            confirmed[kind] = max(confirmed.get(kind, 0), RISK_RANK.get(finding.severity or "", 0))

    probes = {probe.id: probe for probe in candidate.probes}
    changes = []
    for finding in sorted(candidate.findings, key=finding_priority):
        kind = finding_kind(finding)
        change: FindingChange
        if kind not in known:
            change = "new"
        elif finding.counted_as != "confirmed":
            continue
        elif kind not in confirmed:
            change = "newly_confirmed"
        elif 0 < confirmed[kind] < RISK_RANK.get(finding.severity or "", 0):
            change = "higher_severity"
        else:
            continue
        detail = describe_finding(finding, probes[finding.probe_id])
        changes.append(
            FollowUpFinding(
                finding_id=detail.finding_id,
                probe_id=finding.probe_id,
                title=detail.title,
                vulnerability_type=detail.vulnerability_type,
                severity=detail.severity,
                counted_as=detail.counted_as,
                change=change,
                candidate_attempt_id=detail.example_attempt_id,
            )
        )
    return changes


def verify_remediation(baseline: RedteamRun, candidate: RedteamRun) -> RemediationVerification:
    """Compare the findings and coverage of two saved scans."""
    _check_pair(baseline, candidate)
    before, after = _attacks(baseline), _attacks(candidate)
    first = {probe.id: probe for probe in baseline.probes}
    second = {probe.id: probe for probe in candidate.probes}
    by_probe: dict[tuple[str, FindingKind], Finding] = {}
    by_kind: dict[FindingKind, Finding] = {}
    for finding in sorted(candidate.findings, key=finding_priority):
        kind = finding_kind(finding)
        by_probe.setdefault((finding.probe_id, kind), finding)
        by_kind.setdefault(kind, finding)
    same_settings = _settings(baseline) == _settings(candidate)
    rows = []
    for finding in baseline.findings:
        probe = first[finding.probe_id]
        attempted = after.get(probe.id, [])
        kind = finding_kind(finding)
        recurrence = by_probe.get((probe.id, kind)) or by_kind.get(kind)
        status: VerificationStatus = "inconclusive"
        evidence_id = None
        if probe.id not in second:
            reason = "This probe is missing from the follow-up scan."
        elif probe != second[probe.id]:
            reason = "The follow-up tested a different probe configuration."
        elif recurrence is not None:
            status = "observed"
            reason = (
                "The follow-up recorded the same finding on another probe."
                if recurrence.probe_id != probe.id
                else "The follow-up recorded the same finding on this probe."
            )
            evidence_id = next(iter(recurrence.attempt_ids), None)
        else:
            missing = _evidence(
                finding,
                probe,
                before.get(probe.id, []),
                attempted,
                scans_completed=baseline.state == candidate.state == "completed",
                same_settings=same_settings,
            )
            if missing is None:
                status = "not_observed"
                reason = "Every planned attempt had a matching configuration and the follow-up resisted each one."
            else:
                reason = missing
        rows.append(
            FindingVerification(
                finding_id=finding.id,
                probe_id=probe.id,
                status=status,
                reason=reason,
                planned_attempts=probe.planned_attempts,
                decided_attempts=len(
                    {
                        attempt.number
                        for attempt in attempted
                        if 1 <= attempt.number <= probe.planned_attempts and attempt.outcome in ("hit", "resisted")
                    }
                ),
                candidate_attempt_id=evidence_id,
            )
        )
    return RemediationVerification(
        baseline_run=baseline.id,
        candidate_run=candidate.id,
        findings=rows,
        baseline_summary=summarize_findings(baseline.findings),
        candidate_summary=summarize_findings(candidate.findings),
        coverage=_coverage(baseline, candidate, before, after, same_settings=same_settings),
        follow_up_findings=_follow_up_findings(baseline, candidate),
    )
