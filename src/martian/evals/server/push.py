"""Runs pushed to this installation from another: the core's own routes, outside the frozen
contract.

``martian evals push`` (:mod:`martian.evals.push`) sends finished runs from the local edition's
data folder to a server, each with its case results, scores, scoring jobs and traces. A push
carries the lines of the data export's format (:mod:`martian.evals.data_export`), written by the
export's own code: a run's ``runs.jsonl`` line, its project's ``projects.jsonl`` line, and its
``case_results.jsonl``, ``scores.jsonl``, ``scoring_jobs.jsonl`` and ``traces.jsonl`` lines, under
the format's name and version. A server reads only its own version.

The routes are under ``/v1/push``, beside ``/v1/evals`` as OpenTelemetry's ``/v1/traces`` is: an
API for machines that send data in, outside the frozen contract, which holds every ``/v1/evals``
route the Martian server serves.

- ``GET /v1/push``: the organisation a push writes into and the format version read here.
- ``PUT /v1/push/runs/{run_id}``: the run, its project and its finished scoring jobs. The
  answer names each case the server holds a result of, with the scores it holds of it.
- ``POST /v1/push/runs/{run_id}/cases``: some of the run's cases, each with its result, its
  scores and every trace they refer to, or traces of the run alone.

Every write is idempotent, so a push sent again, whole or in part, adds nothing twice. A run, a
scoring job, a case result and a score keep their ids, which the record store keeps apart for each
organisation; one the server already holds with other content is refused (``409``). A trace is
recorded again in place, and a score of it is added only when the trace has none like it.

**Trace ids.** MLflow's trace ids are one namespace in a tracking store, whatever the workspace,
so the same trace pushed into two organisations would be one row. A pushed trace gets
:func:`pushed_trace_id`, ``tr-`` and 32 hex digits of the SHA-256 of the organisation's id and the
trace's own: in MLflow's form, so every binding keeps it as it is, the same each time the trace is
pushed into one organisation, and another in each other organisation. Every reference to a trace
follows it: a case result's, an attempt's, a turn's and a score's. The store's own identifiers
(experiment, MLflow run and assessment ids) are the receiving store's, as an import assigns them
(:class:`~martian.evals.data_export.References`).

A push writes as :func:`push_principal`, the edition's principal unless the edition binds another:
the Martian server binds its organisation API keys to it, for these routes only. It needs the
``run`` capability, and creating a project the server lacks needs ``manageProjects``.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from martian.evals.data_export import FORMAT, UNFINISHED, VERSION, References, score_key
from martian.evals.model import (
    Attempt,
    CaseResult,
    Identifier,
    Project,
    Run,
    Score,
    ScoringJob,
    StoreRef,
    Trace,
    TurnResult,
)
from martian.evals.records import RecordStore
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, StoreDep, allows
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT
from martian.evals.server.projects import PROJECT_LIMIT
from martian.evals.server.runs import lookup_run, writable_key
from martian.evals.store import EvalStore

router = APIRouter(prefix="/v1/push", tags=["evals push"])

MAX_JOBS = 200
"""Scoring jobs one ``PUT`` of a run carries."""
MAX_CASES = 500
"""Case results one batch carries."""
MAX_TRACES = 1000
"""Traces one batch carries."""
MAX_SCORES = 10000
"""Scores one batch carries."""
LINK_BATCH = 500


def push_principal(principal: PrincipalDep) -> Principal:
    """The principal a push writes as: the edition's. An edition that signs machines in with keys
    of their own binds this dependency instead, for the push routes alone."""
    return principal


PushPrincipalDep = Annotated[Principal, Depends(push_principal)]


def pushed_trace_id(principal: Principal, trace_id: str) -> str:
    """The id a trace pushed by ``principal`` gets in the receiving store: ``tr-`` and the first 32
    hex digits of the SHA-256 of the organisation's id (``local`` for the single local user) and
    the trace's own id."""
    space = principal.organisation_id or "local"
    return "tr-" + hashlib.sha256(f"martian-push\n{space}\n{trace_id}".encode()).hexdigest()[:32]


# --- the lines a push carries: the data export's -----------------------------------------------


