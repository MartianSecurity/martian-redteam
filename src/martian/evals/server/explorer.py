"""The Overview and Quality issues pages: a port of ``POST /overview``, ``POST /quality-issues/search``
and ``POST /quality-issues/triage`` of the server's ``app/api/evals_explorer.py`` and
``app/evals_explorer.py`` (MartianSecurity/MartianBackend) onto the store.

Each operation is the one in version 3 of the frozen contract. The overview and the issue search
take the trace search's ``TraceQuery``, with every filter the trace search has, and the contract
leaves their responses untyped, so the answers are the server's, field for field. Each reads the
newest 1,000 traces the query matches, in the trace search's columns (``projection.trace_row``),
whatever the query's ``limit``, and says ``has_more`` when the query matches more. It starts from
the first trace, so a ``cursor`` is ``422``.

- The overview counts requests and errors, latency (median and 95th percentile), reported tokens
  and cost, a summary of each feedback score by name (a pass rate for booleans, a mean for numbers,
  counts for anything else), and a series of requests, errors and mean latency per time bucket:
  an hour, or whole hours so that the traces' span takes at most 168 buckets.
- An issue is a group of traces: ``execution_error``, the traces whose state is ``ERROR``, and
  ``failed:<name>``, the traces with a boolean ``false`` feedback score of that name that has no
  error. Each group lists its first 100 traces and how many of its traces are still ``open``: not
  triaged, or triaged ``open`` or ``investigating``.
- A triage decision is the ``martian.evals.triage`` tag of each trace it names: its state, the
  rationale, the person (as the bootstrap names them) and when. The trace search shows it as each
  row's ``triage``. It needs the ``review`` capability and a project that takes writes, and names
  only traces of the project: one the project lacks is ``404`` before any trace is changed.

The Martian server mounts this router behind its own plug points, as the local edition does.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from martian.evals.model import Trace
from martian.evals.server.dependencies import PrincipalDep, StoreDep, require_capability
from martian.evals.server.principal import Principal
from martian.evals.server.projection import trace_row, user_out
from martian.evals.server.runs import writable_project
from martian.evals.server.schemas import IssueTriageRequest, TraceQuery
from martian.evals.server.traces import project_scope, store_query
from martian.evals.store import EvalStore

router = APIRouter(prefix="/v1/evals", tags=["evaluation explorer"])

AGGREGATE_LIMIT = 1000
"""The newest traces an overview or an issue search reads."""
TRIAGE_TAG = "martian.evals.triage"
ISSUE_TRACES = 100
"""The traces an issue lists."""
HOUR_MS = 3_600_000
MAX_BUCKETS = 168
_PAGE = 500
METHOD = (
    "Groups persisted execution errors and explicit Boolean failed assessments. Numeric/categorical scores "
    "require their own evaluation policy. These are quality signals, not verified security findings."
)

Row = dict[str, Any]


async def aggregate_rows(store: EvalStore, principal: Principal, body: TraceQuery) -> tuple[list[Row], bool]:
    """The newest :data:`AGGREGATE_LIMIT` traces the query matches, as trace search rows, and
    whether it matches more. No trace before the default project exists."""
    if body.cursor:
        raise HTTPException(422, "Start a fresh search for an aggregate or export")
    project = await project_scope(store, principal, body.project_id)
    if project is None:
        return [], False
    query, cursor = store_query(body, project), None
    found: list[Trace] = []
    # One trace beyond the limit says whether the query matches more.
    while len(found) <= AGGREGATE_LIMIT:
        limit = min(_PAGE, AGGREGATE_LIMIT + 1 - len(found))
        page = await store.search_traces(principal, query.model_copy(update={"limit": limit, "cursor": cursor}))
        found += page.items
        if (cursor := page.next_cursor) is None:
            break
    return [trace_row(trace) for trace in found[:AGGREGATE_LIMIT]], len(found) > AGGREGATE_LIMIT


def _feedback(values: list[Any]) -> dict[str, Any]:
    if all(type(value) is bool for value in values):
        return {"kind": "boolean", "count": len(values), "pass_rate": sum(values) / len(values)}
    if all(type(value) in (int, float) and math.isfinite(value) for value in values):
        return {"kind": "numeric", "count": len(values), "mean": statistics.mean(values)}
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[str(value)] += 1
    return {"kind": "categorical", "count": len(values), "values": dict(counts)}


def summarize(rows: list[Row], *, has_more: bool) -> dict[str, Any]:
    """The overview of ``rows``: as the server's ``summarize``."""
    timestamps = [row["timestamp_ms"] for row in rows if row["timestamp_ms"] is not None]
    hours = math.ceil(((max(timestamps) - min(timestamps)) / HOUR_MS + 1) / MAX_BUCKETS) if timestamps else 1
    bucket_ms = HOUR_MS * max(1, hours)
    latencies = sorted(row["latency_ms"] for row in rows if row["latency_ms"] is not None)
    tokens = [row["tokens"] for row in rows if row["tokens"] is not None]
    costs = [row["cost"] for row in rows if row["cost"] is not None]
    feedback: dict[str, list[Any]] = defaultdict(list)
    buckets: dict[int, dict[str, Any]] = defaultdict(lambda: {"requests": 0, "errors": 0, "latency": []})
    for row in rows:
        for assessment in row["assessments"]:
            if assessment["name"] and not assessment.get("error") and assessment["value"] is not None:
                feedback[assessment["name"]].append(assessment["value"])
        if row["timestamp_ms"] is not None:
            bucket = buckets[row["timestamp_ms"] // bucket_ms * bucket_ms]
            bucket["requests"] += 1
            bucket["errors"] += row["state"] == "ERROR"
            if row["latency_ms"] is not None:
                bucket["latency"].append(row["latency_ms"])
    if buckets:
        # Every bucket between the first and the last, empty ones included.
        for key in range(min(buckets), max(buckets) + bucket_ms, bucket_ms):
            buckets[key]
    return {
        "requests": len(rows),
        "errors": sum(row["state"] == "ERROR" for row in rows),
        "latency": {
            "count": len(latencies),
            "p50": statistics.median(latencies) if latencies else None,
            "p95": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else None,
        },
        "tokens": {"reported": len(tokens), "total": sum(tokens) if tokens else None},
        "cost": {"reported": len(costs), "total": sum(costs) if costs else None},
        "feedback": {name: _feedback(values) for name, values in feedback.items()},
        "has_more": has_more,
        "limit": AGGREGATE_LIMIT,
        "bucket_ms": bucket_ms,
        "series": [
            {
                "timestamp_ms": key,
                "requests": value["requests"],
                "errors": value["errors"],
                "latency_ms": statistics.mean(value["latency"]) if value["latency"] else None,
            }
            for key, value in sorted(buckets.items())
        ],
    }


def quality_issues(rows: list[Row], *, has_more: bool) -> dict[str, Any]:
    """The quality issues among ``rows``: as the server's ``quality_issues``."""
    groups: dict[str, dict[str, Row]] = defaultdict(dict)
    for row in rows:
        if row["state"] == "ERROR":
            groups["execution_error"][row["id"]] = row
        for assessment in row["assessments"]:
            if assessment["value"] is False and not assessment.get("error"):
                groups["failed:" + str(assessment["name"])][row["id"]] = row
    return {
        "items": [
            {
                "id": name,
                "name": "Execution errors" if name == "execution_error" else "Failed " + name.split(":", 1)[1],
                "count": len(traces),
                "open": sum(
                    not row.get("triage") or row["triage"].get("state") in ("open", "investigating")
                    for row in traces.values()
                ),
                "traces": list(traces.values())[:ISSUE_TRACES],
            }
            for name, traces in groups.items()
        ],
        "has_more": has_more,
        "traces_examined": len(rows),
        "limit": AGGREGATE_LIMIT,
        "method": METHOD,
    }


@router.post("/overview", response_model=None)
async def overview(body: TraceQuery, principal: PrincipalDep, store: StoreDep) -> dict[str, Any]:
    rows, has_more = await aggregate_rows(store, principal, body)
    return summarize(rows, has_more=has_more)


@router.post("/quality-issues/search", response_model=None)
async def issue_search(body: TraceQuery, principal: PrincipalDep, store: StoreDep) -> dict[str, Any]:
    rows, has_more = await aggregate_rows(store, principal, body)
    return quality_issues(rows, has_more=has_more)


@router.post("/quality-issues/triage", response_model=None, dependencies=[Depends(require_capability("review"))])
async def triage(body: IssueTriageRequest, principal: PrincipalDep, store: StoreDep) -> dict[str, Any]:
    key = await writable_project(store, principal, body.project_id)
    for trace_id in body.trace_ids:
        if await store.get_trace(principal, key, trace_id) is None:
            raise HTTPException(404, "Trace not found")
    decision = {
        "state": body.state,
        "rationale": body.rationale.strip(),
        "actor_id": user_out(principal.user_id),
        "at": datetime.now(UTC).isoformat(),
    }
    value = json.dumps(decision, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    for trace_id in body.trace_ids:
        await store.set_trace_tag(principal, key, trace_id, TRIAGE_TAG, value)
    return {"persisted": len(body.trace_ids), "state": body.state}


__all__ = ["AGGREGATE_LIMIT", "TRIAGE_TAG", "aggregate_rows", "quality_issues", "router", "summarize"]
