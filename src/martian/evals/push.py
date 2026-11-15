"""Push finished runs from the local edition's data folder to a Martian server.

``martian evals push`` sends each finished run of the data folder, or the runs it names, to the
server ``MARTIAN_API_URL`` names, signed in with the organisation API key in ``MARTIAN_API_TOKEN``,
as ``martian ci`` is. The server's routes are the core's own (:mod:`martian.evals.server.push`).
What it sends are the data export's lines, written by the export's own code
(:func:`~martian.evals.data_export.run_records`, :func:`~martian.evals.data_export.export_line`),
so a push and an export cannot describe a run differently:

1. ``GET /v1/push``: the key is good, and the server reads this SDK's export format version.
2. For each run, oldest first: ``PUT /v1/push/runs/{id}`` with the run, its project and its
   finished scoring jobs. The server answers which cases it holds, with which scores.
3. ``POST /v1/push/runs/{id}/cases`` with each case the server does not hold whole, a batch
   at a time: the case's result, its scores and every trace they refer to; then the run's traces
   that no case refers to.

A push that stops, however it stops, is continued by pushing again: the server holds a case with
its scores and traces or not at all, so the next push sends only the cases it lacks, and every
write is idempotent, so what is sent twice is there once.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from martian.evals.client import BackendError
from martian.evals.data_export import FORMAT, UNFINISHED, VERSION, export_line, run_records
from martian.evals.model import Project, Run, TraceQuery

if TYPE_CHECKING:
    from martian.evals.records import RecordStore
    from martian.evals.server.principal import Principal
    from martian.evals.store import EvalStore

CASE_BATCH = 50
"""Cases one request carries at most."""
TRACE_BATCH = 500
"""Traces one request carries at most, unless one case refers to more."""
BATCH_BYTES = 4_000_000
"""About how large one request's lines are at most, unless one case's are larger."""
TRACE_PAGE = 500

REFUSED = {
    403: "the key may not push it: pushing needs a member's key (evals:write), and a project the server lacks "
    "needs an admin to create it",
    404: "the server could not find what the run refers to",
    409: "the server holds this run or one of its scoring jobs with other content (it changed here after it was "
    "pushed), or a run with its id in another project, or the run's project there is archived or at its project "
    "limit",
    413: "one of its cases is larger than the server takes in one request",
    422: "the server could not read it: check that the SDK and the server are of the same release",
}
"""What a server's refusal of one run means. The push goes on with the next run."""

EPOCH = datetime.min.replace(tzinfo=UTC)


class PushError(Exception):
    """A push that cannot go on. The message says why."""