class _Line(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: Identifier


class ProjectLine(_Line):
    """A line of ``projects.jsonl``."""

    project: Project


class RunLine(_Line):
    """A line of ``runs.jsonl``: the run as the record store holds it, and as the store does."""

    run: Run
    store_run: Run | None = None


class ScoringJobLine(_Line):
    """A line of ``scoring_jobs.jsonl``."""

    job: ScoringJob


class CaseResultLine(_Line):
    """A line of ``case_results.jsonl``."""

    result: CaseResult


class ScoreLine(_Line):
    """A line of ``scores.jsonl``: a score of a run's case in the record store."""

    score: Score


class TraceLine(_Line):
    """A line of ``traces.jsonl``: a trace with its spans and scores."""

    trace: Trace


class _Push(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str
    version: int

    @model_validator(mode="after")
    def readable(self) -> _Push:
        if (self.format, self.version) != (FORMAT, VERSION):
            raise ValueError(f"this server reads {FORMAT} version {VERSION}")
        return self


class RunPush(_Push):
    project: ProjectLine
    run: RunLine
    scoring_jobs: list[ScoringJobLine] = Field(default_factory=list, max_length=MAX_JOBS)


class CasesPush(_Push):
    run: RunLine
    case_results: list[CaseResultLine] = Field(default_factory=list, max_length=MAX_CASES)
    scores: list[ScoreLine] = Field(default_factory=list, max_length=MAX_SCORES)
    traces: list[TraceLine] = Field(default_factory=list, max_length=MAX_TRACES)


class PushTarget(BaseModel):
    format: str
    version: int
    organisation_id: str | None
    organisation_name: str | None


class RunHeld(BaseModel):
    run_id: str
    created: bool
    """Whether this request recorded the run; false when the server held it already."""
    cases: dict[str, list[tuple[str, str]]]
    """Each case the server holds a result of, with the scorer and scoring job (empty for the
    run's own) of each score it holds of the case."""


class CasesPushed(BaseModel):
    cases: int
    scores: int
    traces: int


# --- checks -------------------------------------------------------------------------------------


def _may_push(principal: Principal) -> None:
    if not allows(principal, "run"):
        raise HTTPException(403, "Requires member role")


def _checked(run_id: uuid.UUID, line: RunLine) -> RunLine:
    run, native = line.run, line.store_run
    if run.id != str(run_id) or run.project_id != line.project_id:
        raise HTTPException(422, "The run line names another run or project")
    if native is not None and (native.id != run.id or native.project_id != run.project_id):
        raise HTTPException(422, "The run line's store run is another run's")
    if run.state in UNFINISHED:
        raise HTTPException(422, "Only a finished run is pushed")
    return line


def _same(stored: Run | ScoringJob, pushed: Run | ScoringJob) -> bool:
    """Whether the server holds what is pushed: the same content, but for the store's references
    and a creation time the server stamped on a record that came without one."""
    if pushed.created_at is None:
        pushed = pushed.model_copy(update={"created_at": stored.created_at})
    return stored.model_dump(mode="json", exclude={"store"}) == pushed.model_dump(mode="json", exclude={"store"})


# --- what a push writes ---------------------------------------------------------------------------


async def _project(store: EvalStore, principal: Principal, line: ProjectLine, project_id: str) -> Project:
    """The run's project here: the default project is created on its first write, as for any
    write; another project the server lacks is created under the same id and name, by an admin and
    within the project limit. An archived project takes no more runs."""
    project = line.project
    if line.project_id != project_id or project.id != project_id:
        raise HTTPException(422, "The project line names another project than the run's")
    if project_id != DEFAULT_PROJECT and await store.get_project(principal, project_id) is None:
        if not allows(principal, "manageProjects"):
            raise HTTPException(403, "Requires admin role")
        if sum(p.id != DEFAULT_PROJECT for p in await store.list_projects(principal)) >= PROJECT_LIMIT:
            raise HTTPException(409, f"This organization has reached its {PROJECT_LIMIT}-project limit")
        await store.create_project(
            principal,
            Project(id=project.id, name=project.name, description=project.description, created_at=project.created_at),
        )
    await writable_key(store, principal, project_id)
    found = await store.get_project(principal, project_id)
    assert found is not None
    return found


async def _store_run(store: EvalStore, principal: Principal, line: RunLine) -> Run | None:
    """The run in the store here, started and finished as the pushed one was."""
    native = line.store_run
    if native is None:
        return None
    found = await store.get_run(principal, line.project_id, native.id)
    started = found is None
    if found is None:
        found = await store.start_run(principal, native.model_copy(update={"store": StoreRef()}))
    finished = native.state not in UNFINISHED and native.finished_at is not None
    if finished and (started or (found.state, found.counts) != (native.state, native.counts)):
        found = await store.finish_run(principal, line.project_id, native.id, state=native.state, counts=native.counts)
    return found


def _references(line: RunLine, project: Project, native: Run | None) -> References:
    """The receiving store's experiment and MLflow run for the pushed run's."""
    refs = References()
    refs.add("experiment_id", line.run.store.experiment_id, project.store.experiment_id)
    if line.store_run is not None and native is not None:
        refs.add("experiment_id", line.store_run.store.experiment_id, native.store.experiment_id)
        refs.add("run_id", line.store_run.store.run_id, native.store.run_id)
    return refs


class _Mapping:
    """Points a pushed object's references at the receiving store: its traces' ids (each
    :func:`pushed_trace_id`) and the store's own identifiers."""

    def __init__(self, principal: Principal, refs: References) -> None:
        self.principal, self.refs = principal, refs

    def trace(self, trace_id: str | None) -> str | None:
        return None if trace_id is None else pushed_trace_id(self.principal, trace_id)

    def store(self, ref: StoreRef) -> StoreRef:
        return self.refs.ref(ref).model_copy(update={"trace_id": self.trace(ref.trace_id)})

    def turns(self, turns: list[TurnResult]) -> list[TurnResult]:
        return [turn.model_copy(update={"trace_id": self.trace(turn.trace_id)}) for turn in turns]

    def attempt(self, attempt: Attempt) -> Attempt:
        return attempt.model_copy(update={"trace_id": self.trace(attempt.trace_id), "turns": self.turns(attempt.turns)})

    def result(self, result: CaseResult) -> CaseResult:
        return result.model_copy(
            update={
                "store": self.store(result.store),
                "attempts": [self.attempt(attempt) for attempt in result.attempts],
                "turns": self.turns(result.turns),
            }
        )

    def score(self, score: Score) -> Score:
        return score.model_copy(update={"trace_id": self.trace(score.trace_id), "store": self.store(score.store)})


async def _record_trace(store: EvalStore, principal: Principal, trace: Trace, mapping: _Mapping) -> None:
    """Record the trace under its pushed id, and each of its scores the recorded trace has none
    like; note the receiving store's assessment id of each."""
    target = mapping.trace(trace.id)
    assert target is not None
    saved = await store.record_trace(principal, trace.model_copy(update={"id": target, "scores": []}))
    held: dict[str, list[Score]] = {}
    for score in saved.scores:
        held.setdefault(score_key(score), []).append(score)
    for score in trace.scores:
        wanted = score.model_copy(update={"trace_id": target, "store": StoreRef()})
        if alike := held.get(score_key(wanted)):
            kept = alike.pop(0)
        else:
            kept = await store.log_score(principal, trace.project_id, target, wanted)
        mapping.refs.add("assessment_id", score.store.assessment_id, kept.store.assessment_id)


async def _held(records: RecordStore, principal: Principal, project_id: str, run_id: str) -> dict[str, Any]:
    """Each case of the run the record store holds a result of, with the scorer and scoring job of
    each score it holds of it."""
    held: dict[str, list[tuple[str, str]]] = {}
    offset = 0
    while True:
        page = await records.list_results(principal, project_id, run_id, offset=offset, limit=1000)
        held.update((result.case_id, []) for result in page.items)
        if page.next_cursor is None:
            break
        offset += 1000
    for score in await records.list_scores(principal, project_id, run_id):
        if score.case_id in held:
            held[score.case_id].append((score.scorer, score.scoring_job_id or ""))
    return held


# --- routes -------------------------------------------------------------------------------------


@router.get("", response_model=PushTarget)
async def push_target(principal: PushPrincipalDep) -> PushTarget:
    _may_push(principal)
    return PushTarget(
        format=FORMAT,
        version=VERSION,
        organisation_id=principal.organisation_id,
        organisation_name=principal.organisation_name,
    )


@router.put("/runs/{run_id}", response_model=RunHeld)
async def push_run(
    run_id: uuid.UUID, body: RunPush, principal: PushPrincipalDep, store: StoreDep, records: RecordsDep
) -> RunHeld:
    _may_push(principal)
    line = _checked(run_id, body.run)
    project_id, run = line.project_id, line.run
    for job_line in body.scoring_jobs:
        if job_line.project_id != project_id or job_line.job.run_id != run.id:
            raise HTTPException(422, "A scoring job line names another run or project")
        if job_line.job.state in UNFINISHED:
            raise HTTPException(422, "Only a finished scoring job is pushed")
    project = await _project(store, principal, body.project, project_id)
    # The run routes find a run by its id alone: one id is one run, in one project.
    stored = await lookup_run(store, records, principal, run.id)
    if stored is not None and stored.project_id != project_id:
        raise HTTPException(409, "The server holds a run with this id in another project")
    if stored is not None and not _same(stored, run):
        raise HTTPException(409, "The server holds this run with other content")
    held_jobs = await records.list_scoring_jobs(principal, project_id, run.id) if stored is not None else []
    jobs = {job.id: job for job in held_jobs}
    if any(
        job_line.job.id in jobs and not _same(jobs[job_line.job.id], job_line.job) for job_line in body.scoring_jobs
    ):
        raise HTTPException(409, "The server holds a scoring job of this run with other content")
    native = await _store_run(store, principal, line)
    if stored is None:
        refs = _references(line, project, native)
        await records.create_run(
            principal, run.model_copy(update={"store": _Mapping(principal, refs).store(run.store)})
        )
    for job_line in body.scoring_jobs:
        if job_line.job.id not in jobs:
            await records.create_scoring_job(principal, project_id, job_line.job)
    return RunHeld(run_id=run.id, created=stored is None, cases=await _held(records, principal, project_id, run.id))


@router.post("/runs/{run_id}/cases", response_model=CasesPushed)
async def push_cases(
    run_id: uuid.UUID, body: CasesPush, principal: PushPrincipalDep, store: StoreDep, records: RecordsDep
) -> CasesPushed:
    _may_push(principal)
    line = _checked(run_id, body.run)
    project_id, run = line.project_id, line.run
    project = await store.get_project(principal, project_id)
    stored = await records.get_run(principal, project_id, run.id) if project is not None else None
    if project is None or stored is None:
        raise HTTPException(409, "Push the run before its cases")
    if not _same(stored, run):
        raise HTTPException(409, "The server holds this run with other content")
    cases = set()
    for result_line in body.case_results:
        if result_line.project_id != project_id or result_line.result.run_id != run.id:
            raise HTTPException(422, "A case result line names another run or project")
        cases.add(result_line.result.case_id)
    for score_line in body.scores:
        score = score_line.score
        if score_line.project_id != project_id or score.run_id != run.id:
            raise HTTPException(422, "A score line names another run or project")
        if score.case_id not in cases:
            raise HTTPException(422, "A score line names a case whose result the batch does not carry")
    for trace_line in body.traces:
        trace = trace_line.trace
        if trace_line.project_id != project_id or trace.project_id != project_id:
            raise HTTPException(422, "A trace line names another project")
        if trace.run_id not in (None, run.id):
            raise HTTPException(422, "A trace line names another run")

    native = await store.get_run(principal, project_id, run.id) if line.store_run is not None else None
    mapping = _Mapping(principal, _references(line, project, native))
    linked: list[str] = []
    for trace_line in body.traces:
        await _record_trace(store, principal, trace_line.trace, mapping)
        if trace_line.trace.run_id is not None:
            linked.append(pushed_trace_id(principal, trace_line.trace.id))
    for start in range(0, len(linked), LINK_BATCH):
        await store.link_traces(principal, project_id, run.id, linked[start : start + LINK_BATCH])
    # Each case's result with its scores, in one transaction and after its traces: a case the
    # server holds a result of has its scores and its traces too.
    if body.case_results:
        await records.put_results(
            principal,
            project_id,
            [mapping.result(result_line.result) for result_line in body.case_results],
            scores=[mapping.score(score_line.score) for score_line in body.scores],
        )
    return CasesPushed(cases=len(body.case_results), scores=len(body.scores), traces=len(body.traces))


__all__ = [
    "CasesPush",
    "CasesPushed",
    "PushPrincipalDep",
    "PushTarget",
    "RunHeld",
    "RunPush",
    "push_principal",
    "pushed_trace_id",
    "router",
]
