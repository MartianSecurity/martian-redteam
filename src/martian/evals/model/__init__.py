"""The shared eval data model: one set of objects for the SDK, the local edition and the server.

A suite file, a local ``results.jsonl`` and the server's evals tables all describe these objects;
``docs/eval-model.md`` maps every field of each onto them. Tenancy, request receipts and storage
mechanics are not part of the model: each edition adds its own around it. MLflow stays the store
underneath, so an object that lives there carries its MLflow identifiers in ``store``.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator

MODEL_VERSION = "martian-eval-model-v1"

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Value = bool | int | float | str | None

TargetKind = Literal["function", "http", "agent", "stored"]
ScorerKind = Literal["llm_judge", "check", "code", "human"]
DatasetSource = Literal["file", "draft", "global", "upload", "traces"]
RunSource = Literal["local", "console", "ci"]
RunState = Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
CaseStatus = Literal["pending", "passed", "failed", "error", "skipped"]
ScoreSource = Literal["code", "llm_judge", "human"]
QuestionKind = Literal["boolean", "numeric", "categorical", "text"]


def digest(value: Any) -> str:
    """SHA-256 of a value's canonical JSON: keys sorted, no whitespace, UTF-8."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Object(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StoreRef(_Object):
    """Where an object lives in the MLflow store. Empty for an object never written there."""

    experiment_id: str | None = None
    run_id: str | None = None
    dataset_id: str | None = None
    trace_id: str | None = None
    assessment_id: str | None = None


class Project(_Object):
    """A named body of evaluation work. In MLflow, one experiment."""

    id: Identifier
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    created_at: datetime | None = None
    archived_at: datetime | None = None
    store: StoreRef = StoreRef()


class Case(_Object):
    """One test case: what the target receives, what to check it against, and optionally a stored
    output to grade instead of calling a target."""

    id: Identifier
    inputs: dict[str, Any] = Field(min_length=1)
    expectations: dict[str, Any] = Field(default_factory=dict)
    outputs: Any = None
    tags: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetVersion(_Object):
    """An immutable set of cases. ``digest`` identifies the content whatever it is called."""

    id: Identifier
    name: str = Field(min_length=1, max_length=200)
    version: int | None = Field(default=None, ge=1)
    digest: Digest
    case_count: int = Field(ge=0)
    source: DatasetSource
    location: str = ""
    created_at: datetime | None = None
    created_by: str | None = None
    store: StoreRef = StoreRef()


class DatasetDraft(_Object):
    """An editable working copy of a dataset. Publishing it creates a ``DatasetVersion``; every edit
    raises ``revision``, so two people cannot overwrite each other's changes unnoticed."""

    id: Identifier
    project_id: Identifier
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    revision: int = Field(default=1, ge=1)
    state: Literal["editing", "published"] = "editing"
    source_dataset_id: str | None = None
    case_count: int = Field(default=0, ge=0)
    published_digest: Digest | None = None
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Target(_Object):
    """The thing under test. An HTTP target's ``url`` and ``headers`` are kept as the suite file
    writes them, with ``${NAME}`` references unexpanded: the values a run reads from the
    environment are never part of the model."""

    kind: TargetKind
    file: str | None = None
    function: str | None = None
    url: str | None = None
    body: dict[str, Any] | None = None
    response_path: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    agent_id: str | None = None

    @model_validator(mode="after")
    def fields_for_kind(self) -> Target:
        given = {
            name
            for name in ("file", "function", "url", "body", "response_path", "headers", "agent_id")
            if getattr(self, name)
        }
        allowed = {
            "function": {"file", "function"},
            "http": {"url", "body", "response_path", "headers"},
            "agent": {"agent_id"},
            "stored": set(),
        }[self.kind]
        required = {"function": {"file", "function"}, "http": {"url", "body"}, "agent": {"agent_id"}, "stored": set()}
        if missing := sorted(required[self.kind] - given):
            raise ValueError(f"a {self.kind} target needs {', '.join(missing)}")
        if extra := sorted(given - allowed):
            raise ValueError(f"{', '.join(extra)} does not apply to a {self.kind} target")
        return self