class Server(Protocol):
    """The server's push routes. :class:`martian.evals.client.Backend` is one."""

    def push_target(self) -> dict[str, Any]: ...

    def push_run(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def push_cases(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PushedRun:
    """A run the server holds whole after the push. ``created``: it did not hold the run before.
    ``sent``: how many of its ``cases`` this push sent, the ones the server lacked or held without
    some of their scores; ``traces``: how many traces it sent."""

    run_id: str
    code: str
    created: bool
    cases: int
    sent: int
    traces: int


@dataclass(frozen=True)
class RefusedRun:
    """A run the server refused, with the status it answered and what that means."""

    run_id: str
    code: str
    status: int
    reason: str


@dataclass(frozen=True)
class PushSummary:
    """A finished push: the organisation it pushed into, the runs it pushed, the runs it left
    because they have not finished, and the runs the server refused."""

    organisation_id: str | None
    organisation_name: str | None
    pushed: tuple[PushedRun, ...]
    unfinished: tuple[str, ...]
    refused: tuple[RefusedRun, ...]


class _Batch:
    """The cases and traces of one request."""

    def __init__(self, run_line: dict[str, Any]) -> None:
        self.run_line = run_line
        self.results: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.traces: list[dict[str, Any]] = []
        self.size = 0

    def fits(self, results: list[dict[str, Any]], traces: list[dict[str, Any]], size: int) -> bool:
        if not self.results and not self.traces:
            return True
        return (
            len(self.results) + len(results) <= CASE_BATCH
            and len(self.traces) + len(traces) <= TRACE_BATCH
            and self.size + size <= BATCH_BYTES
        )

    def add(
        self, results: list[dict[str, Any]], scores: list[dict[str, Any]], traces: list[dict[str, Any]], size: int
    ) -> None:
        self.results += results
        self.scores += scores
        self.traces += traces
        self.size += size

    def body(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "run": self.run_line,
            "case_results": self.results,
            "scores": self.scores,
            "traces": self.traces,
        }


def _size(lines: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(line, ensure_ascii=False).encode("utf-8")) for line in lines)


def _score_held(score: dict[str, Any]) -> tuple[str, str, str]:
    """How the server's ``PUT`` names a score it holds: its scorer, scoring job and source."""
    return (score["scorer"], score.get("scoring_job_id") or "", score["source"])


def _trace_ids(result: dict[str, Any], scores: list[dict[str, Any]]) -> set[str]:
    """Every trace a case result and its scores refer to."""
    found = {(result.get("store") or {}).get("trace_id")}
    for attempt in result.get("attempts") or []:
        found.add(attempt.get("trace_id"))
        found |= {turn.get("trace_id") for turn in attempt.get("turns") or []}
    found |= {turn.get("trace_id") for turn in result.get("turns") or []}
    for line in scores:
        found |= {line["score"].get("trace_id"), (line["score"].get("store") or {}).get("trace_id")}
    return {trace_id for trace_id in found if isinstance(trace_id, str) and trace_id}


async def _runs(
    store: EvalStore, records: RecordStore, principal: Principal, wanted: Sequence[str]
) -> list[tuple[Project, Run]]:
    """The runs to push, oldest first: every run, or those ``wanted`` names by id or code."""
    found: dict[str, tuple[Project, Run]] = {}
    for project in await store.list_projects(principal):
        cursor = None
        while True:
            page = await records.list_runs(principal, project.id, limit=500, cursor=cursor)
            found.update((run.id, (project, run)) for run in page.items)
            if (cursor := page.next_cursor) is None:
                break
    ordered = sorted(found.values(), key=lambda pair: (pair[1].created_at or EPOCH, pair[1].id))
    if not wanted:
        return ordered
    chosen: dict[str, tuple[Project, Run]] = {}
    for name in wanted:
        matches = [(project, run) for project, run in ordered if name in (run.id, run.code)]
        if not matches:
            raise PushError(f"the data folder has no run with the id or code {name}")
        chosen.update((run.id, (project, run)) for project, run in matches)
    return sorted(chosen.values(), key=lambda pair: (pair[1].created_at or EPOCH, pair[1].id))


async def _run_traces(store: EvalStore, principal: Principal, project_id: str, run_id: str) -> list[str]:
    """The ids of the run's traces, oldest first."""
    found: dict[str, None] = {}
    cursor = None
    while True:
        query = TraceQuery(project_id=project_id, run_id=run_id, oldest_first=True, limit=TRACE_PAGE, cursor=cursor)
        page = await store.search_traces(principal, query)
        found.update(dict.fromkeys(trace.id for trace in page.items))
        if (cursor := page.next_cursor) is None:
            return list(found)


async def _push_run(
    store: EvalStore, records: RecordStore, principal: Principal, server: Server, project: Project, run: Run
) -> PushedRun:
    lines: dict[str, list[dict[str, Any]]] = {}
    await run_records(
        store, records, principal, lambda name, line: lines.setdefault(name, []).append(line), project.id, run.id
    )
    [run_line] = lines["runs.jsonl"]
    # A scoring job still queued or running is left here with its scores; a later push sends them.
    jobs = [line for line in lines.get("scoring_jobs.jsonl", []) if line["job"]["state"] not in UNFINISHED]
    finished = {line["job"]["id"] for line in jobs}
    scores: dict[str, list[dict[str, Any]]] = {}
    for line in lines.get("scores.jsonl", []):
        job = line["score"].get("scoring_job_id")
        if job is None or job in finished:
            scores.setdefault(line["score"]["case_id"], []).append(line)
    held = server.push_run(
        run.id,
        {
            "format": FORMAT,
            "version": VERSION,
            "project": export_line(project.id, project=project),
            "run": run_line,
            "scoring_jobs": jobs,
        },
    )
    on_server = {case: {tuple(key) for key in keys} for case, keys in (held.get("cases") or {}).items()}

    batch, sent, traced = _Batch(run_line), 0, 0
    referenced: set[str] = set()

    async def traces_of(trace_ids: list[str]) -> list[dict[str, Any]]:
        found = []
        for trace_id in trace_ids:
            trace = await store.get_trace(principal, project.id, trace_id)
            # A trace another run recorded is pushed with that run.
            if trace is not None and trace.run_id in (None, run.id):
                found.append(export_line(project.id, trace=trace))
        return found

    def add(results: list[dict[str, Any]], case_scores: list[dict[str, Any]], traces: list[dict[str, Any]]) -> None:
        nonlocal batch
        size = _size(results) + _size(case_scores) + _size(traces)
        if not batch.fits(results, traces, size):
            server.push_cases(run.id, batch.body())
            batch = _Batch(run_line)
        batch.add(results, case_scores, traces, size)

    for result_line in lines.get("case_results.jsonl", []):
        result = result_line["result"]
        case_scores = scores.get(result["case_id"], [])
        trace_ids = _trace_ids(result, case_scores)
        referenced |= trace_ids
        keys = {_score_held(line["score"]) for line in case_scores}
        if result["case_id"] in on_server and keys <= on_server[result["case_id"]]:
            continue
        traces = await traces_of(sorted(trace_ids))
        add([result_line], case_scores, traces)
        sent += 1
        traced += len(traces)
    # The run's traces no case refers to, recorded again each time: the server keeps one of each.
    for trace_id in await _run_traces(store, principal, project.id, run.id):
        if trace_id not in referenced and (loose := await traces_of([trace_id])):
            add([], [], loose)
            traced += 1
    if batch.results or batch.traces:
        server.push_cases(run.id, batch.body())
    return PushedRun(
        run_id=run.id,
        code=run.code or run.id,
        created=bool(held.get("created")),
        cases=len(lines.get("case_results.jsonl", [])),
        sent=sent,
        traces=traced,
    )


async def push_data(
    store: EvalStore, records: RecordStore, principal: Principal, server: Server, runs: Sequence[str] = ()
) -> PushSummary:
    """Push every finished run the store and the record store hold for ``principal``, or the runs
    ``runs`` names by id or code, to ``server``. A run still queued or running is left for a later
    push; a run the server refuses is named in the summary, and the push goes on with the next.
    Any other failure stops the push with :class:`~martian.evals.client.BackendError`; pushing
    again continues it."""
    target = server.push_target()
    if (target.get("format"), target.get("version")) != (FORMAT, VERSION):
        raise PushError(
            f"the server reads {target.get('format')} version {target.get('version')}, and this SDK writes "
            f"version {VERSION}: use an SDK of the server's release"
        )
    pushed: list[PushedRun] = []
    unfinished: list[str] = []
    refused: list[RefusedRun] = []
    for project, run in await _runs(store, records, principal, runs):
        code = run.code or run.id
        if run.state in UNFINISHED:
            unfinished.append(code)
            continue
        if str(_uuid(run.id)) != run.id:
            refused.append(RefusedRun(run.id, code, 0, "its id is not a UUID, which the server's routes take"))
            continue
        try:
            pushed.append(await _push_run(store, records, principal, server, project, run))
        except BackendError as error:
            if error.status not in REFUSED:
                raise
            refused.append(RefusedRun(run.id, code, error.status, REFUSED[error.status]))
    return PushSummary(
        organisation_id=target.get("organisation_id"),
        organisation_name=target.get("organisation_name"),
        pushed=tuple(pushed),
        unfinished=tuple(unfinished),
        refused=tuple(refused),
    )


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def push_local(home: Path, server: Server, runs: Sequence[str] = ()) -> PushSummary:
    """Push the finished runs of the local edition's data folder ``home``, or the ones ``runs``
    names, to ``server``, as the single local user."""
    from martian.evals.data_export import _local_stores
    from martian.evals.server.principal import SINGLE_USER

    if not home.is_dir():
        raise PushError(f"there is no data folder at {home}")
    store, records = _local_stores(home)
    try:
        return asyncio.run(push_data(store, records, SINGLE_USER, server, runs))
    finally:
        records.close()


__all__ = [
    "BATCH_BYTES",
    "CASE_BATCH",
    "REFUSED",
    "TRACE_BATCH",
    "PushError",
    "PushSummary",
    "PushedRun",
    "RefusedRun",
    "Server",
    "push_data",
    "push_local",
]
