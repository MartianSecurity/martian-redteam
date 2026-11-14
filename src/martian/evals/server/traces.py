"""Trace search and trace detail: a port of the trace routes of the server's
``app/api/evals_explorer.py`` (MorningHQ/MartianBackend) onto the store.

The store's ``TraceQuery`` filters on project, session, text, state and time and orders by time.
The contract's search also filters on user, minimum latency and a boolean assessment, and orders
by latency; the core applies those to what the store returns:

- With a user, latency or assessment filter, a page is filled from the store's pages in turn, at
  most 1,000 traces read per request; the cursor names the store's page and how many of its
  matches were already returned.
- Ordering by latency reads every trace the store's filters match, up to 1,000, and refuses more.

A cursor belongs to its query and project: another query gets ``409``.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Response

from martian.evals.model import Trace, digest
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
STORE_PAGE = 100
READ_LIMIT = 1000
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
        text=body.q or None,
        states=[TRACE_STATE_BACK[body.state]] if body.state else [],
        since=_from_ms(body.start_time_ms),
        # The contract's end is inclusive; the store's is exclusive.
        until=_from_ms(body.end_time_ms + 1) if body.end_time_ms is not None else None,
        oldest_first=body.order == "oldest",
        limit=STORE_PAGE,
    )


def _filter(body: TraceQuery) -> Callable[[Trace], bool] | None:
    tests: list[Callable[[Trace], bool]] = []
    if body.user_id:
        tests.append(lambda t: t.user_id == body.user_id)
    if body.min_latency_ms is not None:
        minimum = body.min_latency_ms
        tests.append(lambda t: t.latency_ms is not None and t.latency_ms >= minimum)
    if body.assessment:
        wanted = body.assessment_value == "true"
        tests.append(
            lambda t: any(
                s.scorer == body.assessment and isinstance(s.value, bool) and s.value is wanted for s in t.scores
            )
        )
    if not tests:
        return None
    return lambda trace: all(test(trace) for test in tests)


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


async def _by_latency(
    store: EvalStore, principal: Principal, body: TraceQuery, project: str, keep: Callable[[Trace], bool] | None
) -> tuple[list[Trace], str | None]:
    query, rows, cursor = _store_query(body, project), [], None
    while True:
        page = await store.search_traces(principal, query.model_copy(update={"cursor": cursor}))
        rows.extend(page.items)
        if len(rows) > READ_LIMIT:
            raise HTTPException(422, "Ordering by latency covers at most 1,000 traces; narrow the filters")
        if (cursor := page.next_cursor) is None:
            break
    rows = [t for t in rows if keep is None or keep(t)]
    known = [t for t in rows if t.latency_ms is not None]
    known.sort(key=lambda t: t.latency_ms or 0, reverse=body.order == "slowest")
    rows = known + [t for t in rows if t.latency_ms is None]
    offset = _position(body, project).get("offset", 0)
    if not isinstance(offset, int) or offset < 0:
        raise HTTPException(409, EXPIRED)
    end = offset + body.limit
    return rows[offset:end], _cursor(body, project, offset=end) if end < len(rows) else None


async def _in_order(
    store: EvalStore, principal: Principal, body: TraceQuery, project: str, keep: Callable[[Trace], bool] | None
) -> tuple[list[Trace], str | None]:
    position = _position(body, project)
    query = _store_query(body, project).model_copy(update={"limit": body.limit if keep is None else STORE_PAGE})
    cursor, skip = position.get("page"), position.get("skip", 0)
    if not (cursor is None or isinstance(cursor, str)) or not isinstance(skip, int) or skip < 0:
        raise HTTPException(409, EXPIRED)
    items: list[Trace] = []
    for _ in range(READ_LIMIT // STORE_PAGE):
        page = await store.search_traces(principal, query.model_copy(update={"cursor": cursor}))
        matches = [t for t in page.items if keep is None or keep(t)][skip:]
        wanted = body.limit - len(items)
        if len(matches) > wanted:
            return items + matches[:wanted], _cursor(body, project, page=cursor, skip=skip + wanted)
        items += matches
        skip = 0
        if page.next_cursor is None:
            return items, None
        cursor = page.next_cursor
        if len(items) == body.limit:
            break
    return items, _cursor(body, project, page=cursor, skip=0)


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
    keep = _filter(body)
    if body.order in ("slowest", "fastest"):
        traces, cursor = await _by_latency(store, principal, body, project, keep)
    else:
        traces, cursor = await _in_order(store, principal, body, project, keep)
    return {"items": [trace_row(t) for t in traces], "next_cursor": cursor, "limit": body.limit}


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