class ScorerVersion(_Object):
    """One way of scoring a case, pinned to the version that produced the score.

    A judge's ``model`` is an MLflow model URI (``openai:/<model>``); ``endpoint`` is the base
    address of the OpenAI-compatible endpoint that serves it, or None for the provider's own. Keys
    are never part of the model. An unset ``endpoint`` is left out when the scorer is written, so
    a scorer without one has the digest it had before the field existed.
    """

    name: Identifier
    kind: ScorerKind
    type: str = Field(min_length=1, max_length=80)
    version: str = ""
    model: str | None = None
    endpoint: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    criteria: dict[str, Any] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _without_unset_endpoint(self, handler: SerializerFunctionWrapHandler) -> Any:
        written = handler(self)
        if isinstance(written, dict) and written.get("endpoint", "") is None:
            del written["endpoint"]
        return written


class Policy(_Object):
    """When a scorer's results pass the suite."""

    scorer: Identifier
    direction: Literal["higher", "lower"] = "higher"
    threshold: float | None = Field(default=None, allow_inf_nan=False)
    minimum_coverage: float = Field(default=1, ge=0, le=1)


class RunSettings(_Object):
    """How a run of the suite is carried out, not what it grades. ``None`` leaves the runner's
    default. A case result's fingerprint leaves these out, so changing one keeps earlier results."""

    concurrency: int | None = Field(default=None, ge=1)
    batch: int | None = Field(default=None, ge=1)
    target_timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    judge_max_retries: int | None = Field(default=None, ge=0)
    judge_rate_limit_wait_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Headers added to every judge request, values as written with ${NAME} unexpanded.
    judge_headers: dict[str, str] | None = None
    output: str | None = None
    mlflow_tracking_uri: str | None = None
    mlflow_experiment: str | None = None


class Suite(_Object):
    """A dataset, a target and scorers, run together. A new version for every change.

    ``dataset_location`` is where the cases are read from: a suite file's ``data`` path, as written
    there. ``input_key`` names the input that judges grade against and that a plain-text case
    input fills.
    """

    id: Identifier
    name: str = Field(min_length=1, max_length=120)
    version: int = Field(default=1, ge=1)
    dataset_name: str | None = None
    dataset_digest: Digest | None = None
    dataset_location: str | None = None
    input_key: str = Field(default="message", min_length=1)
    target: Target | None = None
    scorers: list[ScorerVersion] = Field(min_length=1)
    policies: list[Policy] = Field(default_factory=list)
    settings: RunSettings = RunSettings()
    digest: Digest
    created_at: datetime | None = None
    created_by: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> Suite:
        names = [scorer.name for scorer in self.scorers]
        if len(set(names)) != len(names):
            raise ValueError("scorer names must be unique within a suite")
        if unknown := sorted({policy.scorer for policy in self.policies} - set(names)):
            raise ValueError(f"policies name scorers the suite does not have: {', '.join(unknown)}")
        return self


def suite_digest(suite: Suite) -> str:
    """The digest of a suite's definition: every field but ``id``, ``version``, ``digest`` and who
    saved the version when. Run settings count, so a store that keeps one version per digest never
    drops a change to them."""
    return digest(suite.model_dump(mode="json", exclude={"id", "version", "digest", "created_at", "created_by"}))


class Counts(_Object):
    cases: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)


class Run(_Object):
    """One execution of a suite, or of a dataset, target and scorers, over its cases."""

    id: Identifier
    project_id: Identifier
    code: str = ""
    source: RunSource
    state: RunState
    suite_id: str | None = None
    suite_version: int | None = Field(default=None, ge=1)
    suite_digest: Digest | None = None
    dataset_name: str = ""
    dataset_digest: Digest | None = None
    target: Target | None = None
    scorers: list[ScorerVersion] = Field(default_factory=list)
    counts: Counts = Counts()
    max_cases: int | None = Field(default=None, ge=1)
    truncated: bool = False
    error: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)
    requested_by: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    """When someone asked to stop the run. Whoever executes it stops before its next case."""
    store: StoreRef = StoreRef()


