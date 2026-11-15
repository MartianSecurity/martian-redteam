"""The typed request and response bodies of the read routes, as the frozen evals contract has them.

Each class is the schema of the same name in version 3 of ``contracts/evals-api.json`` in
MorningHQ/MartianBackend (``app/schemas.py`` and ``app/evals_explorer_schema.py`` there), field for
field, so the core's OpenAPI operations are the contract's. ``tests/evals/test_read_contract.py``
compares them. The routes build these from the shared model in ``martian.evals.server.projection``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvalDatasetOut(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    item_count: int = 0
    scope: str = "org"
    version: str | None = None


class EvalDatasetItemOut(BaseModel):
    id: str
    status: str
    prompt: str = ""
    expected: str = ""
    usable: bool = False


class EvalDatasetItemsOut(BaseModel):
    name: str
    page: int
    limit: int
    query: str = ""
    total: int | None = None
    total_pages: int | None = None
    has_next: bool = False
    scan_truncated: bool = False
    items: list[EvalDatasetItemOut] = []


class EvalItemOut(BaseModel):
    id: uuid.UUID
    dataset_item_id: str
    position: int
    status: str
    prompt: str
    expected: str
    output: str
    error: str
    evals_trace_id: str = ""
    evals_assessment_id: str = ""
    latency_ms: float | None = None
    evals_telemetry_error: str = ""
    finished_at: datetime | None


class EvalRunOut(BaseModel):
    id: uuid.UUID
    run_code: str
    state: str
    dataset_name: str
    dataset_id: str
    project_id: uuid.UUID | None = None
    max_items: int = 100
    planned_count: int = 0
    cancel_requested_at: datetime | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    truncated: bool
    passed: int
    failed: int
    skipped: int
    errored: int
    error: str
    agent_id: uuid.UUID
    agent_name: str | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    evals_provider: Literal["mlflow"] | None = None
    evals_telemetry_error: str = ""
    source: str = "console"
    ci: dict[str, Any] | None = None

    @field_validator("ci")
    @classmethod
    def public_ci(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        # Provenance only; the token identity and request digest stay server-side.
        if value is None:
            return None
        keep = ("git_sha", "ref", "repo", "run_url", "kit_version", "threshold", "minimum_coverage")
        return {k: value.get(k) for k in keep}


class EvalRunDetailOut(EvalRunOut):
    items: list[EvalItemOut] = []


# The trace search request. Not martian.evals.model.TraceQuery: the trace routes translate it. No
# docstring: it would become the schema's description, which the contract's has none of.
class TraceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID | None = None
    q: str = Field(default="", max_length=200)
    state: Literal["", "OK", "ERROR", "IN_PROGRESS"] = ""
    session_id: str = Field(default="", max_length=200)
    user_id: str = Field(default="", max_length=200)
    start_time_ms: int | None = Field(default=None, ge=0)
    end_time_ms: int | None = Field(default=None, ge=0)
    min_latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    assessment: str = Field(default="", max_length=80, pattern=r"^$|^[a-zA-Z][a-zA-Z0-9_.-]*$")
    assessment_value: Literal["", "true", "false"] = ""
    order: Literal["newest", "oldest", "slowest", "fastest"] = "newest"
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=16000)

    @model_validator(mode="after")
    def valid_range(self) -> TraceQuery:
        if self.start_time_ms is not None and self.end_time_ms is not None and self.start_time_ms > self.end_time_ms:
            raise ValueError("Start must precede end")
        if bool(self.assessment) != bool(self.assessment_value):
            raise ValueError("Choose an assessment name and value together")
        return self
