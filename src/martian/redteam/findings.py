"""The local findings inventory, projected from recorded scan evidence and triage history."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal, get_args
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

from martian._privacy import safe_text
from martian.evals.server.projection import DEFAULT_PROJECT
from martian.evals.store import StoreError
from martian.redteam.catalog import find_entry
from martian.redteam.model import EvidenceLevel, Finding, FindingStatus, ObjectiveClass, VerificationOutcome
from martian.redteam.report.guidance import EVIDENCE_RANK, GUIDANCE, RISK_RANK, finding_kind, finding_title
from martian.redteam.targets.identity import endpoint_identity, identity_digest, known_route

if TYPE_CHECKING:
    from martian.evals.records.sql import RedteamObservationRecord, SqlRecordStore
    from martian.evals.server.principal import Principal

Status = Literal["open", "accepted", "fixed", "false_positive", "ignored"]
Severity = Literal["critical", "high", "medium", "low", "info"]
EvidenceClass = Literal["confirmed", "attempted", "suspected"]
TextFilter = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
FindingExport = Literal["csv", "sarif", "colang-v1", "colang-v2"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InventoryQuery(_Model):
    project_id: Identifier = DEFAULT_PROJECT
    q: TextFilter | None = None
    target: Digest | None = None
    status: Status | None = None
    severity: Severity | Literal["unrated"] | None = None
    category: TextFilter | None = None
    vulnerability_type: TextFilter | None = None
    evidence: EvidenceClass | None = None
    run_id: Identifier | None = None
    seen_after_fix: bool = False
    sort: Literal["severity", "recent"] = "severity"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class TriageChange(_Model):
    request_id: UUID | None = None
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] = ""
    status: Status | None = None
    severity: Severity | None = None

    @model_validator(mode="after")
    def _has_change(self) -> TriageChange:
        if not (self.comment or self.status is not None or "severity" in self.model_fields_set):
            raise ValueError("Enter a comment or choose a status or severity")
        return self


class VerificationReference(_Model):
    """The saved evidence that supported a reviewer's decision."""

    baseline_run_id: Identifier
    candidate_run_id: Identifier
    candidate_started_at: datetime
    scan_finding_id: Identifier
    covered_findings: tuple[str, ...] = Field(min_length=1)
    planned_attempts: int = Field(ge=1)


