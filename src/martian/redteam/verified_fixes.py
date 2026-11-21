"""Record a finding decision backed by two saved scans."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from martian.evals.store import StoreError
from martian.redteam.findings import (
    Digest,
    Identifier,
    InventoryDetail,
    TriageNote,
    VerificationReference,
    read_inventory,
)
from martian.redteam.report.verification import RemediationVerification, VerificationError, verify_remediation
from martian.redteam.store import RedteamStore

if TYPE_CHECKING:
    from martian.evals.records.sql import SqlRecordStore
    from martian.evals.server.principal import Principal


class VerifiedFixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: Identifier
    candidate_run_id: Identifier
    request_id: UUID


class RecordedFix(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: Digest
    note: TriageNote


def _replay(
    saved: tuple[str, dict[str, Any]],
    principal: Principal,
    baseline_run_id: str,
    request: VerifiedFixRequest,
) -> RecordedFix:
    finding_id, raw = saved
    note = TriageNote.model_validate(raw)
    proof = note.verified_fix
    if (
        proof is None
        or note.author != principal.user_id
        or proof.baseline_run_id != baseline_run_id
        or proof.candidate_run_id != request.candidate_run_id
        or proof.scan_finding_id != request.finding_id
    ):
        raise StoreError("conflict", "This request already records a different triage decision")
    return RecordedFix(finding_id=finding_id, note=note)


def _inventory_finding(inventory: dict[str, InventoryDetail], baseline_id: str, finding_id: str) -> InventoryDetail:
    for detail in inventory.values():
        if any(obs.run_id == baseline_id and obs.finding_id == finding_id for obs in detail.observations):
            return detail
    raise StoreError("not_found", "Finding not found in the baseline scan")


async def saved_comparison(
    records: SqlRecordStore,
    principal: Principal,
    project_id: str,
    baseline_run_id: str,
    candidate_run_id: str,
) -> tuple[RemediationVerification, datetime]:
    """Read an eligible pair in the requested project and compare its saved evidence."""
    runs = RedteamStore(records)
    baseline = await runs.get(project_id, baseline_run_id, principal=principal)
    candidate = await runs.get(project_id, candidate_run_id, principal=principal)
    if baseline is None or candidate is None:
        raise StoreError("not_found", "Red team run not found in this project")
    try:
        comparison = verify_remediation(baseline, candidate)
    except VerificationError as error:
        raise StoreError("conflict", str(error)) from None
    started = candidate.started_at
    return comparison, started.astimezone(UTC) if started.tzinfo else started.replace(tzinfo=UTC)


def verification_reference(
    detail: InventoryDetail, comparison: RemediationVerification, started: datetime, scan_finding_id: str
) -> VerificationReference:
    """Check the evidence for a finding as of the follow-up."""
    observations = [obs for obs in detail.observations if obs.run_id == comparison.baseline_run]
    if not any(obs.finding_id == scan_finding_id for obs in observations):
        raise StoreError("not_found", "Finding not found in the baseline scan")
    by_finding = {row.finding_id: row for row in comparison.findings}
    coverage: dict[str, int] = {}
    for obs in observations:
        result = by_finding.get(obs.finding_id)
        if result is None or result.probe_id != obs.probe_id:
            raise StoreError("conflict", "The baseline changed during verification. Reload and compare again.")
        if result.status != "not_observed":
            raise StoreError("conflict", result.reason)
        coverage[result.probe_id] = result.planned_attempts
    if {obs.probe_id for obs in detail.observations if obs.started_at < started} - coverage.keys():
        raise StoreError("conflict", "The comparison does not cover every recorded probe for this finding")

    return VerificationReference(
        baseline_run_id=comparison.baseline_run,
        candidate_run_id=comparison.candidate_run,
        candidate_started_at=started,
        scan_finding_id=scan_finding_id,
        covered_findings=tuple(sorted(obs.finding_id for obs in observations)),
        planned_attempts=sum(coverage.values()),
    )


async def record_verified_fix(
    records: SqlRecordStore,
    principal: Principal,
    project_id: str,
    baseline_run_id: str,
    request: VerifiedFixRequest,
) -> RecordedFix:
    """Check coverage before appending a scoped and idempotent triage decision."""
    entry_id = str(request.request_id)
    if saved := await records.get_redteam_triage_entry(principal, project_id, entry_id):
        return _replay(saved, principal, baseline_run_id, request)

    comparison, started = await saved_comparison(
        records, principal, project_id, baseline_run_id, request.candidate_run_id
    )
    _, inventory = await read_inventory(records, principal, project_id)
    detail = _inventory_finding(inventory, baseline_run_id, request.finding_id)
    proof = verification_reference(detail, comparison, started, request.finding_id)
    baseline_start = max(obs.started_at for obs in detail.observations if obs.run_id == baseline_run_id)
    if any(obs.started_at >= started for obs in detail.observations):
        raise StoreError("conflict", "This finding was observed during or after the follow-up scan")
    if any(obs.started_at >= baseline_start and obs.run_id != baseline_run_id for obs in detail.observations):
        raise StoreError("conflict", "Choose the latest scan that recorded this finding as the baseline")

    current = next((note for note in reversed(detail.notes) if note.status is not None), None)
    if (
        current
        and (existing := current.verified_fix)
        and existing == proof.model_copy(update={"scan_finding_id": existing.scan_finding_id})
    ):
        return RecordedFix(finding_id=detail.finding.id, note=current)

    note = TriageNote(
        id=entry_id,
        author=principal.user_id,
        created_at=datetime.now(UTC),
        status="fixed",
        verified_fix=proof,
    )
    saved = await records.append_redteam_triage(
        principal, project_id, detail.finding.id, id=note.id, at=note.created_at, entry=note.model_dump(mode="json")
    )
    return _replay(saved, principal, baseline_run_id, request)