class CaseResult(_Object):
    """What one case produced in one run. ``fingerprint`` identifies everything that produced it,
    so a resumed run reuses the result only while that is unchanged."""

    run_id: Identifier
    case_id: Identifier
    position: int = Field(default=0, ge=0)
    status: CaseStatus
    inputs: dict[str, Any] = Field(default_factory=dict)
    expectations: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    error: str = ""
    latency_ms: float | None = Field(default=None, ge=0)
    fingerprint: Digest | None = None
    finished_at: datetime | None = None
    store: StoreRef = StoreRef()


class Score(_Object):
    """One scorer's verdict on a case result, or on a trace of live traffic (no run or case).
    ``passed`` is None when there is no verdict."""

    run_id: str | None = None
    case_id: str | None = None
    trace_id: str | None = None
    scorer: Identifier
    scorer_version: str = ""
    source: ScoreSource
    source_id: str = ""
    value: Value = None
    passed: bool | None = None
    rationale: str = ""
    error: str = ""
    scoring_job_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    store: StoreRef = StoreRef()

    @model_validator(mode="after")
    def finite(self) -> Score:
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("a score value must be finite")
        return self


class ScoringJob(_Object):
    """Scoring a finished run again with more scorers. Its scores add to the run's, never replace them.
    A job that applies a saved suite names its version and carries the suite's policies."""

    id: Identifier
    run_id: Identifier
    scorers: list[ScorerVersion] = Field(min_length=1)
    policies: list[Policy] = Field(default_factory=list)
    suite_id: str | None = None
    suite_version: int | None = Field(default=None, ge=1)
    state: RunState
    total: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    error: str = ""
    requested_by: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    """When someone asked to stop the job. Whoever executes it stops before its next case."""


class ReviewQuestion(_Object):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,59}$")
    kind: QuestionKind = "boolean"
    instruction: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=20)
    minimum: float = Field(default=0, allow_inf_nan=False)
    maximum: float = Field(default=1, allow_inf_nan=False)


class ReviewQueue(_Object):
    """Case results of one run handed to people to answer questions about."""

    id: Identifier
    run_id: Identifier
    name: str = Field(min_length=1, max_length=120)
    questions: list[ReviewQuestion] = Field(min_length=1)
    case_ids: list[Identifier] = Field(min_length=1)
    assignee: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    store: StoreRef = StoreRef()


class Review(_Object):
    """A person's answer about one case result. A new answer is a new review that names the one it
    replaces in ``previous_id``; reviews are never edited."""

    id: Identifier
    run_id: Identifier
    case_id: Identifier
    criterion: str = Field(default="human_review", min_length=1, max_length=80)
    state: Literal["pending", "complete"]
    value: Value = None
    comment: str = ""
    reviewer: str | None = None
    assignee: str | None = None
    queue_id: str | None = None
    previous_id: str | None = None
    score_id: str | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def answered_when_complete(self) -> Review:
        if (self.state == "complete") != (self.value is not None):
            raise ValueError("a complete review has a value and a pending one has none")
        return self


TraceState = Literal["ok", "error", "in_progress", "unknown"]


class Span(_Object):
    """One step inside a trace: a model call, a tool call, a retrieval."""

    id: Identifier
    parent_id: str | None = None
    name: str = ""
    kind: str = ""
    status: TraceState = "unknown"
    start_ms: float | None = None
    end_ms: float | None = None
    inputs: Any = None
    outputs: Any = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Trace(_Object):
    """A recorded call to the thing under test, from an eval run or from live traffic. A case result
    is a trace of a run; a trace need not belong to one. ``spans`` is empty in search results and
    filled when one trace is read."""

    id: Identifier
    project_id: Identifier
    name: str = ""
    state: TraceState = "unknown"
    request_time: datetime | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    request: str = ""
    response: str = ""
    run_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    model: str | None = None
    tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tags: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)
    scores: list[Score] = Field(default_factory=list)
    spans: list[Span] = Field(default_factory=list)


