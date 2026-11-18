"""Creating and archiving projects: a port of ``POST /projects`` and ``POST /projects/{id}/archive``
in the server's ``app/api/evals_explorer.py`` (MartianSecurity/MartianBackend) onto the store.

A project's id is derived from the ``request_id`` that created it, so the same request again
returns the same project. An organisation has at most 25 projects besides the default one, as on
the server. A project with a queued or running run or scoring job is not archived.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from martian.evals.model import Project, RunState
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, StoreDep, require_capability
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id
from martian.evals.server.runs import project_row, project_runs
from martian.evals.server.write_schemas import ProjectRequest

router = APIRouter(prefix="/v1/evals", tags=["evaluation explorer"])

PROJECT_LIMIT = 25
ACTIVE: tuple[RunState, ...] = ("queued", "running")


@router.post(
    "/projects", status_code=201, response_model=None, dependencies=[Depends(require_capability("manageProjects"))]
)
async def create_project(body: ProjectRequest, principal: PrincipalDep, store: StoreDep) -> dict[str, Any]:
    project_id = str(derived_id("project", str(body.request_id)))
    if existing := await store.get_project(principal, project_id):
        if (existing.name, existing.description) != (body.name.strip(), body.description):
            raise HTTPException(409, "request_id belongs to another project request")
        return project_row(existing)
    if sum(p.id != DEFAULT_PROJECT for p in await store.list_projects(principal)) >= PROJECT_LIMIT:
        raise HTTPException(409, f"This organization has reached its {PROJECT_LIMIT}-project limit")
    created = await store.create_project(
        principal,
        Project(id=project_id, name=body.name.strip(), description=body.description, created_at=datetime.now(UTC)),
    )
    return project_row(created)


@router.post(
    "/projects/{project_id}/archive",
    response_model=None,
    dependencies=[Depends(require_capability("manageProjects"))],
)
async def archive_project(
    project_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    key = str(project_id)
    if await store.get_project(principal, key) is None:
        raise HTTPException(404, "Not found")
    if (await records.list_runs(principal, key, states=list(ACTIVE), limit=1, kind=None)).items:
        raise HTTPException(409, "Wait for the project's active evaluation before archiving")
    for run in await project_runs(records, principal, key):
        if any(j.state in ACTIVE for j in await records.list_scoring_jobs(principal, key, run.id)):
            raise HTTPException(409, "Wait for the project's active scoring before archiving")
    return project_row(await store.archive_project(principal, key))


__all__ = ["router"]
