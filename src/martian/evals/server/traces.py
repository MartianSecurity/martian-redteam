"""Trace search, sessions and trace detail: a port of the trace routes of the server's
``app/api/evals_explorer.py`` (MorningHQ/MartianBackend) onto the store.

Every filter and order of the contract's ``TraceQuery`` is the store's (``martian.evals.model.TraceQuery``):
content, state, session, user and time, minimum latency, a boolean assessment, and newest, oldest,
slowest or fastest first. So a page of results and a session search are the store's answer for the
whole project, however many traces it holds. A cursor names the store's page and belongs to its
query and project: another query gets ``409``.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Response

from martian.evals.model import SessionSearch, Trace, digest
from martian.evals.model import TraceQuery as StoreQuery
from martian.evals.server.dependencies import PrincipalDep, StoreDep
from martian.evals.server.principal import Principal
from martian.evals.server.projection import (
    DEFAULT_PROJECT,
    TRACE_STATE_BACK,
    project_key,
    trace_info,
    trace_row,
    trace_view,
)
from martian.evals.server.schemas import TraceQuery
from martian.evals.store import EvalStore

router = APIRouter(prefix="/v1/evals", tags=["evaluation explorer"])

TRACE_ID = r"^tr-[a-f0-9]{32}$"
EXPIRED = "Trace page expired or its scope changed; refresh the search"


async def project_scope(store: EvalStore, principal: Principal, project_id: uuid.UUID | None) -> str | None:
    """The store's project for a request, or ``None`` while the default project does not exist.
    Another project that does not exist is ``404``."""
    key = project_key(project_id)
    if await store.get_project(principal, key) is not None:
        return key
    if key == DEFAULT_PROJECT:
        return None
    raise HTTPException(404, "Not found")


def _from_ms(value: int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value / 1000, UTC)


def _store_query(body: TraceQuery, project: str) -> StoreQuery:
    return StoreQuery(
        project_id=project,
        session_id=body.session_id or None,
        user_id=body.user_id or None,
        text=body.q or None,
        states=[TRACE_STATE_BACK[body.state]] if body.state else [],
        since=_from_ms(body.start_time_ms),
        # The contract's end is inclusive; the store's is exclusive.
        until=_from_ms(body.end_time_ms + 1) if body.end_time_ms is not None else None,
        min_latency_ms=body.min_latency_ms,
        verdicts={body.assessment: body.assessment_value == "true"} if body.assessment else {},
        oldest_first=body.order == "oldest",
        slowest_first={"slowest": True, "fastest": False}.get(body.order),
        limit=body.limit,
    )


def _scope(body: TraceQuery, project: str) -> str:
    return digest({"project": project, "query": body.model_dump(mode="json", exclude={"cursor"})})[:32]


def _cursor(body: TraceQuery, project: str, **position: Any) -> str:
    text = json.dumps({"scope": _scope(body, project), **position}, separators=(",", ":"))
    return base64.urlsafe_b64encode(text.encode()).decode()


def _position(body: TraceQuery, project: str) -> dict[str, Any]:
    if not body.cursor:
        return {}
    try:
        found = json.loads(base64.urlsafe_b64decode(body.cursor.encode()))
    except (ValueError, binascii.Error):
        raise HTTPException(409, EXPIRED) from None
    if not isinstance(found, dict) or found.get("scope") != _scope(body, project):
        raise HTTPException(409, EXPIRED)
    return found


@router.post(
    "/traces/search",
    response_model=None,
    openapi_extra={
        "x-mlflow-shaped": "each item's `state` is MLflow's trace state and `assessments[].source` "
        "is MLflow's AssessmentSource"
    },
)
async def trace_search(body: TraceQuery, principal: PrincipalDep, store: StoreDep) -> dict[str, Any]:
    project = await project_scope(store, principal, body.project_id)
    if project is None:
        return {"items": [], "next_cursor": None, "limit": body.limit}
    cursor = _position(body, project).get("page")
    if not (cursor is None or isinstance(cursor, str)):
        raise HTTPException(409, EXPIRED)
    page = await store.search_traces(principal, _store_query(body, project).model_copy(update={"cursor": cursor}))
    after = _cursor(body, project, page=page.next_cursor) if page.next_cursor is not None else None
    return {"items": [trace_row(t) for t in page.items], "next_cursor": after, "limit": body.limit}


@router.post("/sessions/search", response_model=None)
async def session_search(body: TraceQuery, principal: PrincipalDep, store: StoreDep) -> dict[str, Any]:
    if body.cursor:
        raise HTTPException(422, "Start a fresh search for an aggregate or export")
    project = await project_scope(store, principal, body.project_id)
    if project is None:
        found = SessionSearch(items=[], traces_examined=0, ungrouped=0)
    else:
        found = await store.search_sessions(principal, _store_query(body, project))
    return found.model_dump(mode="json")


async def _trace(
    store: EvalStore, principal: Principal, trace_id: str, project_id: uuid.UUID | None
) -> tuple[Trace, dict[str, Any]]:
    """The trace and its ``TraceInfo``; ``404`` when the project has no such trace."""
    project = await project_scope(store, principal, project_id)
    found = await store.get_trace(principal, project, trace_id) if project else None
    if found is None:
        raise HTTPException(404, "Trace not found")
    stored = await store.get_project(principal, found.project_id)
    experiment = (stored.store.experiment_id if stored else None) or found.project_id
    return found, trace_info(found, experiment)


@router.get(
    "/traces/{trace_id}",
    response_model=None,
    openapi_extra={"x-mlflow-shaped": "`info` is MLflow's TraceInfo, unchanged"},
)
async def trace_detail(
    trace_id: Annotated[str, Path(pattern=TRACE_ID)],
    principal: PrincipalDep,
    store: StoreDep,
    project_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    found, info = await _trace(store, principal, trace_id, project_id)
    return {**trace_view(found, trace_id), "info": info}


@router.get(
    "/traces/{trace_id}/payload",
    response_model=None,
    openapi_extra={"x-mlflow-shaped": "the whole body is MLflow's Trace JSON (a download)"},
)
async def trace_payload(
    trace_id: Annotated[str, Path(pattern=TRACE_ID)],
    principal: PrincipalDep,
    store: StoreDep,
    project_id: uuid.UUID | None = None,
) -> Response:
    # The trace's TraceInfo and its spans in the shared model's form.
    found, info = await _trace(store, principal, trace_id, project_id)
    payload = {"trace_info": info, "spans": [span.model_dump(mode="json") for span in found.spans]}
    return Response(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False),
        media_type="application/json",
        headers={"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="eval-trace.json"'},
    )
