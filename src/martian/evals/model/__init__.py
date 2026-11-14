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
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class Target(_Object):
    """The thing under test. Header values and credentials are never part of the model, only the
    names of the headers an HTTP target sends."""

    kind: TargetKind
    file: str | None = None
    function: str | None = None
    url: str | None = None
    body: dict[str, Any] | None = None
    response_path: str | None = None
    header_names: list[str] = Field(default_factory=list)
    agent_id: str | None = None

    @model_validator(mode="after")
    def fields_for_kind(self) -> Target:
        given = {
            name for name in ("file", "function", "url", "body", "response_path", "agent_id") if getattr(self, name)
        }
        if self.header_names:
            given.add("header_names")
        allowed = {
            "function": {"file", "function"},
            "http": {"url", "body", "response_path", "header_names"},
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
    """One way of scoring a case, pinned to the version that produced the score."""

    name: Identifier
    kind: ScorerKind
    type: str = Field(min_length=1, max_length=80)
    version: str = ""
    model: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    criteria: dict[str, Any] = Field(default_factory=dict)


class Policy(_Object):
    """When a scorer's results pass the suite."""

    scorer: Identifier
    direction: Literal["higher", "lower"] = "higher"
    threshold: float | None = Field(default=None, allow_inf_nan=False)
    minimum_coverage: float = Field(default=1, ge=0, le=1)


class Suite(_Object):
    """A dataset, a target and scorers, run together. A new version for every change."""

    id: Identifier
    name: str = Field(min_length=1, max_length=120)
    version: int = Field(default=1, ge=1)
    dataset_name: str | None = None
    dataset_digest: Digest | None = None
    target: Target | None = None
    scorers: list[ScorerVersion] = Field(min_length=1)
    policies: list[Policy] = Field(default_factory=list)
    digest: Digest

    @model_validator(mode="after")
    def consistent(self) -> Suite:
        names = [scorer.name for scorer in self.scorers]
        if len(set(names)) != len(names):
            raise ValueError("scorer names must be unique within a suite")
        if unknown := sorted({policy.scorer for policy in self.policies} - set(names)):
            raise ValueError(f"policies name scorers the suite does not have: {', '.join(unknown)}")
        return self


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
    """One scorer's verdict on one case result. ``passed`` is None when there is no verdict."""

    run_id: Identifier
    case_id: Identifier
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
    """Scoring a finished run again with more scorers. Its scores add to the run's, never replace them."""

    id: Identifier
    run_id: Identifier
    scorers: list[ScorerVersion] = Field(min_length=1)
    state: RunState
    total: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    error: str = ""
    requested_by: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


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
    "Score",
    "ScorerVersion",
    "ScoringJob",
    "StoreRef",
    "Suite",
    "Target",
    "case_status",
    "digest",
    "from_results_record",
]
