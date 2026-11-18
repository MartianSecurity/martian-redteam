"""Red team runs pushed to this installation from another: the core's own route, outside the frozen
contract, beside the evals push routes (:mod:`martian.evals.server.push`).

``martian redteam push`` sends a finished red team run from the local store, or a
``redteam-run.json``, to a Martian server, where the shared Red Team pages list it with the runs
the server ran. It is the shared model's run, as the local store keeps it.

- ``PUT /v1/push/redteam/runs/{run_id}``: the run (``{"run": ...}``), into its project. The answer
  says whether this push created it.

The server labels what it received as executed by a client (``provenance.execution`` is
``client_executed``): the attacks, the target's replies and the verdicts came from the pusher's own
machine, not from this server. The push is idempotent: the same run pushed again changes nothing
(``created`` false); a run with the same id and other content is refused (``409``), as is a run
that has not finished (``422``). It writes as :func:`~martian.evals.server.push.push_principal`,
which an edition that signs machines in with keys binds for its push routes, and needs the ``run``
capability.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from martian.evals.server.dependencies import RecordsDep, allows
from martian.evals.server.push import PushPrincipalDep
from martian.redteam.model import RedteamRun
from martian.redteam.store import RedteamStore

router = APIRouter(prefix="/v1/push/redteam", tags=["redteam push"])

CLIENT_EXECUTED = "client_executed"
"""``provenance.execution`` of a pushed run: what the run holds was executed by the client."""
UNFINISHED = frozenset({"queued", "running"})


class RedteamPush(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: dict[str, Any]


class RedteamHeld(BaseModel):
    run_id: str
    project_id: str
    created: bool


def labelled(run: RedteamRun) -> RedteamRun:
    """``run`` as the server keeps a pushed run: labelled as executed by the client."""
    return run.model_copy(update={"provenance": {**run.provenance, "execution": CLIENT_EXECUTED}})


@router.put("/runs/{run_id}", response_model=RedteamHeld)
async def push_redteam_run(
    run_id: str, body: RedteamPush, principal: PushPrincipalDep, records: RecordsDep
) -> RedteamHeld:
    if not allows(principal, "run"):
        raise HTTPException(403, "Requires member role")
    try:
        run = RedteamRun.model_validate(body.run)
    except ValidationError as error:
        reasons = [
            ".".join(str(part) for part in item["loc"]) + ": " + str(item["msg"])
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        ]
        raise HTTPException(422, "; ".join(reasons[:10])) from None
    if run.id != run_id:
        raise HTTPException(422, "The run names another id than the path")
    if run.state in UNFINISHED:
        raise HTTPException(422, "Only a finished red team run is pushed")
    pushed = labelled(run)
    store = RedteamStore(records)
    stored = await store.get(run.project_id, run.id, principal=principal)
    if stored is not None:
        if stored.model_dump(mode="json") != pushed.model_dump(mode="json"):
            raise HTTPException(409, "The server holds this red team run with other content")
        return RedteamHeld(run_id=run.id, project_id=run.project_id, created=False)
    await store.save(pushed, principal=principal)
    return RedteamHeld(run_id=run.id, project_id=run.project_id, created=True)


__all__ = ["CLIENT_EXECUTED", "RedteamHeld", "RedteamPush", "labelled", "router"]