class TraceQuery(_Object):
    """Which traces of a project to return, newest first unless ``oldest_first``.

    ``min_latency_ms`` keeps traces whose latency is at least that (a trace with no latency is
    left out), and ``verdicts`` keeps traces with a verdict of each named scorer equal to its
    boolean: a feedback score, never an expectation (:func:`is_expectation`). ``slowest_first``
    orders by latency instead of time, slowest first or, when ``False``, fastest first, with the
    traces that have no latency last. A store applies every filter and the order before it pages,
    so a page is the query's, whatever the project's size."""

    project_id: Identifier
    run_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    trace_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    text: str | None = Field(default=None, max_length=500)
    states: list[TraceState] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    since: datetime | None = None
    until: datetime | None = None
    min_latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    verdicts: dict[str, bool] = Field(default_factory=dict)
    oldest_first: bool = False
    slowest_first: bool | None = None
    limit: int = Field(default=50, ge=1, le=500)
    cursor: str | None = None


def is_expectation(score: Score) -> bool:
    """Whether a score is a person's expectation on a trace rather than a verdict. The core records
    an expectation annotation as a score whose metadata has ``martian.annotation: expectation``, and
    a store binding reads MLflow's expectation assessments the same way."""
    return score.metadata.get("martian.annotation") == "expectation"


def query_keeps(trace: Trace, query: TraceQuery) -> bool:
    """Whether a trace, with its scores, passes the query's latency and verdict filters."""
    if query.min_latency_ms is not None and (trace.latency_ms is None or trace.latency_ms < query.min_latency_ms):
        return False
    return all(
        any(s.scorer == name and not is_expectation(s) and s.value is wanted for s in trace.scores)
        for name, wanted in query.verdicts.items()
    )


def query_order(traces: list[Trace], query: TraceQuery) -> list[Trace]:
    """Traces in the query's order: by request time, then by latency when ``slowest_first`` is set."""
    epoch = datetime.min.replace(tzinfo=UTC)
    found = sorted(traces, key=lambda t: (t.request_time or epoch, t.id), reverse=not query.oldest_first)
    if query.slowest_first is None:
        return found
    timed = sorted(
        (t for t in found if t.latency_ms is not None), key=lambda t: t.latency_ms or 0.0, reverse=query.slowest_first
    )
    return timed + [t for t in found if t.latency_ms is None]