class TriageNote(_Model):
    id: str
    author: str
    created_at: datetime
    comment: str = ""
    status: Status | None = None
    severity_changed: bool = False
    severity: Severity | None = None
    verified_fix: VerificationReference | None = None

    @model_serializer(mode="wrap")
    def _without_verification_default(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        written: dict[str, object] = handler(self)
        if self.verified_fix is None:
            written.pop("verified_fix", None)
        return written


class FindingTarget(_Model):
    id: str
    name: str
    kind: str | None


class Observation(_Model):
    run_id: str
    finding_id: str
    probe_id: str
    started_at: datetime
    method: str | None
    attempt_ids: list[str]
    evidence_level: EvidenceLevel
    label: str
    counted_as: EvidenceClass
    severity: Severity | None
    status: FindingStatus
    verification: VerificationOutcome | None
    note_count: int


class InventoryFinding(_Model):
    id: str
    target: FindingTarget
    vulnerability: str
    vulnerability_type: str
    objective_class: ObjectiveClass
    title: str
    category: str
    remediation: str
    recorded_severity: Severity | None
    severity: Severity | None
    severity_overridden: bool
    status: Status
    evidence_level: EvidenceLevel
    counted_as: EvidenceClass
    evidence_label: str
    first_seen: datetime
    last_seen: datetime
    seen_after_fix: bool
    scan_count: int
    observation_count: int
    methods: list[str]


class InventoryStats(_Model):
    scans: int
    findings: int
    open: int
    confirmed: int
    attempted: int
    suspected: int
    seen_after_fix: int


class InventoryFacets(_Model):
    targets: list[FindingTarget]
    categories: list[str]
    types: list[str]


class InventoryPage(_Model):
    items: list[InventoryFinding]
    total: int
    stats: InventoryStats
    facets: InventoryFacets
    export_formats: tuple[FindingExport, ...] = get_args(FindingExport)


class InventoryDetail(_Model):
    finding: InventoryFinding
    observations: list[Observation]
    notes: list[TriageNote]


def _target(record: RedteamObservationRecord) -> FindingTarget:
    target = record.target or {}
    name, kind = target.get("name") or "", target.get("kind")
    if "://" in name:
        _, name = endpoint_identity(kind or "", name)
    if not known_route(kind, record.target_key, linked_archive=record.linked_archive):
        key = identity_digest("run", record.run_id)
    elif record.target_id:
        key = identity_digest("saved", record.target_id)
    elif record.target_key:
        key = identity_digest("adapter", record.target_key)
    else:
        # A legacy display name or purpose can be shared by different targets.
        key = identity_digest("run", record.run_id)
    return FindingTarget(id=key, name=safe_text(name or kind or "Unidentified target"), kind=kind)


def _observation(record: RedteamObservationRecord, finding: Finding) -> Observation:
    return Observation(
        run_id=record.run_id,
        finding_id=finding.id,
        probe_id=finding.probe_id,
        started_at=record.started_at.astimezone(UTC)
        if record.started_at.tzinfo
        else record.started_at.replace(tzinfo=UTC),
        method=record.method,
        attempt_ids=finding.attempt_ids,
        evidence_level=finding.evidence_level,
        label=finding.label,
        counted_as=finding.counted_as,
        severity=finding.severity,
        status=finding.status,
        verification=finding.outcome,
        note_count=len(finding.annotations),
    )


@dataclass
class _Group:
    id: str
    target: FindingTarget
    vulnerability: str
    vulnerability_type: str
    objective_class: ObjectiveClass
    title: str
    category: str
    observations: list[Observation]

    def detail(self, notes: list[TriageNote]) -> InventoryDetail:
        status: Status = "open"
        severity: Severity | None = None
        fix: TriageNote | None = None
        for note in notes:
            if note.status is not None:
                status = note.status
                fix = note if status == "fixed" else None
            if note.severity_changed:
                severity = note.severity
        best = max(self.observations, key=lambda row: (EVIDENCE_RANK[row.counted_as], row.evidence_level))
        recorded = max((row.severity for row in self.observations), key=lambda value: RISK_RANK.get(value or "", 0))
        latest = self.observations[0].started_at
        seen_after_fix = False
        if fix is not None:
            seen_after_fix = (
                latest >= fix.verified_fix.candidate_started_at
                if fix.verified_fix is not None
                else latest > fix.created_at
            )
        return InventoryDetail(
            finding=InventoryFinding(
                id=self.id,
                target=self.target,
                vulnerability=self.vulnerability,
                vulnerability_type=self.vulnerability_type,
                objective_class=self.objective_class,
                title=self.title,
                category=self.category,
                remediation=" ".join(GUIDANCE[self.category].steps),
                recorded_severity=recorded,
                severity=severity or recorded,
                severity_overridden=severity is not None,
                status=status,
                evidence_level=best.evidence_level,
                counted_as=best.counted_as,
                evidence_label=best.label,
                first_seen=self.observations[-1].started_at,
                last_seen=latest,
                seen_after_fix=seen_after_fix,
                scan_count=len({row.run_id for row in self.observations}),
                observation_count=len(self.observations),
                methods=sorted({row.method for row in self.observations if row.method}),
            ),
            observations=self.observations,
            notes=notes,
        )


def build_inventory(
    project_id: str, records: list[RedteamObservationRecord], history: list[tuple[str, dict[str, object]]]
) -> dict[str, InventoryDetail]:
    groups: dict[str, _Group] = {}
    for record in records:
        finding = Finding.model_validate(record.finding)
        target = _target(record)
        vulnerability, kind, objective = finding_kind(finding)
        key = identity_digest("finding-v1", project_id, target.id, vulnerability, kind, objective)
        if key not in groups:
            catalog = find_entry(vulnerability)
            groups[key] = _Group(
                key,
                target,
                vulnerability,
                kind,
                objective,
                finding_title(vulnerability, catalog.label if catalog else None, record.policy_name),
                catalog.group if catalog else "custom",
                [],
            )
        groups[key].observations.append(_observation(record, finding))
    notes: dict[str, list[TriageNote]] = defaultdict(list)
    for key, item in history:
        if key in groups:
            notes[key].append(TriageNote.model_validate(item))
    return {key: group.detail(notes[key]) for key, group in groups.items()}


def _matches(detail: InventoryDetail, query: InventoryQuery) -> bool:
    row = detail.finding
    if query.run_id and not any(item.run_id == query.run_id for item in detail.observations):
        return False
    if query.seen_after_fix and not row.seen_after_fix:
        return False
    filters = (
        (query.target, row.target.id),
        (query.status, row.status),
        (query.severity, row.severity or "unrated"),
        (query.category, row.category),
        (query.vulnerability_type, row.vulnerability_type),
        (query.evidence, row.counted_as),
    )
    if any(wanted is not None and wanted != actual for wanted, actual in filters):
        return False
    terms = (row.title, row.vulnerability, row.vulnerability_type, row.target.name, *row.methods)
    return not query.q or query.q.casefold() in " ".join(terms).casefold()


def select_findings(inventory: dict[str, InventoryDetail], query: InventoryQuery) -> list[InventoryFinding]:
    selected = [detail.finding for detail in inventory.values() if _matches(detail, query)]
    return sorted(
        selected,
        key=lambda row: (
            -RISK_RANK.get(row.severity or "", 0) if query.sort == "severity" else 0,
            -row.last_seen.timestamp(),
            row.id,
        ),
    )


def inventory_page(scans: int, inventory: dict[str, InventoryDetail], query: InventoryQuery) -> InventoryPage:
    rows = [detail.finding for detail in inventory.values()]
    selected = select_findings(inventory, query)
    counts = Counter(row.counted_as for row in rows)
    targets = {row.target.id: row.target for row in rows}
    return InventoryPage(
        items=selected[query.offset : query.offset + query.limit],
        total=len(selected),
        stats=InventoryStats(
            scans=scans,
            findings=len(rows),
            open=sum(row.status == "open" for row in rows),
            confirmed=counts["confirmed"],
            attempted=counts["attempted"],
            suspected=counts["suspected"],
            seen_after_fix=sum(row.seen_after_fix for row in rows),
        ),
        facets=InventoryFacets(
            targets=sorted(targets.values(), key=lambda item: (item.name.casefold(), item.id)),
            categories=sorted({row.category for row in rows}),
            types=sorted({row.vulnerability_type for row in rows}),
        ),
    )


async def read_inventory(
    records: SqlRecordStore, principal: Principal, project_id: str
) -> tuple[int, dict[str, InventoryDetail]]:
    scans, observations, notes = await records.read_redteam_inventory(principal, project_id)
    return scans, build_inventory(project_id, observations, notes)


async def read_finding(
    records: SqlRecordStore, principal: Principal, project_id: str, finding_id: str
) -> InventoryDetail:
    _, inventory = await read_inventory(records, principal, project_id)
    if finding_id not in inventory:
        raise StoreError("not_found", "Finding not found in this project")
    return inventory[finding_id]


async def record_triage(
    records: SqlRecordStore, principal: Principal, project_id: str, finding_id: str, change: TriageChange
) -> TriageNote:
    note = TriageNote(
        id=str(change.request_id or uuid4()),
        author=principal.user_id,
        created_at=datetime.now(UTC),
        comment=change.comment,
        status=change.status,
        severity_changed="severity" in change.model_fields_set,
        severity=change.severity,
    )
    saved = await records.get_redteam_triage_entry(principal, project_id, note.id) if change.request_id else None
    if saved is None:
        await read_finding(records, principal, project_id, finding_id)
        saved = await records.append_redteam_triage(
            principal, project_id, finding_id, id=note.id, at=note.created_at, entry=note.model_dump(mode="json")
        )
    saved_finding, entry = saved
    existing = TriageNote.model_validate(entry)
    if saved_finding != finding_id or existing != note.model_copy(update={"created_at": existing.created_at}):
        raise StoreError("conflict", "This request already records a different triage decision")
    return existing
