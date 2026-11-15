"""The typed request and response bodies of the write routes, as the frozen evals contract has them.

Each class is the schema of the same name in version 3 of ``contracts/evals-api.json`` in
MorningHQ/MartianBackend, field for field and with the same validation: ``EvalRunRequest`` from
``app/schemas.py``, the scoring, review and suite requests from ``app/evals_workbench_schema.py``,
the project, judge, question, annotation and view requests from ``app/evals_explorer_schema.py``,
and the dataset authoring bodies from ``app/api/evals_datasets.py``. ``tests/evals/
test_write_contract.py`` compares them. No class has a docstring: it would become the schema's
description, which the contract's have none of.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from martian.evals.judge_settings import ENDPOINT_PATTERN, MODEL_URI_PATTERN, judge_model_problem
from martian.evals.server.schemas import TraceQuery
from martian.evals.worker.scorers import CODE_SCORERS, JUDGE_SCORERS

ScorerName = Literal[
    "exact_match",
    "contains_expected",
    "valid_json",
    "response_length",
    "correctness",
    "safety",
    "policy",
]
RESERVED_NAMES = frozenset({*CODE_SCORERS, *JUDGE_SCORERS, "human_review", "martian_triage"})


class EvalRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    agent_id: uuid.UUID
    project_id: uuid.UUID | None = None
    max_items: int = Field(default=100, ge=1, le=1000)
    dataset_name: str = Field(min_length=1, max_length=200)


# --- scoring, reviews and suites ------------------------------------------------------------


class JudgeReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,59}$")
    version: int = Field(ge=1)


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    scorers: list[ScorerName] = Field(default_factory=list, max_length=7)
    judges: list[JudgeReference] = Field(default_factory=list, max_length=7)
    suite_version_id: uuid.UUID | None = None
    guidelines: str = Field(default="", max_length=4000)
    acknowledge_model_cost: bool = False

    @model_validator(mode="after")
    def validate_suite(self) -> ScoreRequest:
        if len(set(self.scorers)) != len(self.scorers):
            raise ValueError("Select each scorer only once")
        names = [j.name for j in self.judges]
        if (
            not 1 <= len(self.scorers) + len(names) <= 7
            or len(set(names)) != len(names)
            or set(names) & {*CODE_SCORERS, *JUDGE_SCORERS}
        ):
            raise ValueError("Choose one to seven unique criteria")
        if "policy" in self.scorers and not self.guidelines.strip():
            raise ValueError("Policy scoring requires guidelines")
        if "policy" not in self.scorers and self.guidelines:
            raise ValueError("Guidelines require the policy scorer")
        if (set(JUDGE_SCORERS) & set(self.scorers) or self.judges) and not self.acknowledge_model_cost:
            raise ValueError("Model scoring requires cost acknowledgement")
        return self


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    previous_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    state: Literal["pending", "complete"]
    value: StrictBool | StrictInt | StrictFloat | StrictStr | None = None
    queue_id: uuid.UUID | None = None
    question_name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,59}$")
    comment: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_rating(self) -> ReviewRequest:
        if self.question_name and not self.queue_id:
            raise ValueError("A named question requires its review queue")
        if (self.state == "complete") != (self.value is not None):
            raise ValueError("Completed reviews require a rating; pending reviews have no rating")
        if self.queue_id is None and self.value is not None and type(self.value) is not bool:
            raise ValueError("Unstructured reviews require a Boolean rating")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("Rating must be finite")
        if isinstance(self.value, str) and len(self.value) > 4000:
            raise ValueError("Rating is too long")
        if self.state == "complete" and not self.comment.strip():
            raise ValueError("Explain the human rating")
        return self


class CriterionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ScorerName
    direction: Literal["higher", "lower"] = "higher"
    threshold: float | None = Field(default=None, allow_inf_nan=False)
    minimum_coverage: float = Field(default=1, ge=0, le=1)


class SuiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    suite_id: uuid.UUID | None = None
    expected_version: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=120)
    scorers: list[ScorerName] = Field(min_length=1, max_length=7)
    guidelines: str = Field(default="", max_length=4000)
    policies: list[CriterionPolicy] = Field(default_factory=list, max_length=7)

    @model_validator(mode="after")
    def definition(self) -> SuiteRequest:
        ScoreRequest(
            request_id=self.request_id, scorers=self.scorers, guidelines=self.guidelines, acknowledge_model_cost=True
        )
        names = [p.name for p in self.policies]
        if len(set(names)) != len(names) or not set(names).issubset(self.scorers):
            raise ValueError("Policies must refer to unique selected scorers")
        if not self.name.strip():
            raise ValueError("Name is required")
        for policy in self.policies:
            if policy.name != "response_length" and policy.threshold is not None and not 0 <= policy.threshold <= 1:
                raise ValueError("Boolean scorer thresholds are pass rates between zero and one")
        return self


class ReviewQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="quality", pattern=r"^[a-z][a-z0-9_]{0,59}$")
    kind: Literal["boolean", "numeric", "categorical", "text"] = "boolean"
    instruction: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=20)
    minimum: float = Field(default=0, allow_inf_nan=False)
    maximum: float = Field(default=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounds(self) -> ReviewQuestion:
        if self.name == "human_review":
            raise ValueError("human_review is reserved for the general review audit chain")
        if not self.instruction.strip():
            raise ValueError("Question instruction is required")
        if self.minimum >= self.maximum:
            raise ValueError("Minimum must be below maximum")
        if self.kind == "categorical" and (
            len(self.options) < 2
            or len(set(self.options)) != len(self.options)
            or any(not x.strip() or len(x) > 100 for x in self.options)
        ):
            raise ValueError("Provide 2 to 20 unique category labels")
        if self.kind != "categorical" and self.options:
            raise ValueError("Only categorical questions have options")
        return self


class QueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    run_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    assignee_id: uuid.UUID | None = None
    question: ReviewQuestion | None = None
    questions: list[ReviewQuestion] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_items(self) -> QueueRequest:
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("Select each result only once")
        if not self.name.strip():
            raise ValueError("Name is required")
        if bool(self.question) == bool(self.questions):
            raise ValueError("Supply one question or a list of questions")
        names = [q.name for q in self.question_list()]
        if len(names) != len(set(names)):
            raise ValueError("Each queue question needs a unique criterion name")
        return self

    def question_list(self) -> list[ReviewQuestion]:
        return self.questions or ([self.question] if self.question else [])


# --- projects, judges, questions, annotations and views -------------------------------------


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectRequest(StrictRequest):
    request_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def nonblank(self) -> ProjectRequest:
        if not self.name.strip():
            raise ValueError("Project name is required")
        return self


JudgeModelUri = Annotated[str, Field(max_length=240, pattern=MODEL_URI_PATTERN)]
JudgeEndpoint = Annotated[str, Field(max_length=500, pattern=ENDPOINT_PATTERN)]


class JudgeVersionRequest(StrictRequest):
    request_id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,59}$")
    expected_version: int = Field(default=0, ge=0)
    description: str = Field(default="", max_length=1000)
    instructions: str = Field(min_length=20, max_length=4000)
    kind: Literal["boolean", "numeric", "categorical", "text"] = "boolean"
    options: list[str] = Field(default_factory=list, max_length=20)
    minimum: float = Field(default=0, allow_inf_nan=False)
    maximum: float = Field(default=1, allow_inf_nan=False)
    model: JudgeModelUri | None = None
    endpoint: JudgeEndpoint | None = None

    @model_validator(mode="after")
    def definition(self) -> JudgeVersionRequest:
        if self.name in RESERVED_NAMES:
            raise ValueError("This name is reserved for an existing criterion")
        if problem := judge_model_problem(self.model, self.endpoint):
            raise ValueError(problem)
        if "{{ outputs }}" not in self.instructions:
            raise ValueError("Include {{ outputs }} in the judge instructions")
        # Bounds apply only to numeric judges; normalise them so a definition has one form.
        if self.kind != "numeric":
            self.minimum, self.maximum = 0, 1
        ReviewQuestion(
            name=self.name,
            instruction=self.instructions,
            kind=self.kind,
            options=self.options,
            minimum=self.minimum,
            maximum=self.maximum,
        )
        return self


class JudgeModelRequest(StrictRequest):
    model: JudgeModelUri | None = None
    endpoint: JudgeEndpoint | None = None

    @model_validator(mode="after")
    def consistent(self) -> JudgeModelRequest:
        if problem := judge_model_problem(self.model, self.endpoint):
            raise ValueError(problem)
        return self


class JudgeModelOut(BaseModel):
    model: str | None
    endpoint: str | None
    selectable: bool


class QuestionVersionRequest(StrictRequest):
    project_id: uuid.UUID | None = None
    expected_version: int = Field(default=0, ge=0, le=99999998)
    question: ReviewQuestion


class TraceAnnotationRequest(StrictRequest):
    project_id: uuid.UUID | None = None
    request_id: uuid.UUID
    kind: Literal["feedback", "expectation"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,59}$")
    value: (
        StrictBool
        | Annotated[float, Field(strict=True, allow_inf_nan=False)]
        | Annotated[str, Field(min_length=1, max_length=4000)]
    )
    rationale: str = Field(default="", max_length=2000, pattern=r"^[^\x00]*$")

    @model_validator(mode="after")
    def validate_annotation(self) -> TraceAnnotationRequest:
        if self.name == "exact_match":
            raise ValueError("Choose a name other than the reserved exact_match criterion")
        if isinstance(self.value, str) and not self.value.strip():
            raise ValueError("Enter an annotation value")
        if self.kind == "expectation" and (not isinstance(self.value, str) or self.rationale):
            raise ValueError("Enter a text expectation")
        return self


TRACE_COLUMNS = frozenset(
    {"name", "state", "request", "response", "timestamp", "latency", "tokens", "cost", "session", "user"}
    | {"assessments", "model"}
)


class SavedViewRequest(StrictRequest):
    request_id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    visibility: Literal["private", "organization"] = "private"
    revision: int = Field(default=0, ge=0)
    query: TraceQuery = Field(default_factory=TraceQuery)
    columns: list[str] = Field(
        default_factory=lambda: ["name", "state", "request", "response", "timestamp", "latency", "assessments"],
        min_length=1,
        max_length=12,
    )
    density: Literal["comfortable", "compact"] = "comfortable"

    @model_validator(mode="after")
    def valid_view(self) -> SavedViewRequest:
        if (
            not self.name.strip()
            or len(set(self.columns)) != len(self.columns)
            or not set(self.columns) <= TRACE_COLUMNS
        ):
            raise ValueError("Choose a name and unique supported columns")
        if self.query.cursor or self.query.project_id != self.project_id:
            raise ValueError("Saved views must use this project without a cursor")
        self.name = self.name.strip()
        return self


# --- dataset authoring ------------------------------------------------------------------------


class DraftCreateIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=4000)
    from_dataset: str | None = Field(
        default=None, max_length=200, description="Seed from a published dataset (edit or copy)"
    )


class DraftPatchIn(BaseModel):
    base_revision: int
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class DraftOut(BaseModel):
    id: uuid.UUID
    scope: str
    name: str
    public_name: str
    description: str
    source_dataset: str | None
    revision: int
    state: str
    record_count: int
    operation_id: uuid.UUID | None
    published_sha256: str | None
    created_by: uuid.UUID
    updated_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class RecordIn(BaseModel):
    base_revision: int
    record: dict[str, Any]


class RecordPatchIn(BaseModel):
    base_revision: int
    changes: dict[str, Any]


class MergeIn(BaseModel):
    base_revision: int
    format: Literal["json", "jsonl", "csv"]
    content: str | None = Field(default=None, description="Raw file text (required for jsonl and csv)")
    dataset: Any = Field(default=None, description="Parsed JSON dataset, as an alternative to content")
    dry_run: bool = False


class DeleteIn(BaseModel):
    base_revision: int
    item_ids: list[str] = Field(min_length=1, max_length=100_000)


class FromEvalRunIn(BaseModel):
    base_revision: int
    eval_run_id: uuid.UUID
    item_result_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    expected_from: Literal["expected", "actual"] = "expected"


class PublishIn(BaseModel):
    base_revision: int
    expected_sha256: str = Field(min_length=64, max_length=64)
    rationale: str = Field(min_length=8, max_length=2000)


class ResumeIn(BaseModel):
    rationale: str = Field(min_length=8, max_length=2000)
    confirmed_not_submitted: bool = False


class AbortIn(BaseModel):
    rationale: str = Field(min_length=8, max_length=2000)
    before_submission_only: StrictBool = False
