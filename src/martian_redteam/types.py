"""Shared types for red-team runs, test cases, and risk reports."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ErrorKind = Literal["infrastructure", "target"]


class Turn(BaseModel):
    """One conversation turn from the target or simulator."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["human", "ai", "tool"]
    content: str
    tools_called: list[str] = Field(default_factory=list)


class TestCase(BaseModel):
    """One probe: attack, target reply, and evaluator verdict or error."""

    __test__ = False
    model_config = ConfigDict(extra="forbid")

    vulnerability: str
    vulnerability_type: str
    attack_method: str
    input: str | None = None
    actual_output: str | None = None
    turns: list[Turn] = Field(default_factory=list)
    score: Literal[0, 1] | None = None
    reason: str | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    graph_revision: str | None = None

    @field_validator("score")
    @classmethod
    def score_must_be_binary(cls, value: Literal[0, 1] | None) -> Literal[0, 1] | None:
        if value is not None and value not in (0, 1):
            raise ValueError("score must be 0, 1, or null")
        return value


class VulnerabilityTypeResult(BaseModel):
    vulnerability: str
    vulnerability_type: str
    pass_rate: float
    passing: int
    failing: int
    errored: int
    gated: bool


class RiskAssessment(BaseModel):
    """Aggregated result of a red-team run."""

    model_config = ConfigDict(extra="forbid")

    catalog_version: str
    graph_revision: str | None = None
    purpose: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    run_duration_seconds: float = 0.0
    test_cases: list[TestCase] = Field(default_factory=list)
    vulnerability_type_results: list[VulnerabilityTypeResult] = Field(default_factory=list)
    errored: int = 0
    infrastructure_errors: int = 0
    target_errors: int = 0

    def save(self, to: str | Path) -> Path:
        """Write JSON and Markdown reports. Returns the JSON path."""
        directory = Path(to)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "risk-assessment.json"
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        from martian_redteam.report import append_github_summary, write_markdown

        write_markdown(self, directory)
        append_github_summary(self)
        return path


class SimulatorContext(BaseModel):
    """Fields sent as Aegra run context for assistant_id=simulator."""

    model_config = ConfigDict(extra="forbid")

    vulnerability: str
    vulnerability_type: str
    purpose: str
    attack_method: str
    mode: Literal["baseline", "enhance"] = "baseline"


class EvaluatorContext(BaseModel):
    """Fields sent as Aegra run context for assistant_id=evaluator."""

    model_config = ConfigDict(extra="forbid")

    vulnerability: str
    vulnerability_type: str
    attack_method: str
    criteria: str | None = None


class SimulatorOutput(BaseModel):
    """Graph state returned by POST /runs/wait for the simulator."""

    model_config = ConfigDict(extra="ignore")

    attack: str = Field(min_length=1)
    attack_method: str = Field(min_length=1)
    graph_revision: str | None = None
    messages: list[Any] = Field(default_factory=list)


class EvaluatorOutput(BaseModel):
    """Graph state returned by POST /runs/wait for the evaluator."""

    model_config = ConfigDict(extra="ignore")

    score: Literal[0, 1]
    reason: str = Field(min_length=1)
    graph_revision: str | None = None
    messages: list[Any] = Field(default_factory=list)
