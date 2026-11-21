"""Local-only findings inventory, triage, exports and per-scan remediation."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from martian.evals.records.sql import SqlRecordStore
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, require_capability
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT
from martian.redteam.findings import (
    Digest,
    FindingExport,
    InventoryDetail,
    InventoryPage,
    InventoryQuery,
    TriageChange,
    TriageNote,
    inventory_page,
    read_finding,
    read_inventory,
    record_triage,
    select_findings,
)
from martian.redteam.model import RedteamRun
from martian.redteam.report.inventory import render_findings
from martian.redteam.report.remediation import RemediationReport, build_remediation, render_remediation_print
from martian.redteam.report.verification import (
    RemediationFollowUps,
    RemediationVerification,
    VerificationError,
    follow_up_scans,
    follow_up_unavailable,
    verify_remediation,
)
from martian.redteam.server import ProjectQuery
from martian.redteam.store import RedteamStore, redteam_header
from martian.redteam.targets.identity import recorded_target_keys
from martian.redteam.verified_fixes import RecordedFix, VerifiedFixRequest, record_verified_fix

router = APIRouter(prefix="/v1/local/redteam", tags=["local red team findings"])


def _sql_records(records: RecordsDep) -> SqlRecordStore:
    if not isinstance(records, SqlRecordStore):
        raise HTTPException(503, "The local findings inventory requires the SQL record store")
    return records


_Records = Annotated[SqlRecordStore, Depends(_sql_records)]
_Query = Annotated[InventoryQuery, Query()]

_EXPORT_TYPE = {
    "csv": "text/csv",
    "sarif": "application/sarif+json",
    "colang-v1": "application/zip",
    "colang-v2": "application/zip",
}


@router.get("/findings")
async def list_findings(query: _Query, records: _Records, principal: PrincipalDep) -> InventoryPage:
    scans, inventory = await read_inventory(records, principal, query.project_id)
    return inventory_page(scans, inventory, query)


@router.get("/findings/export/{format}")
async def export_findings(format: FindingExport, query: _Query, records: _Records, principal: PrincipalDep) -> Response:
    _, inventory = await read_inventory(records, principal, query.project_id)
    selected = select_findings(inventory, query)
    file_end = f"-{format}.zip" if format in ("colang-v1", "colang-v2") else f".{format}"
    return Response(
        render_findings(selected, format),
        media_type=_EXPORT_TYPE[format],
        headers={
            "Content-Disposition": f'attachment; filename="martian-findings{file_end}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/findings/{finding_id}")
async def get_finding(
    finding_id: Digest, records: _Records, principal: PrincipalDep, project_id: ProjectQuery = DEFAULT_PROJECT
) -> InventoryDetail:
    return await read_finding(records, principal, project_id, finding_id)


@router.post("/findings/{finding_id}/triage", status_code=201, dependencies=[Depends(require_capability("review"))])
async def add_triage_note(
    finding_id: Digest,
    body: TriageChange,
    records: _Records,
    principal: PrincipalDep,
    project_id: ProjectQuery = DEFAULT_PROJECT,
) -> TriageNote:
    return await record_triage(records, principal, project_id, finding_id, body)


def _require_finished(run: RedteamRun | None) -> RedteamRun:
    if run is None:
        raise HTTPException(404, "Red team run not found")
    if run.state in ("queued", "running"):
        raise HTTPException(409, "Reports are available when this scan finishes")
    return run


async def _finished_run(run_id: str, records: SqlRecordStore, principal: Principal, project_id: str) -> RedteamRun:
    return _require_finished(await RedteamStore(records).get(project_id, run_id, principal=principal))


@router.get("/runs/{run_id}/report.pdf", response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
async def scan_pdf(
    run_id: str, records: _Records, principal: PrincipalDep, project_id: ProjectQuery = DEFAULT_PROJECT
) -> Response:
    run = await _finished_run(run_id, records, principal, project_id)
    try:
        from martian.redteam.report.pdf import PDF_NAME, render_scan_pdf
    except ImportError:
        raise HTTPException(503, "PDF reports require martian-sdk[evals]") from None
    return Response(
        await asyncio.to_thread(render_scan_pdf, run),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{PDF_NAME}"', "Cache-Control": "no-store"},
    )


@router.get("/runs/{run_id}/remediation")
async def remediation_report(
    run_id: str, records: _Records, principal: PrincipalDep, project_id: ProjectQuery = DEFAULT_PROJECT
) -> RemediationReport:
    return build_remediation(await _finished_run(run_id, records, principal, project_id))


@router.get("/runs/{run_id}/remediation/follow-ups")
async def remediation_follow_ups(
    run_id: str,
    records: _Records,
    principal: PrincipalDep,
    project_id: ProjectQuery = DEFAULT_PROJECT,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RemediationFollowUps:
    record = await records.get_run(principal, project_id, run_id)
    baseline = _require_finished(
        RedteamRun.model_validate(redteam_header(record)) if record is not None and record.kind == "redteam" else None
    )
    if reason := follow_up_unavailable(baseline):
        return RemediationFollowUps(items=[], unavailable_reason=reason)
    saved, adapter = recorded_target_keys(baseline.provenance, baseline.target.kind if baseline.target else None)
    candidates = await records.list_redteam_target_runs(
        principal, project_id, target_id=saved, adapter_identity=adapter
    )
    return follow_up_scans(
        baseline, (RedteamRun.model_validate(redteam_header(candidate)) for candidate in candidates), limit=limit
    )


@router.get("/runs/{run_id}/remediation/verification")
async def remediation_verification(
    run_id: str,
    candidate_run_id: Annotated[str, Query(min_length=1, max_length=200)],
    records: _Records,
    principal: PrincipalDep,
    project_id: ProjectQuery = DEFAULT_PROJECT,
) -> RemediationVerification:
    baseline = await _finished_run(run_id, records, principal, project_id)
    candidate = await _finished_run(candidate_run_id, records, principal, project_id)
    try:
        return verify_remediation(baseline, candidate)
    except VerificationError as error:
        raise HTTPException(409, str(error)) from None


@router.post(
    "/runs/{run_id}/remediation/verified-fix", status_code=201, dependencies=[Depends(require_capability("review"))]
)
async def save_verified_fix(
    run_id: str,
    body: VerifiedFixRequest,
    records: _Records,
    principal: PrincipalDep,
    project_id: ProjectQuery = DEFAULT_PROJECT,
) -> RecordedFix:
    return await record_verified_fix(records, principal, project_id, run_id, body)


@router.get("/runs/{run_id}/remediation/print", response_class=HTMLResponse)
async def remediation_print(
    run_id: str, records: _Records, principal: PrincipalDep, project_id: ProjectQuery = DEFAULT_PROJECT
) -> HTMLResponse:
    report = build_remediation(await _finished_run(run_id, records, principal, project_id))
    return HTMLResponse(
        render_remediation_print(report, link_attempts=True),
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
            "Cache-Control": "no-store",
        },
    )
