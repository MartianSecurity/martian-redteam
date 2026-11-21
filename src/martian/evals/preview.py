"""Validate suite data and preview deterministic checks on supplied responses."""

from __future__ import annotations

import asyncio
import sys
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError
from yaml.nodes import MappingNode, SequenceNode

from martian._privacy import safe_text
from martian.config import ConfigError
from martian.evals import runner, suite_file
from martian.evals.check_options import check_scorer
from martian.evals.worker.checks import measurement_trace, prepare, score_output

SuiteText = Annotated[str, Field(min_length=1, max_length=65_536)]
CaseText = Annotated[str, Field(min_length=1, max_length=524_288)]
CheckKind = Annotated[str, Field(min_length=1, max_length=60)]
Measurement = Annotated[float, Field(ge=0, allow_inf_nan=False)]
TokenCount = Annotated[int, Field(ge=0)]
MAX_REQUEST_BYTES = 2_500_000


class _Preview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InlineCases(_Preview):
    # MCP interprets top-level JSON-looking strings as objects for some argument types.
    text: CaseText


class SuiteInput(_Preview):
    operation: Literal["validate_suite"] = "validate_suite"
    suite_yaml: SuiteText
    case_data: CaseText | None = None


class CheckSample(_Preview):
    output: JsonValue
    expectations: dict[str, JsonValue] = Field(default_factory=dict)
    latency_ms: Measurement | None = None
    cost_usd: Measurement | None = None
    total_tokens: TokenCount | None = None


class CheckInput(_Preview):
    operation: Literal["run_check"] = "run_check"
    kind: CheckKind
    sample: CheckSample
    options: dict[str, JsonValue] = Field(default_factory=dict)


class SuiteValidation(_Preview):
    valid: bool
    checks: int | None = None
    cases: int | None = None
    target: Literal["stored", "function", "http"] | None = None
    error: str | None = None


class CheckPreview(_Preview):
    verdict: Literal["passed", "failed", "error"]
    reason: str = ""
    error: str = ""


def _message(value: object) -> str:
    return safe_text(value)[:600]


def validate_suite(suite: runner.Suite, case_data: str | None = None) -> SuiteValidation:
    """Apply the suite's static definition, check and dataset rules."""
    try:
        cases = suite_file.validate_definition(suite, case_data)
        for check in suite.checks:
            if check.type in runner.CHECKS:
                try:
                    runner._build_check(check)
                except ValueError as error:
                    raise ConfigError(f"check {check.label}: {error}") from None
    except ConfigError as error:
        return SuiteValidation(valid=False, error=_message(error))
    target: Literal["stored", "function", "http"] = (
        "stored" if suite.target is None else "function" if suite.target.file is not None else "http"
    )
    return SuiteValidation(valid=True, checks=len(suite.checks), cases=cases, target=target)


def _check_yaml(text: str) -> None:
    """Bound expansion of YAML nodes before handing untrusted inline text to the suite parser."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError):
        raise ConfigError("The suite is not valid YAML or is nested too deeply.") from None
    if root is None:
        return
    nodes = [(root, 0)]
    visited = 0
    while nodes:
        node, depth = nodes.pop()
        visited += 1
        if depth > 64 or visited > 65_536:
            raise ConfigError("The expanded suite YAML is too large or nested too deeply.")
        if isinstance(node, MappingNode):
            nodes.extend((child, depth + 1) for pair in node.value for child in pair)
        elif isinstance(node, SequenceNode):
            nodes.extend((child, depth + 1) for child in node.value)


async def _run_check(request: CheckInput) -> CheckPreview:
    try:
        scorer = check_scorer(request.kind, None, request.options)
        check = prepare([scorer])[scorer.name]
    except ValueError as error:
        return CheckPreview(verdict="error", error=_message(error))
    sample = request.sample
    trace = None
    if scorer.type in runner.CALL_CHECKS:
        trace = measurement_trace(sample.latency_ms, tokens=sample.total_tokens, cost_usd=sample.cost_usd)
    value, reason, error_text = await score_output(check, sample.output, dict(sample.expectations), trace)
    if not isinstance(value, bool):
        return CheckPreview(verdict="error", error=_message(error_text or "The check did not give a verdict."))
    return CheckPreview(verdict="passed" if value else "failed", reason=_message(reason))


def main() -> int:
    """Process one request from the local MCP server on stdin."""
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        return 2
    request_type = Annotated[SuiteInput | CheckInput, Field(discriminator="operation")]
    try:
        request = TypeAdapter(request_type).validate_json(raw)
    except ValidationError:
        return 2
    result: SuiteValidation | CheckPreview
    if isinstance(request, SuiteInput):
        try:
            _check_yaml(request.suite_yaml)
            result = validate_suite(runner.parse_suite(request.suite_yaml), request.case_data)
        except ConfigError as error:
            result = SuiteValidation(valid=False, error=_message(error))
    else:
        result = asyncio.run(_run_check(request))
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
