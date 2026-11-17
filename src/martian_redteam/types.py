"""Shared types for red-team runs, test cases, and risk reports."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PrivateAttr, field_validator

from martian._errors import error_code, saved_error_code
from martian._privacy import RedactionPolicy

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
    policy_revision: str | None = None
    simulation_operation_id: str | None = None
    evaluation_operation_id: str | None = None
    _error_code: str | None = PrivateAttr(default=None)

    @property
    def error_code(self) -> str | None:
        return self._error_code or saved_error_code(self.error)

    def _bind_error_code(self, code: object) -> TestCase:
        self._error_code = error_code(code)
        return self


class VulnerabilityTypeResult(BaseModel):
    vulnerability: str
    vulnerability_type: str
    pass_rate: float = Field(ge=0, le=1)
    passing: int = Field(ge=0)
    failing: int = Field(ge=0)
    errored: int = Field(ge=0)
    gated: bool


class RiskAssessment(BaseModel):
    """Aggregated result of a red-team run."""

    model_config = ConfigDict(extra="forbid")

    catalog_version: str
    graph_revision: str | None = None
    policy_revision: str | None = None
    purpose: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    run_duration_seconds: float = 0.0
    test_cases: list[TestCase] = Field(default_factory=list)
    vulnerability_type_results: list[VulnerabilityTypeResult] = Field(default_factory=list)
    errored: int = 0
    infrastructure_errors: int = 0
    target_errors: int = 0
    expected_probe_count: int | None = Field(default=None, ge=1)
    _redaction: RedactionPolicy = PrivateAttr(default_factory=RedactionPolicy)

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return self._redaction

    def _bind_redaction(self, policy: RedactionPolicy) -> RiskAssessment:
        self._redaction = self._redaction.combine(policy)
        return self

    def __repr__(self) -> str:
        from martian._privacy import safe_text

        return safe_text(super().__repr__(), policy=self._redaction)

    def __str__(self) -> str:
        from martian._privacy import safe_text

        return safe_text(super().__str__(), policy=self._redaction)

    def for_report(self, *, include_target_content: bool = False) -> RiskAssessment:
        """A sanitized copy; the in-memory assessment remains available to the customer."""
        from martian._errors import message
        from martian._privacy import redact_data, report_text, safe_text

        data = redact_data(self.model_dump(), policy=self._redaction)
        data["purpose"] = report_text(
            self.purpose or "",
            include_content=include_target_content,
            redact_env="MARTIAN_EVALS_REDACT_INPUTS",
            policy=self._redaction,
        )
        for original, case in zip(self.test_cases, data["test_cases"], strict=True):
            for field in ("input", "actual_output", "reason"):
                if case[field] is not None:
                    flag = "MARTIAN_EVALS_REDACT_INPUTS" if field == "input" else "MARTIAN_EVALS_REDACT_OUTPUTS"
                    case[field] = report_text(
                        case[field], include_content=include_target_content, redact_env=flag, policy=self._redaction
                    )
            if case["error"]:
                code = original.error_code
                case["error"] = (
                    message(code, operation_id=case["evaluation_operation_id"] or case["simulation_operation_id"])
                    if code
                    else f"{case['error_kind'] or 'probe'} execution failed"
                )
            for turn in case["turns"]:
                turn["content"] = report_text(
                    turn["content"],
                    include_content=include_target_content,
                    redact_env="MARTIAN_EVALS_REDACT_OUTPUTS",
                    policy=self._redaction,
                )
                turn["tools_called"] = [safe_text(t, policy=self._redaction) for t in turn["tools_called"]]
        return RiskAssessment.model_validate(data)._bind_redaction(self._redaction)

    def save(self, to: str | Path, *, include_target_content: bool = False) -> Path:
        """Write JSON and Markdown reports. Returns the JSON path."""
        directory = Path(to)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "risk-assessment.json"
        report = self.for_report(include_target_content=include_target_content)
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        from martian_redteam.report import append_github_summary, write_markdown

        write_markdown(report, directory)
        append_github_summary(self.for_report())
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


class ModelUsage(BaseModel):
    """What one model call used, as its provider reported it. ``None`` where it reported nothing."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class SimulatorOutput(BaseModel):
    """Graph state returned by POST /runs/wait for the simulator."""

    model_config = ConfigDict(extra="ignore")

    attack: str = Field(min_length=1)
    attack_method: str = Field(min_length=1)
    graph_revision: str | None = None
    policy_revision: str | None = None
    operation_id: str | None = None
    messages: list[Any] = Field(default_factory=list)
    usage: ModelUsage | None = None
    """What the generation used, when the service reports it (the local service does)."""

    @field_validator("attack", "attack_method")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class EvaluatorOutput(BaseModel):
    """Graph state returned by POST /runs/wait for the evaluator."""

    model_config = ConfigDict(extra="ignore")

    score: BinaryScore
    reason: str = Field(min_length=1)
    graph_revision: str | None = None
    policy_revision: str | None = None
    operation_id: str | None = None
    messages: list[Any] = Field(default_factory=list)
    usage: ModelUsage | None = None
    """What the judgment used, when the service reports it (the local service does)."""

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class RedteamService(Protocol):
    """Generation/judgment interface; orchestration never chooses credentials or origin."""

    @property
    def redaction_policy(self) -> RedactionPolicy: ...

    async def simulate(self, context: SimulatorContext, *, prompt: str | None = None) -> SimulatorOutput: ...

    async def evaluate(
        self, context: EvaluatorContext, *, attack: str, target_output: str, tools_called: list[str] | None = None
    ) -> EvaluatorOutput: ...
