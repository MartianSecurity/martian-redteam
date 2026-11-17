"""The ``/v1/redteam`` routes of the red team core: read a run, its attempts, evidence and findings
and the catalogue; start and stop a run; annotate a finding.

Every edition serves the same routes: the local edition through ``martian ui`` and the hosted
backend by mounting this router. Their shapes are frozen in ``contract/redteam-api-v<N>.json``, with
the four SDK operation routes the hosted backend serves (simulate, evaluate, read and cancel an
operation) copied unchanged from its ``contracts/redteam-operations.json``. ``docs/redteam-api.md``
describes them; :mod:`martian.redteam.server.contract` builds and checks the file.

The routes read the evals core's record store (``request.app.state.martian_evals_records``) and its
principal, as the evals routes do. Starting and stopping a run needs a launcher
(``request.app.state.martian_redteam_launcher``, a :class:`Launcher`); an edition without one answers
``501``. Route functions have no docstrings: FastAPI would publish them in the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from martian.evals.server.dependencies import PrincipalDep, RecordsDep
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT
from martian.redteam.model import (
    Annotation,
    Attempt,
    Counts,
    Evidence,
    Finding,
    FindingStatus,
    Probe,
    RedteamRun,
    RunSource,
    RunState,
    TargetRef,
    Verification,
)
from martian.redteam.store import RedteamStore

PREFIX = "/v1/redteam"
MAX_PAGE = 200

router = APIRouter(prefix=PREFIX, tags=["redteam"])


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunSummary(_Out):
    """A red team run in a listing."""

    id: str
    project_id: str
    source: RunSource
    state: RunState
    catalog_version: str
    purpose: str | None = None
    target: TargetRef | None = None
    started_at: datetime
    finished_at: datetime | None = None
    counts: Counts


class RunPage(_Out):
    items: list[RunSummary]
    next_cursor: str | None = None


class RunOut(RunSummary):
    """A run without its attempts and evidence, which are read page by page."""

    graph_revision: str | None = None
    policy_revision: str | None = None
    duration_seconds: float = 0.0
    expected_attempts: int | None = None
    probes: list[Probe]
    verifications: list[Verification]
    findings: list[Finding]
    budget: dict[str, Any]
    limitations: list[str]


class AttemptPage(_Out):
    items: list[Attempt]
    next_cursor: str | None = None


class FindingList(_Out):
    items: list[Finding]


class CatalogVulnerability(_Out):
    name: str
    id: str
    label: str
    objective_class: str
    gated: bool
    types: list[str]


class CatalogAttack(_Out):
    name: str
    id: str
    label: str
    kind: str


class CatalogFramework(_Out):
    id: str
    coverage: str
    vulnerability_ids: list[str]


class CatalogOut(_Out):
    version: str
    vulnerabilities: list[CatalogVulnerability]
    attacks: list[CatalogAttack]
    frameworks: list[CatalogFramework]


class RunStart(_Out):
    """Start a run from a version 2 run file, given as its parsed mapping."""

    project_id: str = Field(default=DEFAULT_PROJECT, min_length=1, max_length=200)
    run_file: dict[str, Any]
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


class AnnotationIn(_Out):
    comment: str = Field(default="", max_length=4000)
    status: FindingStatus | None = None


class Launcher(Protocol):
    """Starts and stops red team runs for an edition: the local engine, or the hosted campaign."""

    async def start(self, principal: Principal, request: RunStart) -> RedteamRun: ...

    async def stop(self, principal: Principal, project_id: str, run_id: str) -> RedteamRun: ...


def _summary(run: RedteamRun) -> RunSummary:
    return RunSummary.model_validate(run.model_dump(include=set(RunSummary.model_fields)))


async def _run(records: Any, principal: Principal, project_id: str, run_id: str) -> RedteamRun:
    run = await RedteamStore(records).get(project_id, run_id, principal=principal)
    if run is None:
        raise HTTPException(404, "Red team run not found")
    return run


def _launcher(request: Request) -> Launcher:
    launcher: Launcher | None = getattr(request.app.state, "martian_redteam_launcher", None)
    if launcher is None:
        raise HTTPException(501, "This installation cannot start or stop red team runs")
    return launcher


ProjectQuery = Annotated[str, Query(min_length=1, max_length=200)]


@router.get("/runs", response_model=RunPage)
async def list_redteam_runs(
    principal: PrincipalDep,
    records: RecordsDep,
    project_id: ProjectQuery = DEFAULT_PROJECT,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    cursor: str | None = None,
) -> RunPage:
    page = await RedteamStore(records).list(project_id, principal=principal, limit=limit, cursor=cursor)
    items = []
    for record in page.items:
        header = dict(record.provenance.get("redteam", {}))
        header.update({"state": record.state, "finished_at": record.finished_at})
        items.append(RunSummary.model_validate({key: header[key] for key in RunSummary.model_fields if key in header}))
    return RunPage(items=items, next_cursor=page.next_cursor)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_redteam_run(
    run_id: str, principal: PrincipalDep, records: RecordsDep, project_id: ProjectQuery = DEFAULT_PROJECT
) -> RunOut:
    run = await _run(records, principal, project_id, run_id)
    return RunOut.model_validate(run.model_dump(include=set(RunOut.model_fields)))


@router.get("/runs/{run_id}/attempts", response_model=AttemptPage)
async def list_redteam_attempts(
    run_id: str,
    principal: PrincipalDep,
    records: RecordsDep,
    project_id: ProjectQuery = DEFAULT_PROJECT,
    probe_id: str | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
) -> AttemptPage:
    run = await _run(records, principal, project_id, run_id)
    chosen = [attempt for attempt in run.attempts if probe_id is None or attempt.probe_id == probe_id]
    page = chosen[offset : offset + limit]
    more = offset + limit < len(chosen)
    return AttemptPage(items=page, next_cursor=str(offset + limit) if more else None)


@router.get("/runs/{run_id}/attempts/{attempt_id}", response_model=Attempt)
async def get_redteam_attempt(
    run_id: str,
    attempt_id: str,
    principal: PrincipalDep,
    records: RecordsDep,
    project_id: ProjectQuery = DEFAULT_PROJECT,
) -> Attempt:
    run = await _run(records, principal, project_id, run_id)
    found = next((attempt for attempt in run.attempts if attempt.id == attempt_id), None)
    if found is None:
        raise HTTPException(404, "Attempt not found")
    return found


@router.get("/runs/{run_id}/evidence/{evidence_id}", response_model=Evidence)
async def get_redteam_evidence(
    run_id: str,
    evidence_id: str,
    principal: PrincipalDep,
    records: RecordsDep,
    project_id: ProjectQuery = DEFAULT_PROJECT,
) -> Evidence:
    run = await _run(records, principal, project_id, run_id)
    found = next((item for item in run.evidence if item.id == evidence_id), None)
    if found is None:
        raise HTTPException(404, "Evidence not found")
    return found


@router.get("/runs/{run_id}/findings", response_model=FindingList)
async def list_redteam_findings(
    run_id: str, principal: PrincipalDep, records: RecordsDep, project_id: ProjectQuery = DEFAULT_PROJECT
) -> FindingList:
    run = await _run(records, principal, project_id, run_id)
    return FindingList(items=run.findings)


@router.post("/runs/{run_id}/findings/{finding_id}/annotations", response_model=Finding)
async def annotate_redteam_finding(
    run_id: str,
    finding_id: str,
    body: AnnotationIn,
    principal: PrincipalDep,
    records: RecordsDep,
    project_id: ProjectQuery = DEFAULT_PROJECT,
) -> Finding:
    run = await _run(records, principal, project_id, run_id)
    finding = next((item for item in run.findings if item.id == finding_id), None)
    if finding is None:
        raise HTTPException(404, "Finding not found")
    if not body.comment and body.status is None:
        raise HTTPException(422, "An annotation has a comment, a status or both")
    note = Annotation(author=principal.user_id, comment=body.comment, status=body.status, created_at=datetime.now(UTC))
    changed = finding.model_copy(
        update={"annotations": [*finding.annotations, note], "status": body.status or finding.status}
    )
    findings = [changed if item.id == finding_id else item for item in run.findings]
    await RedteamStore(records).save(run.model_copy(update={"findings": findings}), principal=principal)
    return changed


@router.get("/catalog", response_model=CatalogOut)
async def get_redteam_catalog(principal: PrincipalDep) -> CatalogOut:
    from martian.redteam.catalog import ATTACKS, CATALOG_VERSION, FRAMEWORKS, VULNERABILITIES
    from martian.redteam.model.maps import objective_of

    vulnerabilities = []
    for name, factory in VULNERABILITIES.items():
        item = factory()
        vulnerabilities.append(
            CatalogVulnerability(
                name=name,
                id=item.id,
                label=item.label,
                objective_class=objective_of(item.id),
                gated=item.gated,
                types=list(item.allowed_types),
            )
        )
    attacks = [
        CatalogAttack(name=name, id=attack.id, label=attack.label, kind=attack.kind)
        for name, attack in ((name, make()) for name, make in ATTACKS.items())
    ]
    frameworks = [
        CatalogFramework(id=item.id, coverage=item.coverage, vulnerability_ids=list(item.vulnerability_ids))
        for item in FRAMEWORKS.values()
    ]
    return CatalogOut(version=CATALOG_VERSION, vulnerabilities=vulnerabilities, attacks=attacks, frameworks=frameworks)


@router.post("/runs", response_model=RunSummary, status_code=202)
async def start_redteam_run(body: RunStart, request: Request, principal: PrincipalDep) -> RunSummary:
    from pydantic import ValidationError

    from martian.redteam.runfile import RunFile

    try:
        RunFile.model_validate(body.run_file)
    except ValidationError as error:
        reasons = [
            ".".join(str(part) for part in item["loc"]) + ": " + str(item["msg"]).removeprefix("Value error, ")
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        ]
        raise HTTPException(422, "; ".join(reasons)) from None
    run = await _launcher(request).start(principal, body)
    return _summary(run)


@router.post("/runs/{run_id}/stop", response_model=RunSummary)
async def stop_redteam_run(
    run_id: str, request: Request, principal: PrincipalDep, project_id: ProjectQuery = DEFAULT_PROJECT
) -> RunSummary:
    run = await _launcher(request).stop(principal, project_id, run_id)
    return _summary(run)


__all__ = [
    "MAX_PAGE",
    "PREFIX",
    "AnnotationIn",
    "AttemptPage",
    "CatalogOut",
    "FindingList",
    "Launcher",
    "RunOut",
    "RunPage",
    "RunStart",
    "RunSummary",
    "router",
]
