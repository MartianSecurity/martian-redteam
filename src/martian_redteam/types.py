"""Shared types for red-team runs, test cases, and risk reports."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

ErrorKind = Literal["infrastructure", "target"]


def _require_exact_int(value: object) -> object:
    """Reject anything that is not exactly ``int`` before the literal check.

    Pydantic compares literals by equality, so ``True``, ``1.0`` and (in JSON)
    ``true`` all match ``Literal[0, 1]``, and ``Field(strict=True)`` cannot be
    applied to a literal schema. A judge that answers ``vulnerable: true``
    style booleans must fail the schema instead of being read as a pass.
    """
    if value is not None and type(value) is not int:
        raise ValueError("score must be the integer 0 or 1")
    return value


BinaryScore = Annotated[Literal[0, 1], BeforeValidator(_require_exact_int)]


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
    score: BinaryScore | None = None
    reason: str | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    graph_revision: str | None = None


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

    score: BinaryScore
    reason: str = Field(min_length=1)
    graph_revision: str | None = None
    messages: list[Any] = Field(default_factory=list)
