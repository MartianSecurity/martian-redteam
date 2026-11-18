"""The local edition's red team routes, outside the frozen ``/v1/redteam`` contract.

``martian ui`` (:func:`martian.evals.local.create_local_app`) serves them beside the core's routes,
for the Red Team UI's setup page and its run report:

- ``POST /v1/local/redteam/runs``: start a run as ``POST /v1/redteam/runs`` does, from a version 2 run
  file, and, optionally, against one of the local edition's targets
  (:mod:`martian.evals.local_targets`), named by ``target_id``. The run file then reaches the target
  at the target's own address and names its key as ``${MARTIAN_TARGET_KEY}``; the launcher fills it
  in when it builds the target (:class:`~martian.redteam.engine.launcher.LocalLauncher`). It answers
  ``202`` with the run, ``running``.
- ``POST /v1/local/redteam/runs/{run_id}/resume``: go on with a stopped, cancelled or failed run with
  the run file and target it was started with, as ``martian redteam run FILE --resume RUN_ID`` does.
  It answers ``202`` with the run, ``running`` again; ``409`` for a run that is running or completed,
  or one the launcher did not start, whose run file it does not have.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from martian.evals.server.dependencies import EntitlementsDep, PrincipalDep
from martian.evals.server.projection import DEFAULT_PROJECT
from martian.redteam.server import ProjectQuery, RunStart, RunSummary, _summary, check_run_file

if TYPE_CHECKING:
    from martian.redteam.engine.launcher import LocalLauncher

PREFIX = "/v1/local/redteam/runs"


class LocalRunStart(RunStart):
    """A run file and, optionally, the local target it runs against."""

    target_id: str | None = Field(default=None, min_length=1, max_length=200)


def _local_launcher(request: Request) -> LocalLauncher:
    from martian.redteam.engine.launcher import LocalLauncher

    launcher = getattr(request.app.state, "martian_redteam_launcher", None)
    if not isinstance(launcher, LocalLauncher):
        raise HTTPException(501, "This installation cannot start red team runs against local targets")
    return launcher


router = APIRouter(prefix=PREFIX, tags=["local red team runs"])


@router.post("", response_model=RunSummary, status_code=202)
async def start_local_redteam_run(
    body: LocalRunStart, request: Request, principal: PrincipalDep, entitlements: EntitlementsDep
) -> RunSummary:
    check_run_file(body.run_file, entitlements)
    start = RunStart(project_id=body.project_id, run_file=body.run_file, request_id=body.request_id)
    run = await _local_launcher(request).start(principal, start, target_id=body.target_id)
    return _summary(run)


@router.post("/{run_id}/resume", response_model=RunSummary, status_code=202)
async def resume_local_redteam_run(
    run_id: str, request: Request, principal: PrincipalDep, project_id: ProjectQuery = DEFAULT_PROJECT
) -> RunSummary:
    run = await _local_launcher(request).resume(principal, project_id, run_id)
    return _summary(run)


__all__ = ["PREFIX", "LocalRunStart", "router"]
