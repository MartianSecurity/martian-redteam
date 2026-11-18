"""Saved trace views: a port of the ``/views`` routes of the server's ``app/api/evals_explorer.py``
(MartianSecurity/MartianBackend) onto the record store.

A view belongs to a project (the default one when ``project_id`` is null) and to the person who
saved it. A private view is seen only by its owner; an organisation view by everyone. Only the
owner, or a role that may manage views, changes or deletes one, naming the revision it last read.
A view's id is derived from the ``request_id`` that created it; a person keeps at most 100 views.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from martian.evals.records import RecordStore, SavedView
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, StoreDep, allows, require_capability
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id, project_key, user_out
from martian.evals.server.runs import project_keys
from martian.evals.server.write_schemas import SavedViewRequest
from martian.evals.store import EvalStore

router = APIRouter(prefix="/v1/evals", tags=["evaluation explorer"])

VIEW_LIMIT = 100
DEFINITION = {"project_id", "query", "columns", "density"}


def view_out(view: SavedView) -> dict[str, Any]:
    return {
        "id": view.id,
        "name": view.name,
        "owner_id": user_out(view.owner),
        "visibility": view.visibility,
        "revision": view.revision,
        **view.definition,
    }


def visible(view: SavedView, principal: Principal) -> bool:
    return view.owner == principal.user_id or view.visibility == "organization"


async def _project(store: EvalStore, principal: Principal, project_id: uuid.UUID | None) -> str:
    key = project_key(project_id)
    if key != DEFAULT_PROJECT and await store.get_project(principal, key) is None:
        raise HTTPException(404, "Not found")
    return key


async def _find(store: EvalStore, records: RecordStore, principal: Principal, view_id: uuid.UUID) -> SavedView:
    for key in await project_keys(store, principal):
        if (view := await records.get_view(principal, key, str(view_id))) and visible(view, principal):
            return view
    raise HTTPException(404, "View not found")


def _mine_or_managed(view: SavedView, principal: Principal, action: str) -> None:
    if view.owner != principal.user_id and not allows(principal, "manageViews"):
        raise HTTPException(403, f"Only the view owner or an administrator can {action} this view")


@router.get("/views", response_model=None)
async def saved_views(
    principal: PrincipalDep, store: StoreDep, records: RecordsDep, project_id: uuid.UUID | None = None
) -> dict[str, Any]:
    key = await _project(store, principal, project_id)
    return {"items": [view_out(v) for v in await records.list_views(principal, key) if visible(v, principal)]}


@router.post("/views", status_code=201, response_model=None, dependencies=[Depends(require_capability("saveViews"))])
async def create_view(
    body: SavedViewRequest, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    key = await _project(store, principal, body.project_id)
    view_id = str(derived_id("view", str(body.request_id)))
    definition = body.model_dump(mode="json", include=DEFINITION)
    if existing := await records.get_view(principal, key, view_id):
        if (existing.owner, existing.name, existing.visibility, existing.definition) != (
            principal.user_id,
            body.name,
            body.visibility,
            definition,
        ):
            raise HTTPException(409, "request_id belongs to another view")
        return view_out(existing)
    owned = 0
    for project in await project_keys(store, principal):
        owned += sum(v.owner == principal.user_id for v in await records.list_views(principal, project))
    if owned >= VIEW_LIMIT:
        raise HTTPException(409, f"Delete an unused view before saving another ({VIEW_LIMIT}-view limit)")
    view = SavedView(
        id=view_id,
        project_id=key,
        owner=principal.user_id,
        name=body.name,
        visibility=body.visibility,
        definition=definition,
    )
    return view_out(await records.save_view(principal, key, view))


@router.put("/views/{view_id}", response_model=None, dependencies=[Depends(require_capability("saveViews"))])
async def update_view(
    view_id: uuid.UUID, body: SavedViewRequest, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    view = await _find(store, records, principal, view_id)
    _mine_or_managed(view, principal, "change")
    if view.revision != body.revision or view.project_id != project_key(body.project_id):
        raise HTTPException(409, "View changed; reload before saving")
    changed = view.model_copy(
        update={
            "name": body.name,
            "visibility": body.visibility,
            "definition": body.model_dump(mode="json", include=DEFINITION),
        }
    )
    return view_out(await records.save_view(principal, view.project_id, changed, expected_revision=view.revision))


@router.delete("/views/{view_id}", status_code=204, dependencies=[Depends(require_capability("saveViews"))])
async def delete_view(
    view_id: uuid.UUID, revision: int, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> Response:
    view = await _find(store, records, principal, view_id)
    _mine_or_managed(view, principal, "delete")
    if view.revision != revision:
        raise HTTPException(409, "View changed; reload before deleting")
    await records.delete_view(principal, view.project_id, view.id, expected_revision=revision)
    return Response(status_code=204)


__all__ = ["router", "view_out"]
