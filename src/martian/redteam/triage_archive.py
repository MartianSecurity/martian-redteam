"""Portable project review history tied to saved Red Team observations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from martian.evals.model import digest
from martian.evals.records.sql import RedteamTriageEntry
from martian.evals.store import StoreError
from martian.redteam.findings import (
    Digest,
    Identifier,
    Observation,
    TriageNote,
    build_inventory,
    read_inventory,
)
from martian.redteam.report.verification import RemediationVerification
from martian.redteam.verified_fixes import saved_comparison, verification_reference

if TYPE_CHECKING:
    from martian.evals.records.sql import SqlRecordStore
    from martian.evals.server.principal import Principal


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewObservation(_Model):
    run_id: Identifier
    finding_id: Identifier
    probe_id: Identifier
    started_at: AwareDatetime

    @classmethod
    def from_observation(cls, observation: Observation) -> Self:
        return cls(
            run_id=observation.run_id,
            finding_id=observation.finding_id,
            probe_id=observation.probe_id,
            started_at=observation.started_at,
        )


class ReviewHistory(_Model):
    finding_id: Digest
    observations: tuple[ReviewObservation, ...] = Field(min_length=1)
    notes: tuple[TriageNote, ...] = Field(min_length=1)


class TriageArchive(_Model):
    format: Literal["martian-findings-triage"]
    version: Literal[1]
    project_id: Identifier
    findings: tuple[ReviewHistory, ...]
    sha256: Digest

    @model_validator(mode="after")
    def check_manifest(self) -> Self:
        if digest(self.model_dump(mode="json", exclude={"sha256"})) != self.sha256:
            raise ValueError("Triage archive checksum does not match")
        if len({item.finding_id for item in self.findings}) != len(self.findings):
            raise ValueError("Triage archive repeats an inventory finding")
        identities: set[str] = set()
        for item in self.findings:
            for note in item.notes:
                if str(UUID(note.id)) != note.id or note.id in identities:
                    raise ValueError("Triage review IDs must be unique canonical UUIDs")
                if note.created_at.utcoffset() is None:
                    raise ValueError("Triage review timestamps require a timezone")
                if note.verified_fix is not None and note.status != "fixed":
                    raise ValueError("A verified fix requires a fixed review")
                identities.add(note.id)
        return self


class TriageImport(_Model):
    project_id: Identifier
    findings: int
    added: int
    reused: int
    dry_run: bool


async def export_triage(
    records: SqlRecordStore, principal: Principal, project_id: str, *, finding_id: str | None = None
) -> tuple[TriageArchive, int]:
    _, observations, history = await records.read_redteam_inventory(principal, project_id)
    inventory = build_inventory(project_id, observations, history)
    if finding_id is not None and finding_id not in inventory:
        raise StoreError("not_found", "Finding not found in this project")
    included = [
        ReviewHistory(
            finding_id=key,
            observations=tuple(ReviewObservation.from_observation(obs) for obs in detail.observations),
            notes=tuple(detail.notes),
        ).model_dump(mode="json")
        for key, detail in sorted(inventory.items())
        if detail.notes and (finding_id is None or key == finding_id)
    ]
    contents = {"format": "martian-findings-triage", "version": 1, "project_id": project_id, "findings": included}
    skipped = sum(key not in inventory for key, _ in history) if finding_id is None else 0
    return TriageArchive.model_validate({**contents, "sha256": digest(contents)}), skipped


async def import_triage(
    records: SqlRecordStore, principal: Principal, project_id: str, archive: TriageArchive, *, dry_run: bool = False
) -> TriageImport:
    if project_id != archive.project_id:
        raise StoreError("conflict", "Triage archive belongs to a different project")
    _, inventory = await read_inventory(records, principal, project_id)
    comparisons: dict[tuple[str, str], tuple[RemediationVerification, datetime]] = {}
    entries: list[RedteamTriageEntry] = []
    for item in archive.findings:
        detail = inventory.get(item.finding_id)
        if detail is None or not set(item.observations).intersection(
            ReviewObservation.from_observation(obs) for obs in detail.observations
        ):
            raise StoreError("conflict", f"Restore a matching scan observation for finding {item.finding_id} first")
        for note in item.notes:
            if proof := note.verified_fix:
                pair = (proof.baseline_run_id, proof.candidate_run_id)
                if pair not in comparisons:
                    comparisons[pair] = await saved_comparison(records, principal, project_id, *pair)
                comparison, started = comparisons[pair]
                verified = verification_reference(detail, comparison, started, proof.scan_finding_id)
                if verified != proof:
                    raise StoreError("conflict", f"Saved scans differ from the proof for review {note.id}")
            entries.append(
                RedteamTriageEntry(
                    item.finding_id, note.id, note.created_at.astimezone(UTC), note.model_dump(mode="json")
                )
            )
    added, reused = await records.restore_redteam_triage(principal, project_id, entries, dry_run=dry_run)
    return TriageImport(
        project_id=project_id, findings=len(archive.findings), added=added, reused=reused, dry_run=dry_run
    )