class Page[T](BaseModel):
    """One page of results. ``next_cursor`` is None on the last page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[T]
    next_cursor: str | None = None


SESSION_TRACE_LIMIT = 1000
"""The most traces one session search groups."""


class Session(_Object):
    """One conversation: the traces that share a session id, each one a turn. Built from traces,
    never stored. ``preview`` is the request of the session's last trace in the order the
    search read them, so its first turn when newest first."""

    id: str = Field(min_length=1)
    turns: int = Field(ge=1)
    errors: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    last_at: datetime | None = None
    preview: str = ""
    tokens: int | None = Field(default=None, ge=0)


class SessionSearch(_Object):
    """The sessions among the first ``limit`` traces a query matches, in the order of each
    session's first trace in the query's order. ``has_more`` says more traces matched, so counts
    may be incomplete; ``ungrouped`` counts the traces read that have no session id."""

    items: list[Session]
    has_more: bool = False
    traces_examined: int = Field(ge=0)
    ungrouped: int = Field(ge=0)
    limit: int = Field(default=SESSION_TRACE_LIMIT, ge=1)


def sessions_from_traces(traces: list[Trace], *, has_more: bool, limit: int = SESSION_TRACE_LIMIT) -> SessionSearch:
    """Group traces, in the order a search returned them, into sessions by ``session_id``."""
    groups: dict[str, list[Trace]] = {}
    for trace in traces:
        if trace.session_id:
            groups.setdefault(trace.session_id, []).append(trace)
    items = []
    for session_id, turns in groups.items():
        times = [t.request_time for t in turns if t.request_time is not None]
        tokens = [t.tokens for t in turns if t.tokens is not None]
        items.append(
            Session(
                id=session_id,
                turns=len(turns),
                errors=sum(t.state == "error" for t in turns),
                started_at=min(times, default=None),
                last_at=max(times, default=None),
                preview=turns[-1].request,
                tokens=sum(tokens) if tokens else None,
            )
        )
    return SessionSearch(
        items=items,
        has_more=has_more,
        traces_examined=len(traces),
        ungrouped=sum(not t.session_id for t in traces),
        limit=limit,
    )


class QueueItem(_Object):
    """One trace in a review queue, and whether its review is done."""

    queue_id: Identifier
    trace_id: Identifier
    case_id: str | None = None
    status: Literal["pending", "completed"] = "pending"


def case_status(passed: bool | None) -> CaseStatus:
    """A case's status from the SDK's three-valued verdict."""
    return "error" if passed is None else "passed" if passed else "failed"


# Every field of a ``martian evals run`` results.jsonl record, and where the model keeps it.
RESULTS_RECORD_FIELDS = {
    "id": "CaseResult.case_id",
    "passed": "CaseResult.status",
    "checks": "Score (one per check label)",
    "inputs": "CaseResult.inputs",
    "output": "CaseResult.output",
    "error": "CaseResult.error",
    "fingerprint": "CaseResult.fingerprint",
}


def from_results_record(
    record: dict[str, Any], *, run_id: str, position: int, scorers: dict[str, ScorerVersion] | None = None
) -> tuple[CaseResult, list[Score]]:
    """One ``results.jsonl`` record as a case result and its scores.

    ``scorers`` maps a check label to the scorer that produced it, so each score names its
    version and source; a label not in it is recorded as a check.
    """
    if unknown := sorted(set(record) - set(RESULTS_RECORD_FIELDS)):
        raise ValueError(f"results record has fields the model does not map: {', '.join(unknown)}")
    result = CaseResult(
        run_id=run_id,
        case_id=str(record["id"]),
        position=position,
        status=case_status(record["passed"]),
        inputs=record.get("inputs") or {},
        output=record.get("output"),
        error=record.get("error") or "",
        fingerprint=record.get("fingerprint"),
    )
    scores = []
    for label, check in (record.get("checks") or {}).items():
        scorer = (scorers or {}).get(label)
        scores.append(
            Score(
                run_id=run_id,
                case_id=result.case_id,
                scorer=label,
                scorer_version=scorer.version if scorer else "",
                source="llm_judge" if scorer and scorer.kind == "llm_judge" else "code",
                value=check.get("passed"),
                passed=check.get("passed"),
                rationale=check.get("rationale") or "",
                error=check.get("error") or "",
            )
        )
    return result, scores


__all__ = [
    "MODEL_VERSION",
    "SESSION_TRACE_LIMIT",
    "DatasetDraft",
    "Page",
    "QueueItem",
    "Session",
    "SessionSearch",
    "Span",
    "Trace",
    "TraceQuery",
    "TraceState",
    "RESULTS_RECORD_FIELDS",
    "Case",
    "CaseResult",
    "CaseStatus",
    "Counts",
    "DatasetVersion",
    "Policy",
    "Project",
    "Review",
    "ReviewQuestion",
    "ReviewQueue",
    "Run",
    "RunSettings",
    "Score",
    "ScorerVersion",
    "ScoringJob",
    "StoreRef",
    "Suite",
    "Target",
    "case_status",
    "digest",
    "from_results_record",
    "is_expectation",
    "query_keeps",
    "query_order",
    "sessions_from_traces",
    "suite_digest",
]
