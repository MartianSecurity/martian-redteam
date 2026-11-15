"""Run an eval suite from one YAML file, locally, with only a model key.

    martian evals run evals.yaml

The suite names a data file (JSONL or CSV), the checks to apply, and optionally the target under
test: a Python function, or an HTTP endpoint. Nothing here contacts the Martian platform: a
function target runs in this process, a URL target is called from it, LLM judges call the model
you configure, and MLflow's evaluation harness uses a local store in the output folder unless you
point it at your own server. The run, its case results, scores and traces are also recorded in the
local Evals UI's data folder, so ``martian ui`` shows them. A run can be stopped and started again;
a finished case is skipped while nothing that produced its verdict has changed. A suite can run each
case several times and combine the attempts' verdicts by a rule (:mod:`martian.evals.repeats`).
A case can be a conversation of several user turns, sent in order to a target that keeps the
conversation and graded per turn and as a whole (:mod:`martian.evals.conversations`). Use
``martian ci`` when a result must be recorded on the platform.

Every target call has a time limit, ``target.timeout_seconds``: a URL target's client stops the
call, and a function runs on a thread of its own, which the run stops waiting for at the limit. A
run has limits too (``limits``: ``max_seconds``, ``max_judge_tokens``): once it reaches one, it
calls no more targets, records the cases it finished, and ends ``stopped``; running it again
resumes after them. A case is the unit a run stops at and resumes from: with repeats, a case whose
attempts did not all run has no result, and so has a conversation whose turns did not all run;
running again runs all of its attempts and turns. Cancelling the run in ``martian ui`` stops it the
same way, as ``cancelled``.
"""

from __future__ import annotations

import contextvars
import csv
import functools
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from martian._version import __version__
from martian.config import ConfigError
from martian.evals import _numeric, _policy
from martian.evals._endpoint_errors import names_unknown_model
from martian.evals._json_schema import check_schema
from martian.evals.conversations import (
    CONVERSATION_INPUTS,
    HISTORY,
    MESSAGES,
    PLACEHOLDERS,
    SESSION_ID,
    assistant,
    conversation_record,
    graded_turns,
    replies,
    turn_checks,
    user,
    user_text,
)
from martian.evals.model import MAX_ATTEMPTS, MAX_TURNS, CheckScope, CombineRule, Turn
from martian.evals.repeats import combine_attempts, passed_every_attempt, plan, reliability, representative
from martian.evals.stats import proportion_interval

GROUP_TAG = "martian.group"
"""The case tag that holds a data file case's ``group``."""
# LLM judges graded by criteria the suite or the case gives, under the judge's own type name.
CRITERIA_JUDGES = ("rubric", "guidelines", "conditions")
# LLM judges of the case's documents (inputs.context) that take no criteria.
GROUNDING_JUDGES = ("groundedness", "context_relevance", "context_sufficiency")
JUDGES = CRITERIA_JUDGES + GROUNDING_JUDGES
CHECKS = (
    "contains",
    "not_contains",
    "contains_any",
    "contains_all",
    "equals",
    "starts_with",
    "ends_with",
    "similarity",
    "matches",
    "not_matches",
    "is_json",
    "json_schema",
    "length",
    "latency",
    "cost",
    "number_equals",
    "sums_to",
    "reconciles",
    "table_equals",
    "no_prohibited_phrases",
    "no_personal_data",
    "no_secrets",
    "resists_injection",
    "no_refusal",
)
# Checks that look for given text, from `values` or from each case's `expectation` key.
TEXT_CHECKS = (
    "contains",
    "not_contains",
    "contains_any",
    "contains_all",
    "equals",
    "starts_with",
    "ends_with",
    "similarity",
)
# Checks that measure the target's call rather than its reply.
CALL_CHECKS = ("latency", "cost")
# Checks that read the reply's numbers and tables as exact decimals, within a tolerance.
NUMERIC_CHECKS = ("number_equals", "sums_to", "reconciles", "table_equals")
# The policy checks. A reply that refuses passes the first four, so a suite with any of them, or
# with a no_refusal check, reports how many of its benign cases were refused (false_refusals).
REFUSAL_PASSING = ("no_prohibited_phrases", "no_personal_data", "no_secrets", "resists_injection")
POLICY_CHECKS = (*REFUSAL_PASSING, "no_refusal")
SECURITY_CHECKS = ("not_contains", "not_matches", *REFUSAL_PASSING)
"""Check types that fail a reply for holding something it must never hold, such as a leaked secret,
personal data, a prohibited phrase or an injected instruction's marker. A case's attempts combine by
``all`` for these unless the check names its own rule, so a leak in one attempt of five is never
outvoted. ``no_refusal`` is not one: it fails a reply for refusing a benign request, which measures
over-refusal rather than a leak, so its attempts combine by the suite's rule. A new check type that
fails a reply for a leak belongs here."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# In a URL target's body: {{input}}, {{inputs.NAME}} and {{uuid}} are filled in for each case, and
# {{session_id}}, {{history}} and {{messages}} with the conversation (martian.evals.conversations).
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_PLACEHOLDER_NAME = re.compile(r"input|uuid|session_id|history|messages|inputs\.[A-Za-z_][A-Za-z0-9_]*")
# In a URL target's url and header values: ${NAME} is read from the environment.
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _placeholders(value: Any) -> list[str]:
    """Every {{name}} in the string values of a body template."""
    if isinstance(value, str):
        return _PLACEHOLDER.findall(value)
    if isinstance(value, dict):
        return [name for item in value.values() for name in _placeholders(item)]
    if isinstance(value, list):
        return [name for item in value for name in _placeholders(item)]
    return []


DEFAULT_TARGET_TIMEOUT_SECONDS = 120
"""The time limit of one target call when nothing sets it: a suite file's ``target.timeout_seconds``
and the pytest gate's ``target_timeout_seconds``."""


class TargetCallError(RuntimeError):
    """A URL target call failed. The message is the SDK's own wording: it names what went wrong
    and never holds the address, a header, a credential or anything the server sent back."""


def _check_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        raise ValueError("target.url is not a valid address") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("target.url must be an http or https address")
    if parts.username is not None or parts.password is not None:
        raise ValueError("target.url must not contain credentials; send them in target.headers")


def _response_steps(path: str | None, setting: str) -> list[str]:
    """The steps of a ``response_path``. A step of digits indexes a list, so its digits must be
    0-9: one with other digits, such as ``²``, is a ``ValueError`` that names ``setting``."""
    steps = [part for part in (path or "").split(".") if part]
    for position, step in enumerate(steps, 1):
        if step.isdigit() and not step.isascii():
            raise ValueError(f"{setting} indexes lists with the digits 0-9 only; step {position} uses other digits")
    return steps


class TargetRef(_Model):
    """The thing under test: a Python function in a file, or an HTTP endpoint that takes a JSON POST.
    ``timeout_seconds`` bounds each call of either."""

    file: str | None = None
    function: str = "target"
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    response_path: str | None = None
    timeout_seconds: float = Field(default=DEFAULT_TARGET_TIMEOUT_SECONDS, ge=1, le=600)

    @model_validator(mode="after")
    def one_kind(self) -> TargetRef:
        if (self.file is None) == (self.url is None):
            raise ValueError("a target needs a file or a url, not both")
        given = self.model_fields_set
        if self.file is not None:
            extra = sorted(given & {"headers", "body", "response_path"})
            if extra:
                raise ValueError(f"{', '.join(extra)} only applies to a url target")
            return self
        if "function" in given:
            raise ValueError("function only applies to a file target")
        if not _ENV_REFERENCE.search(self.url or ""):  # otherwise checked once the variables are read
            _check_url(self.url or "")
        _response_steps(self.response_path, "target.response_path")
        if not self.body:
            raise ValueError("a url target needs a body, with {{input}} where the case's input goes")
        names = _placeholders(self.body)
        unknown = sorted({name for name in names if not _PLACEHOLDER_NAME.fullmatch(name)})
        if unknown:
            allowed = "{{input}}, {{inputs.NAME}}, {{uuid}}, {{session_id}}, {{history}} or {{messages}}"
            raise ValueError(f"unknown body placeholder(s) {', '.join(unknown)}; use {allowed}")
        if not any(name not in ("uuid", SESSION_ID, HISTORY) for name in names):
            raise ValueError(
                "a url target's body must use {{input}}, {{inputs.NAME}} or {{messages}}, or every case sends the same"
            )
        return self


class JudgeConfig(_Model):
    model: str
    inference_params: dict[str, float | int] = Field(default_factory=dict)
    # Added to every judge request; ${NAME} in a value is read from the environment.
    headers: dict[str, str] = Field(default_factory=dict)
    # Server errors and connection failures are retried this many times.
    max_retries: int = Field(default=5, ge=0, le=10)
    # Rate limits are retried for up to this many seconds, as long as the provider asks.
    rate_limit_wait_seconds: float = Field(default=300, ge=0, le=3600)


class CheckSpec(_Model):
    type: Literal[
        "rubric",
        "guidelines",
        "conditions",
        "groundedness",
        "context_relevance",
        "context_sufficiency",
        "contains",
        "not_contains",
        "contains_any",
        "contains_all",
        "equals",
        "starts_with",
        "ends_with",
        "similarity",
        "matches",
        "not_matches",
        "is_json",
        "json_schema",
        "length",
        "latency",
        "cost",
        "number_equals",
        "sums_to",
        "reconciles",
        "table_equals",
        "no_prohibited_phrases",
        "no_personal_data",
        "no_secrets",
        "resists_injection",
        "no_refusal",
    ]
    name: str | None = None
    # Criteria for an LLM judge, applied to every row that does not carry its own.
    rubric: str | None = None
    guidelines: list[str] | None = None
    conditions: list[dict[str, str]] | None = None
    # Deterministic checks.
    values: list[str] = Field(default_factory=list)
    expectation: str | None = None
    any_of: bool = False
    case_sensitive: bool = False
    min_similarity: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    pattern: str | None = None
    required_keys: list[str] = Field(default_factory=list)
    # `schema` in the file; a field of that name would hide BaseModel.schema.
    json_schema: dict[str, Any] | None = Field(default=None, alias="schema")
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    max_ms: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, ge=1)
    # Numeric and table checks. A figure stays as written, a number or text such as "1,234.56",
    # so the file says the same when written again; CheckSpec.complete checks that it is one.
    value: Any = None
    path: str | None = None
    abs_tolerance: Any = 0
    rel_tolerance: Any = 0
    parts: list[str] = Field(default_factory=list)
    add: list[str] = Field(default_factory=list)
    subtract: list[str] = Field(default_factory=list)
    total: str | None = None
    key: str | list[str] | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] | None = None
    # Policy checks: the kinds of personal data or secret to look for (every kind when left out),
    # and values that may appear.
    kinds: list[str] | None = None
    allow: list[str] = Field(default_factory=list)
    # How this check's verdicts over a case's attempts combine, when the suite repeats its cases.
    combine: CombineRule | None = None
    # What it grades in a conversation case: each turn's reply, or the whole conversation once.
    scope: CheckScope = "turn"

    @model_validator(mode="after")
    def complete(self) -> CheckSpec:
        if self.scope == "conversation" and self.type in JUDGES:
            raise ValueError(
                f"a {self.type} judge grades each turn's reply; scope: conversation is for deterministic checks"
            )
        if self.scope == "conversation" and self.type in CALL_CHECKS:
            raise ValueError(f"a {self.type} check measures one target call; scope: conversation does not apply to it")
        if self.type in ("matches", "not_matches") and not self.pattern:
            raise ValueError(f"a {self.type} check needs a pattern")
        if self.type in TEXT_CHECKS and not self.values and not self.expectation:
            raise ValueError(f"a {self.type} check needs values or an expectation key")
        if self.type == "similarity" and self.min_similarity is None:
            raise ValueError("a similarity check needs min_similarity, from 0 to 1")
        if self.type == "json_schema":
            if self.json_schema is None:
                raise ValueError("a json_schema check needs a schema")
            check_schema(self.json_schema)
        if self.type == "length":
            if self.min_length is None and self.max_length is None:
                raise ValueError("a length check needs min_length, max_length or both")
            if self.min_length is not None and self.max_length is not None and self.min_length > self.max_length:
                raise ValueError("min_length is more than max_length")
        if self.type == "latency" and self.max_ms is None:
            raise ValueError("a latency check needs max_ms")
        if self.type == "cost" and self.max_usd is None and self.max_tokens is None:
            raise ValueError("a cost check needs max_usd, max_tokens or both")
        if self.type in NUMERIC_CHECKS:
            self._numeric_options()
        if self.type in ("no_prohibited_phrases", "resists_injection"):
            if not self.values and not self.expectation:
                what = "phrases" if self.type == "no_prohibited_phrases" else "markers"
                raise ValueError(f"a {self.type} check needs values (its {what}) or an expectation key")
            _policy.check_values(self.type, self.values)
        if self.type in ("no_personal_data", "no_secrets"):
            _policy.data_options(check=self.type, kinds=self.kinds, allow=self.allow)
        return self

    def _numeric_options(self) -> Any:
        """The options of a numeric or table check, read as the check reads them, so a bad one
        is refused when the suite loads."""
        tolerance = {"abs_tolerance": self.abs_tolerance, "rel_tolerance": self.rel_tolerance}
        if self.type == "number_equals":
            return _numeric.number_options(
                value=self.value, expectation=self.expectation, path=self.path, pattern=self.pattern, **tolerance
            )
        if self.type == "table_equals":
            return _numeric.table_options(
                rows=self.rows,
                expectation=self.expectation,
                key=self.key,
                columns=self.columns,
                path=self.path,
                case_sensitive=self.case_sensitive,
                **tolerance,
            )
        sums = self.type == "sums_to"
        return _numeric.balance_options(
            check=self.type,
            add_option="parts" if sums else "add",
            add=self.parts if sums else self.add,
            subtract=() if sums else self.subtract,
            total=self.total,
            **tolerance,
        )

    @property
    def label(self) -> str:
        return self.name or (f"{self.type}_judge" if self.type in JUDGES else self.type)

    @property
    def security(self) -> bool:
        """Whether this is a security check, whose attempts combine by ``all`` unless it says otherwise."""
        return self.type in SECURITY_CHECKS


class MlflowConfig(_Model):
    tracking_uri: str | None = None
    experiment: str | None = None


class LimitsConfig(_Model):
    """When the run stops before its last case: after ``max_seconds`` of wall time, or once its
    judges have used ``max_judge_tokens`` tokens (``RunLimits``)."""

    max_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_judge_tokens: int | None = Field(default=None, ge=1)


class Suite(_Model):
    """The YAML file. Paths are relative to the file itself."""

    data: str
    checks: list[CheckSpec] = Field(min_length=1)
    target: TargetRef | None = None
    judge: JudgeConfig | None = None
    concurrency: int = Field(default=4, ge=1, le=64)
    batch: int = Field(default=25, ge=1, le=500)
    output: str = "results"
    input_key: str = "message"
    mlflow: MlflowConfig = Field(default_factory=MlflowConfig)
    # Attempts of each case, and the rule a check's attempts combine by when it names none and is
    # not a security check (martian.evals.repeats).
    repeats: int = Field(default=1, ge=1, le=MAX_ATTEMPTS)
    combine: CombineRule = "all"
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @model_validator(mode="after")
    def consistent(self) -> Suite:
        labels = [check.label for check in self.checks]
        if len(set(labels)) != len(labels):
            raise ValueError("check names must be unique; give repeated check types a name")
        kinds = [check.type for check in self.checks if check.type in JUDGES]
        if len(set(kinds)) != len(kinds):
            raise ValueError(f"use at most one check of each judge type ({', '.join(JUDGES)})")
        if kinds and self.judge is None:
            raise ValueError("LLM judge checks need a judge.model")
        if self.limits.max_judge_tokens is not None and not kinds:
            raise ValueError("limits.max_judge_tokens counts the tokens of LLM judge checks, and the suite has none")
        calls = [check.label for check in self.checks if check.type in CALL_CHECKS]
        if calls and self.target is None:
            raise ValueError(
                f"{', '.join(calls)} measures the target's call, and a suite without a target grades stored "
                "outputs, which no call produced; name a target or remove the check"
            )
        costs = [check.label for check in self.checks if check.type == "cost"]
        if costs and self.target is not None and self.target.url is not None:
            raise ValueError(
                f"{', '.join(costs)} reads the token usage and cost the target's MLflow tracing records, and a "
                "URL target's call records neither; use a function target or remove the check"
            )
        return self

    def repeat_plan(self) -> dict[str, Any]:
        """The attempts of each case, the suite's rule, and each check's rule and where it came from."""
        checks = [(check.label, check.combine, check.security) for check in self.checks]
        return plan(self.repeats, self.combine, checks)


def load_suite(path: str | Path) -> tuple[Suite, Path]:
    file = Path(path).resolve()
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"could not read the suite file: {type(error).__name__}") from None
    return parse_suite(text), file.parent


def parse_suite(text: str) -> Suite:
    """A suite file's text, validated. Paths in it stay as written, relative to the file."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"could not read the suite file: {type(error).__name__}") from None
    if not isinstance(raw, dict):
        raise ConfigError("the suite file must be a YAML mapping")
    try:
        return Suite.model_validate(raw)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'suite'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid suite file: {problems}") from None


def load_cases(path: str | Path, *, input_key: str = "message") -> list[dict[str, Any]]:
    """Read cases from JSONL or CSV into MLflow rows with a stable ``id``.

    JSONL: one object per line with ``inputs`` (an object) or ``input`` (a string), and optional
    ``id``, ``outputs``/``output``, ``expectations`` and ``group`` (a string naming the group of
    related cases it belongs to, which the summary's intervals allow for). A conversation case has
    ``turns`` instead of ``inputs``: a list of turns, each with ``inputs`` or ``input`` and optional
    ``expectations`` and ``checks`` (the names of the turn checks that grade it).
    CSV: columns ``input``, optional ``id`` and ``output``; every other non-empty column becomes
    an expectation of that name.
    """
    file = Path(path)
    suffix = file.suffix.lower()
    if suffix not in (".jsonl", ".csv"):
        raise ConfigError("the data file must be .jsonl or .csv")
    try:
        text = file.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ConfigError(f"could not read the data file: {type(error).__name__}") from None
    records: list[dict[str, Any]] = []
    if suffix == ".jsonl":
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                raise ConfigError(f"data line {number} is not valid JSON") from None
            if not isinstance(record, dict):
                raise ConfigError(f"data line {number} must be a JSON object")
            records.append(record)
    else:
        for record in csv.DictReader(text.splitlines()):
            known = {key: record.get(key) for key in ("id", "input", "output")}
            expectations = {k: v for k, v in record.items() if k not in known and k and v not in (None, "")}
            records.append({**{k: v for k, v in known.items() if v not in (None, "")}, "expectations": expectations})
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, record in enumerate(records, 1):
        identifier = str(record.get("id") or position)
        turns = _turns(record, identifier, input_key) if "turns" in record else None
        inputs = record.get("inputs")
        if inputs is None and isinstance(record.get("input"), str):
            inputs = {input_key: record["input"]}
        if turns is not None:
            inputs = {}
        elif not isinstance(inputs, dict) or not inputs:
            raise ConfigError(f"case {position} needs `inputs` (an object) or `input` (a string)")
        if identifier in seen:
            raise ConfigError(f"case id {identifier!r} appears more than once")
        seen.add(identifier)
        expectations = record.get("expectations") or {}
        if not isinstance(expectations, dict):
            raise ConfigError(f"case {identifier}: `expectations` must be an object")
        case: dict[str, Any] = {"id": identifier, "inputs": inputs, "expectations": dict(expectations)}
        if turns is not None:
            case["turns"] = turns
        if "group" in record:
            if not isinstance(record["group"], str) or not record["group"].strip():
                raise ConfigError(f"case {identifier}: `group` must be a non-empty string")
            case["group"] = record["group"]
        outputs = record.get("outputs", record.get("output"))
        if outputs is not None:
            case["outputs"] = outputs
        cases.append(case)
    if not cases:
        raise ConfigError("the data file has no cases")
    return cases


def _turns(record: dict[str, Any], identifier: str, input_key: str) -> list[dict[str, Any]]:
    """A conversation case's turns, each with its ``inputs``, ``expectations`` and, when it names
    them, ``checks``."""
    given = record["turns"]
    if not isinstance(given, list) or not given:
        raise ConfigError(f"case {identifier}: `turns` must be a list of at least one turn")
    if len(given) > MAX_TURNS:
        raise ConfigError(f"case {identifier}: a conversation has at most {MAX_TURNS} turns")
    if given_inputs := sorted({"input", "inputs"} & set(record)):
        raise ConfigError(f"case {identifier}: a conversation gives its input in each turn; remove {given_inputs[0]}")
    if {"output", "outputs"} & set(record):
        raise ConfigError(f"case {identifier}: a conversation runs against a target, so it has no stored output")
    turns: list[dict[str, Any]] = []
    for number, turn in enumerate(given, 1):
        where = f"case {identifier}, turn {number}"
        if not isinstance(turn, dict):
            raise ConfigError(f"{where} must be a JSON object")
        inputs = turn.get("inputs")
        if inputs is None and isinstance(turn.get("input"), str):
            inputs = {input_key: turn["input"]}
        if not isinstance(inputs, dict) or not inputs:
            raise ConfigError(f"{where} needs `inputs` (an object) or `input` (a string)")
        if reserved := sorted(set(inputs) & set(CONVERSATION_INPUTS)):
            raise ConfigError(
                f"{where}: an input cannot be named {', '.join(reserved)}; each turn is given the "
                "conversation's session_id and history"
            )
        expectations = turn.get("expectations") or {}
        if not isinstance(expectations, dict):
            raise ConfigError(f"{where}: `expectations` must be an object")
        found: dict[str, Any] = {"inputs": inputs, "expectations": dict(expectations)}
        if "checks" in turn:
            checks = turn["checks"]
            if not isinstance(checks, list) or not all(isinstance(label, str) and label for label in checks):
                raise ConfigError(f"{where}: `checks` must be a list of check names")
            found["checks"] = list(checks)
        turns.append(found)
    return turns


def _expand(text: str, setting: str, value_of: Callable[[str], str | None], source: str) -> str:
    """Replace each ${NAME} in ``text`` with ``value_of(NAME)``. A name without a value is a
    ``ConfigError`` that names the setting and ``source``, never a value."""

    def value(match: re.Match[str]) -> str:
        found = value_of(match[1])
        if not found:
            raise ConfigError(f"{setting} needs {source} {match[1]}")
        return found

    return _ENV_REFERENCE.sub(value, text)


def _from_environment(text: str, setting: str) -> str:
    """Replace ${NAME} with the environment's value. The suite file itself holds no secrets."""
    return _expand(text, setting, os.environ.get, "the environment variable")


def _status_reason(response: httpx.Response) -> str:
    """Why a target answered with an error status, naming the setting to check. The response
    text is read only to tell an unknown model from a wrong address; it is never repeated."""
    status = response.status_code
    reason = f"the target returned HTTP {status}"
    if status in (400, 403, 404, 422) and names_unknown_model(response.text[:2000]):
        return f"{reason}; check the model named in target.body"
    if status in (401, 403):
        return f"{reason}; check the credentials in target.headers"
    if status in (404, 405):
        return f"{reason}; check target.url"
    if status in (400, 422):
        return f"{reason}; check target.body"
    return reason


def _case_values(inputs: dict[str, Any], input_key: str) -> Callable[[str], Any]:
    """What each body placeholder of one case stands for: ``{{input}}`` is the input named
    ``input_key``, ``{{inputs.NAME}}`` the input named NAME, and ``{{uuid}}`` a new UUID."""

    def lookup(name: str) -> Any:
        if name == "uuid":
            return str(uuid.uuid4())
        key = input_key if name == "input" else name.removeprefix("inputs.")
        if key not in inputs:
            raise TargetCallError(f"the case has no input named {key!r}")
        return inputs[key]

    return lookup


def request_body(
    template: Any,
    inputs: dict[str, Any],
    input_key: str,
    *,
    prompt: str | None = None,
    conversation: tuple[str, list[dict[str, str]]] | None = None,
) -> Any:
    """One request's body from a URL target's ``template``: ``{{input}}``, ``{{inputs.NAME}}`` and
    ``{{uuid}}`` as :func:`_case_values` gives them, and the conversation's ``{{session_id}}``,
    ``{{history}}`` (the earlier turns as messages) and ``{{messages}}`` (the history and this
    turn's user message). ``conversation`` is the session id and history of a conversation turn;
    without one, the request is a conversation of one turn: a new session id, no history.
    ``prompt``, when given, is what ``{{input}}`` and the user message are, as the evals core's
    worker sends a case's prompt; otherwise the user message is the input under ``input_key``."""
    case = _case_values(inputs, input_key)
    said = prompt if prompt is not None else user_text(inputs, input_key)
    session_id, history = conversation if conversation is not None else (str(uuid.uuid4()), [])

    def lookup(name: str) -> Any:
        if name == "input" and prompt is not None:
            return prompt
        if name == SESSION_ID:
            return session_id
        if name == HISTORY:
            return list(history)
        if name == MESSAGES:
            return [*history, user(said)]
        return case(name)

    return _fill(template, lookup)


def _fill(template: Any, lookup: Callable[[str], Any]) -> Any:
    """One case's request body, each placeholder replaced by ``lookup(name)``. Case text is
    inserted as data and never expanded itself."""
    if isinstance(template, dict):
        return {key: _fill(item, lookup) for key, item in template.items()}
    if isinstance(template, list):
        return [_fill(item, lookup) for item in template]
    if not isinstance(template, str):
        return template
    whole = _PLACEHOLDER.fullmatch(template)
    if whole:  # keeps a number or an object as it is, rather than its text
        return lookup(whole[1])
    return _PLACEHOLDER.sub(
        lambda match: found if isinstance(found := lookup(match[1]), str) else json.dumps(found), template
    )


def _unreachable(error: httpx.HTTPError) -> TargetCallError:
    return TargetCallError(f"the target could not be reached: {type(error).__name__}; check target.url")


def _reply(response: httpx.Response, steps: list[str]) -> Any:
    """The output in a target's response: its text, or the value at ``response_path`` (``steps``).
    An error status or a missing value is a ``TargetCallError`` naming the setting to check."""
    if not response.is_success:
        raise TargetCallError(_status_reason(response))
    if not steps:
        return response.text
    try:
        reply = response.json()
    except ValueError:
        raise TargetCallError("the target's response is not JSON, so response_path cannot be read") from None
    for step in steps:
        if isinstance(reply, dict) and step in reply:
            reply = reply[step]
        elif isinstance(reply, list) and step.isascii() and step.isdigit() and int(step) < len(reply):
            reply = reply[int(step)]
        else:
            raise TargetCallError("the target's response has no value at response_path")
    if reply is None:
        raise TargetCallError("the target's response has no value at response_path")
    return reply


TIMED_OUT = "the target did not answer within target.timeout_seconds"
NOT_RUN = "the run stopped before this case"
CLIENT_SLACK_SECONDS = 1.0
"""How much longer than ``timeout_seconds`` a URL target's call may take in all: its client's
timeout bounds each step of the call, so a server that sends its reply a little at a time could
otherwise outlast it, and the client's own errors, which name the setting to check, end it first."""


def _http_target(reference: TargetRef, input_key: str) -> Callable[..., Any]:
    """Call the endpoint once per case: no retry, no redirect, no proxy from the environment.

    Errors are ``TargetCallError``s that name what went wrong but never the address, the headers or
    the response body. The returned function has a ``converse(inputs, session_id=..., history=...)``
    attribute that sends one turn of a conversation (:func:`request_body`). The evals core's worker
    calls an HTTP target with the same pieces (:mod:`martian.evals.worker.targets`).
    """
    url = _from_environment(reference.url or "", "target.url")
    try:
        _check_url(url)
    except ValueError as error:
        raise ConfigError(str(error)) from None
    headers = {name: _from_environment(value, f"target.headers.{name}") for name, value in reference.headers.items()}
    steps = _response_steps(reference.response_path, "target.response_path")

    def send(inputs: dict[str, Any], conversation: tuple[str, list[dict[str, str]]] | None = None) -> Any:
        body = request_body(reference.body, inputs, input_key, conversation=conversation)
        try:
            with httpx.Client(timeout=reference.timeout_seconds, follow_redirects=False, trust_env=False) as client:
                response = client.post(url, headers=headers, json=body)
        except httpx.ReadTimeout:
            raise TargetCallError(TIMED_OUT) from None
        except httpx.HTTPError as error:
            raise _unreachable(error) from None
        return _reply(response, steps)

    def call(**inputs: Any) -> Any:
        return send(inputs)

    def converse(inputs: dict[str, Any], *, session_id: str, history: list[dict[str, str]]) -> Any:
        return send(inputs, (session_id, history))

    call.converse = converse  # type: ignore[attr-defined]
    return call


def _function_target(function: Callable[..., Any], timeout_seconds: float) -> Callable[..., Any]:
    """Call the target function on a thread of its own and wait for it at most ``timeout_seconds``.

    A function that has not returned by then is the case's error (``TIMED_OUT``), and the run goes
    on. Python cannot stop a thread, so the function keeps its thread, a daemon thread that ends
    with the process, until it returns. The thread runs in a copy of the caller's context, so the
    spans the function records with MLflow tracing stay in the case's trace. The wrapper keeps the
    function's signature, which MLflow's evaluation harness reads."""

    @functools.wraps(function)
    def call(*args: Any, **kwargs: Any) -> Any:
        context = contextvars.copy_context()
        outcome: dict[str, Any] = {}
        ended = threading.Event()

        def run() -> None:
            try:
                outcome["value"] = context.run(function, *args, **kwargs)
            except BaseException as error:  # noqa: BLE001 - raised again in the caller's thread
                outcome["error"] = error
            finally:
                ended.set()

        threading.Thread(target=run, name="martian-evals-target", daemon=True).start()
        if not ended.wait(timeout_seconds):
            raise TargetCallError(TIMED_OUT)
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    return call


class _Calls:
    """The target as MLflow's evaluation harness calls it, once per case. Once the run must stop
    (``stopping()``) it calls the target no more, and the case is ``NOT_RUN``. It keeps why each
    call failed with a ``TargetCallError``, the SDK's own words, by the inputs it was given, since
    the harness reports only that a call failed; two cases with identical inputs send identical
    requests, so sharing is safe. A function target's own exception may hold anything, so it is
    not kept."""

    def __init__(self, target: Callable[..., Any], stopping: Callable[[], bool]) -> None:
        self._reasons: dict[str, list[str]] = {}
        self._lock = threading.Lock()

        @functools.wraps(target)
        def call(**inputs: Any) -> Any:
            try:
                if stopping():
                    raise TargetCallError(NOT_RUN)
                return target(**inputs)
            except TargetCallError as error:
                with self._lock:
                    self._reasons.setdefault(self._key(inputs), []).append(str(error))
                raise

        self.predict_fn = call

    @staticmethod
    def _key(inputs: dict[str, Any]) -> str:
        return json.dumps(inputs, sort_keys=True, default=str)

    def failure_reason(self, inputs: dict[str, Any]) -> str | None:
        """Why one call with ``inputs`` failed, in the SDK's words, or None."""
        with self._lock:
            found = self._reasons.get(self._key(inputs))
            return found.pop(0) if found else None


def _conversation_parameters(function: Callable[..., Any], name: str) -> tuple[str, ...]:
    """Which of ``history`` and ``session_id`` a function target takes, so each turn of a
    conversation gives it those. One that takes neither cannot keep a conversation."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        raise ConfigError(f"could not read the parameters of the target function {name!r}") from None
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return CONVERSATION_INPUTS
    named = {
        parameter.name
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    if accepted := tuple(found for found in CONVERSATION_INPUTS if found in named):
        return accepted
    raise ConfigError(
        f"target.function: the data has conversation cases, and {name!r} cannot keep a conversation; "
        "give it a history or session_id parameter, or both"
    )


def _conversation_inputs(reference: TargetRef, target: Callable[..., Any]) -> tuple[str, ...]:
    """Which of ``history`` and ``session_id`` each turn of a conversation gives a function target
    (:func:`_conversation_parameters`); none for a URL target, whose body holds the conversation.
    A target that cannot keep a conversation is a ``ConfigError``, raised before the run starts."""
    if getattr(target, "converse", None) is None:
        return _conversation_parameters(target, reference.function)
    if not set(_placeholders(reference.body)) & set(PLACEHOLDERS):
        raise ConfigError(
            "target.body: the data has conversation cases, and a body without {{session_id}}, {{history}} "
            "or {{messages}} cannot keep a conversation; use one of them"
        )
    return ()


def _conversation_target(
    target: Callable[..., Any], accepted: tuple[str, ...], stopping: Callable[[], bool]
) -> tuple[Callable[..., Any], dict[str, str]]:
    """What MLflow's evaluation harness calls for each turn of a conversation, and why each failed
    call failed with a ``TargetCallError``, the SDK's own words (such as ``TIMED_OUT``), by session
    id. A turn's row gives its inputs and the conversation's ``session_id`` and ``history``; a
    function target is given those of them it takes (``accepted``). The call is traced under the
    session id, so the turns of one conversation group into one session. Once the run must stop
    (``stopping()``) it sends no more turns, and the turn is ``NOT_RUN``."""
    import mlflow

    reasons: dict[str, str] = {}
    lock = threading.Lock()
    converse: Callable[..., Any] | None = getattr(target, "converse", None)

    def turn(**inputs: Any) -> Any:
        session_id, history = inputs.pop(SESSION_ID), inputs.pop(HISTORY)
        mlflow.update_current_trace(session_id=session_id)
        try:
            if stopping():
                raise TargetCallError(NOT_RUN)
            if converse is None:
                given = {SESSION_ID: session_id, HISTORY: history}
                return target(**inputs, **{name: given[name] for name in accepted})
            return converse(inputs, session_id=session_id, history=history)
        except TargetCallError as error:
            with lock:
                reasons[session_id] = str(error)
            raise

    return mlflow.trace(turn, name=getattr(target, "__name__", "target")), reasons


def _load_target(reference: TargetRef, base: Path, input_key: str = "message") -> Callable[..., Any]:
    """The target's call, which never takes much longer than ``timeout_seconds``, whatever it is."""
    if reference.url is not None:
        call = _http_target(reference, input_key)
        limit = reference.timeout_seconds + CLIENT_SLACK_SECONDS
        timed = _function_target(call, limit)
        # A conversation's turn has the same time limit as a case's call.
        timed.converse = _function_target(call.converse, limit)  # type: ignore[attr-defined]
        return timed
    file = Path(reference.file or "")
    file = file if file.is_absolute() else (base / file)
    spec = importlib.util.spec_from_file_location("martian_evals_target", file)
    if spec is None or spec.loader is None or not file.is_file():
        raise ConfigError("could not load the target file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # noqa: BLE001 - the customer's module may raise anything on import
        raise ConfigError(f"the target file failed to import: {type(error).__name__}") from None
    function: Callable[..., Any] | None = getattr(module, reference.function, None)
    if function is None or not callable(function):
        raise ConfigError(f"the target file has no function {reference.function!r}")
    return _function_target(function, reference.timeout_seconds)


def _build_scorers(suite: Suite) -> list[Any]:
    from martian.evals import grounding, judges

    built = []
    judge_headers = {
        name: _from_environment(value, f"judge.headers.{name}")
        for name, value in (suite.judge.headers if suite.judge else {}).items()
    }
    factories: dict[str, Callable[..., Any]] = {
        "rubric": judges.response_rubric,
        "guidelines": judges.response_guidelines,
        "conditions": judges.response_conditions,
        "groundedness": grounding.groundedness,
        "context_relevance": grounding.context_relevance,
        "context_sufficiency": grounding.context_sufficiency,
    }
    for check in suite.checks:
        if check.type in JUDGES:
            assert suite.judge is not None
            factory = factories[check.type]
            built.append(
                factory(
                    model=suite.judge.model,
                    name=check.label,
                    input_key=suite.input_key,
                    inference_params=dict(suite.judge.inference_params) or None,
                    max_retries=suite.judge.max_retries,
                    rate_limit_wait_seconds=suite.judge.rate_limit_wait_seconds,
                    headers=judge_headers,
                )
            )
        else:
            built.append(_build_check(check))
    return built


def _build_check(check: CheckSpec) -> Any:
    """The deterministic check a suite file's entry names. ``CheckSpec.complete`` has made sure the
    options each type needs are there."""
    from martian.evals import checks as deterministic

    name = check.label
    text: dict[str, Any] = {"expectation": check.expectation, "name": name, "case_sensitive": check.case_sensitive}
    match check.type:
        case "contains":
            return deterministic.contains(*check.values, any_of=check.any_of, **text)
        case "not_contains":
            return deterministic.not_contains(*check.values, **text)
        case "contains_any":
            return deterministic.contains_any(*check.values, **text)
        case "contains_all":
            return deterministic.contains_all(*check.values, **text)
        case "equals":
            return deterministic.equals(*check.values, **text)
        case "starts_with":
            return deterministic.starts_with(*check.values, **text)
        case "ends_with":
            return deterministic.ends_with(*check.values, **text)
        case "similarity":
            assert check.min_similarity is not None
            return deterministic.similarity(*check.values, min_similarity=check.min_similarity, **text)
        case "matches":
            return deterministic.matches(check.pattern or "", name=name)
        case "not_matches":
            return deterministic.not_matches(check.pattern or "", name=name)
        case "is_json":
            return deterministic.is_json(required_keys=check.required_keys, name=name)
        case "json_schema":
            assert check.json_schema is not None
            return deterministic.json_schema(check.json_schema, name=name)
        case "length":
            return deterministic.length(min_length=check.min_length, max_length=check.max_length, name=name)
        case "latency":
            assert check.max_ms is not None
            return deterministic.latency(max_ms=check.max_ms, name=name)
        case "cost":
            return deterministic.cost(max_usd=check.max_usd, max_tokens=check.max_tokens, name=name)
    numbers: dict[str, Any] = {"abs_tolerance": check.abs_tolerance, "rel_tolerance": check.rel_tolerance, "name": name}
    match check.type:
        case "number_equals":
            return deterministic.number_equals(
                check.value, expectation=check.expectation, path=check.path, pattern=check.pattern, **numbers
            )
        case "sums_to":
            assert check.total is not None
            return deterministic.sums_to(parts=check.parts, total=check.total, **numbers)
        case "reconciles":
            assert check.total is not None
            return deterministic.reconciles(add=check.add, subtract=check.subtract, total=check.total, **numbers)
        case "table_equals":
            assert check.key is not None
            return deterministic.table_equals(
                check.rows,
                key=check.key,
                expectation=check.expectation,
                columns=check.columns,
                path=check.path,
                case_sensitive=check.case_sensitive,
                **numbers,
            )
    match check.type:
        case "no_prohibited_phrases":
            return deterministic.no_prohibited_phrases(*check.values, expectation=check.expectation, name=name)
        case "no_personal_data":
            return deterministic.no_personal_data(kinds=check.kinds, allow=check.allow, name=name)
        case "no_secrets":
            return deterministic.no_secrets(kinds=check.kinds, allow=check.allow, name=name)
        case "resists_injection":
            return deterministic.resists_injection(*check.values, expectation=check.expectation, name=name)
        case "no_refusal":
            return deterministic.no_refusal(expectation=check.expectation, name=name)
    raise AssertionError(f"{check.type} is not a deterministic check")


def _with_suite_criteria(case: dict[str, Any], suite: Suite) -> dict[str, Any]:
    """Fill in criteria given once in the suite file for rows that do not carry their own."""
    expectations = dict(case["expectations"])
    for check in suite.checks:
        for key in ("rubric", "guidelines", "conditions"):
            value = getattr(check, key)
            if check.type == key and value is not None and key not in expectations:
                expectations[key] = value
    return expectations


def _missing(value: Any) -> bool:
    return value is None or value != value  # NaN


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _suite_fingerprint(suite: Suite, base: Path) -> str:
    """Everything in the suite a verdict depends on: the checks and their criteria, the judge model
    and its parameters, the target, and the SDK version that holds the judge prompts.

    Settings that change how a run is carried out but not what it grades (concurrency, batch size,
    output folder, retries and the rate-limit wait, timeout, the judge's headers, MLflow location)
    are left out, so changing them keeps earlier results. The judge's headers, like its key and
    address in the environment, say how its endpoint is reached. A function target counts by the
    contents of its file; modules that file imports and the service behind a URL target cannot be
    seen from here.

    With repeats, the attempts per case and the rule each check's attempts combine by count too.
    With one attempt every rule gives the same verdict, so neither counts and the fingerprint is
    the one a suite without repeats has. A check's scope counts only when it is ``conversation``,
    so a suite that names none keeps the fingerprint it had before scopes existed.
    """
    target: dict[str, Any] | None = None
    if suite.target is not None:
        target = suite.target.model_dump(mode="json", exclude={"timeout_seconds"})
        if suite.target.file is not None:
            file = Path(suite.target.file)
            file = file if file.is_absolute() else base / file
            target["file_sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
    definition: dict[str, Any] = {
        "sdk": __version__,
        "checks": [
            check.model_dump(mode="json", exclude={"combine"} | ({"scope"} if check.scope == "turn" else set()))
            for check in suite.checks
        ],
        "judge": suite.judge.model_dump(mode="json", exclude={"max_retries", "rate_limit_wait_seconds", "headers"})
        if suite.judge
        else None,
        "input_key": suite.input_key,
        "target": target,
    }
    if suite.repeats > 1:
        repeats = suite.repeat_plan()
        definition["repeats"] = {
            "attempts": repeats["attempts"],
            "combine": {label: rule["combine"] for label, rule in repeats["checks"].items()},
        }
    return _digest(definition)


def _case_fingerprint(suite_fingerprint: str, case: dict[str, Any]) -> str:
    """What a case's verdict depends on besides the suite: its inputs, expectations and stored
    output, and a conversation's turns."""
    found = {
        "suite": suite_fingerprint,
        "inputs": case["inputs"],
        "expectations": case["expectations"],
        "outputs": case.get("outputs"),
    }
    if "turns" in case:
        found["turns"] = case["turns"]
    return _digest(found)


class Results:
    """``results.jsonl`` in the output folder: one record per case, the latest record wins."""

    def __init__(self, folder: Path, *, resume: bool) -> None:
        self.folder = folder
        self.path = folder / "results.jsonl"
        folder.mkdir(parents=True, exist_ok=True)
        if not resume and self.path.exists():
            self.path.unlink()

    def records(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    found[record["id"]] = record
        return found

    def append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _pass_rate(verdicts: list[tuple[str, bool | None]], groups: dict[str, str]) -> dict[str, Any]:
    """The share of cases with a verdict that passed, and its 95% interval (``martian.evals.stats``),
    allowing for the cases' groups when the data names any. ``verdicts`` holds each case's id and
    verdict; a case without a verdict is left out."""
    graded = [(case_id, passed) for case_id, passed in verdicts if passed is not None]
    if not graded:
        return {"pass_rate": None, "confidence_95": None}
    outcomes = [passed for _, passed in graded]
    labels = [groups.get(case_id) for case_id, _ in graded] if groups else None
    interval = proportion_interval(outcomes, labels)
    assert interval is not None  # there is at least one verdict
    return {"pass_rate": sum(outcomes) / len(outcomes), "confidence_95": [interval[0], interval[1]]}


def _counts(verdicts: list[bool | None]) -> dict[str, int]:
    return {
        "passed": sum(passed is True for passed in verdicts),
        "failed": sum(passed is False for passed in verdicts),
        "errors": sum(passed is None for passed in verdicts),
    }


def _false_refusals(
    checks: list[CheckSpec],
    records: list[dict[str, Any]],
    expectations: dict[str, dict[str, Any]],
    turns: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """How many benign cases the suite's no_refusal checks found refused, for a suite whose checks
    a refusal passes. A case is benign for a check without an expectation key, or when its key is
    true; one with no verdict from an applicable check counts under errors, not in the rate.

    A conversation case (its ``turns`` by case id) counts once too. For a turn check its benign
    flags are those of the turns the check grades, and it is benign when one of them is; its
    verdict is the check's on the conversation, which fails when the check failed a benign turn.
    A check that grades nothing in a conversation does not count for it."""
    refusal = [check for check in checks if check.type == "no_refusal"]
    if not refusal and not any(check.type in REFUSAL_PASSING for check in checks):
        return None
    turn_labels = [check.label for check in checks if check.scope == "turn"]
    benign = refused = errors = 0
    for record in records:
        verdicts: list[bool | None] = []
        talk = (turns or {}).get(record["id"])
        for check in refusal:
            if check.label not in record["checks"]:
                continue
            if check.expectation is not None:
                if talk is not None and check.scope == "turn":
                    graded = graded_turns([turn.get("checks") for turn in talk], turn_labels)[check.label]
                    flags = [talk[number - 1]["expectations"].get(check.expectation) for number in graded]
                else:
                    flags = [expectations[record["id"]].get(check.expectation)]
                try:
                    if not any([_policy.benign_flag(flag, check.expectation) for flag in flags]):
                        continue
                except ValueError:  # the check could not tell, and has no verdict either
                    verdicts.append(None)
                    continue
            verdicts.append(record["checks"][check.label]["passed"])
        if None in verdicts:
            errors += 1
        elif verdicts:
            benign += 1
            refused += False in verdicts
    return {
        "checks": [check.label for check in refusal],
        "benign": benign,
        "refused": refused,
        "errors": errors,
        "rate": round(refused / benign, 4) if benign else None,
    }


def false_refusal_line(found: dict[str, Any]) -> str:
    """The false-refusal rate as one line, printed under the results table."""
    if not found["checks"]:
        return (
            "False refusals: not measured. A reply that refuses everything passes these checks; "
            "add a no_refusal check and mark the benign cases."
        )
    if not found["benign"] and not found["errors"]:
        return "False refusals: not measured. No case is marked benign."
    line = "False refusals: not measured"
    if found["benign"]:
        case = "case" if found["benign"] == 1 else "cases"
        line = f"False refusals: {found['refused']} of {found['benign']} benign {case} refused ({found['rate']:.1%})"
    if found["errors"]:
        line += f"; {found['errors']} benign {'case' if found['errors'] == 1 else 'cases'} without a verdict"
    return line + "."


def _summary(
    records: list[dict[str, Any]],
    labels: list[str],
    seconds: float,
    invalidated: int,
    groups: dict[str, str],
    repeats: dict[str, Any] | None = None,
    false_refusals: dict[str, Any] | None = None,
    scopes: dict[str, CheckScope] | None = None,
    *,
    not_run: int = 0,
    stopped: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The run's counts and pass rates. A record is one case: with repeats its verdicts are its
    attempts combined, so the pass rates and their intervals count cases, never attempts. With
    ``repeats`` (the suite's :meth:`Suite.repeat_plan`, when it runs each case more than once) the
    summary also states the plan and counts the attempts, the target and check errors are counted
    over attempts, where they happened, and ``reliability`` is the share of cases that passed every
    attempt, with its interval and the flaky cases (:func:`martian.evals.repeats.reliability`).
    ``false_refusals`` (:func:`_false_refusals`, for a suite with policy checks) counts cases by
    their combined verdicts too. ``not_run`` is how many cases a run that ``stopped`` at a limit
    did not reach (with repeats, a case whose attempts did not all run, and a conversation whose
    turns did not all run): ``records`` holds none of them, so no count, rate, reliability or
    false-refusal figure includes them.

    A conversation case counts once too, by its conversation's verdict. ``scopes`` (each check's
    scope, given when the run has conversation cases) adds ``conversations``: the conversation
    cases' counts and their turns' counts, over attempts with repeats, each check's ``scope``, and
    a turn check's own ``turns`` counts. A check's counts leave out the conversations it grades
    nothing in, and a check that gave a turn no verdict is counted under that turn's reason."""
    repeated = repeats is not None
    units = [attempt for r in records for attempt in r["attempts"]] if repeated else records
    turns = [turn for unit in units for turn in unit.get("turns", [])]
    per_check = {}
    for label in labels:
        verdicts = [(str(r["id"]), r["checks"][label]["passed"]) for r in records if label in r["checks"]]
        per_check[label] = {**_counts([passed for _, passed in verdicts]), **_pass_rate(verdicts, groups)}
        if repeated:
            tried = [unit["checks"][label]["passed"] for unit in units if label in unit["checks"]]
            per_check[label]["attempts"] = _counts(tried)
        if scopes is not None:
            per_check[label]["scope"] = scopes[label]
            if scopes[label] == "turn":
                per_check[label]["turns"] = _counts(
                    [t["checks"][label]["passed"] for t in turns if label in t["checks"]]
                )
    target_errors: dict[str, int] = {}
    # Why checks gave no verdict, such as a judge setting to fix; a target failure is counted above.
    # A conversation's turn checks are counted by the turns they graded, its conversation checks
    # by the conversation.
    check_errors: dict[str, dict[str, int]] = {}
    for unit in units:
        if unit.get("error"):
            target_errors[str(unit["error"])] = target_errors.get(str(unit["error"]), 0) + 1
            continue
        graded = unit["checks"].items()
        if "turns" in unit and scopes is not None:
            own = [(label, check) for label, check in graded if scopes[label] == "conversation"]
            graded = [*own, *((label, check) for turn in unit["turns"] for label, check in turn["checks"].items())]
        for label, check in graded:
            if check.get("passed") is None and check.get("error"):
                reasons = check_errors.setdefault(label, {})
                reasons[str(check["error"])] = reasons.get(str(check["error"]), 0) + 1
    shown = {groups[str(r["id"])] for r in records if str(r["id"]) in groups}
    summary: dict[str, Any] = {
        "cases": len(records),
        **_counts([r["passed"] for r in records]),
        **_pass_rate([(str(r["id"]), r["passed"]) for r in records], groups),
        "groups": len(shown) if shown else None,
    }
    if repeated:
        tried = [attempt["passed"] for r in records for attempt in r["attempts"]]
        each = [(str(r["id"]), [attempt["passed"] for attempt in r["attempts"]]) for r in records]
        reliable = reliability(each)
        flaky_cases = reliable.pop("flaky")
        verdicts = [(case_id, passed_every_attempt(attempts)) for case_id, attempts in each]
        summary |= {
            "repeats": repeats,
            "attempts": {"total": len(tried), **_counts(tried)},
            "reliability": {**reliable, **_pass_rate(verdicts, groups), "flaky": flaky_cases},
        }
    if scopes is not None:
        talks = [r["passed"] for r in records if "turns" in r]
        summary["conversations"] = {
            "cases": len(talks),
            **_counts(talks),
            "turns": {"total": len(turns), **_counts([turn["passed"] for turn in turns])},
        }
    return summary | {
        "checks": per_check,
        **({} if false_refusals is None else {"false_refusals": false_refusals}),
        "target_errors": target_errors,
        "check_errors": check_errors,
        "invalidated": invalidated,
        "not_run": not_run,
        "stopped": stopped,
        "seconds_this_run": round(seconds, 1),
    }


def _junit_properties(summary: dict[str, Any]) -> list[str]:
    """The run's pass rates and their 95% intervals, overall and per check, as testsuite properties,
    and with repeats the attempts per case and the rule each check's attempts combined by."""
    values: list[tuple[str, Any]] = []
    for prefix, rate in [("", summary), *((f"check.{label}.", found) for label, found in summary["checks"].items())]:
        if rate["pass_rate"] is not None:
            low, high = rate["confidence_95"]
            values += [(f"{prefix}pass_rate", rate["pass_rate"]), (f"{prefix}pass_rate_95_low", low)]
            values += [(f"{prefix}pass_rate_95_high", high)]
    if summary["groups"] is not None:
        values.append(("case_groups", summary["groups"]))
    if (repeats := summary.get("repeats")) is not None:
        values.append(("repeats", repeats["attempts"]))
        values += [(f"check.{label}.combine", rule["combine"]) for label, rule in repeats["checks"].items()]
    if not values:
        return []
    shown = [(name, value if isinstance(value, str) else f"{value:.6g}") for name, value in values]
    return [
        "  <properties>",
        *(f"    <property name={quoteattr(name)} value={quoteattr(value)}/>" for name, value in shown),
        "  </properties>",
    ]


def write_junit(
    records: list[dict[str, Any]],
    path: Path,
    *,
    suite_name: str,
    summary: dict[str, Any],
    not_run: Sequence[str] = (),
    stopped: str = "",
) -> None:
    """JUnit XML for CI systems: a failed check is a failure, a case without a verdict an error,
    and so is each case of ``not_run``, which a run that stopped did not reach, with ``stopped``.
    The testsuite's properties hold the summary's pass rates and their intervals."""
    failures = sum(r["passed"] is False for r in records)
    errors = sum(r["passed"] is None for r in records) + len(not_run)
    tests = len(records) + len(not_run)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name={quoteattr(suite_name)} tests="{tests}" failures="{failures}" errors="{errors}">',
        *_junit_properties(summary),
    ]
    for record in records:
        lines.append(f"  <testcase classname={quoteattr(suite_name)} name={quoteattr(str(record['id']))}>")
        details = [
            f"{label}: {check.get('rationale') or check.get('error') or 'no detail'}"
            for label, check in record["checks"].items()
            if check.get("passed") is not True
        ]
        message = str(record["error"]) if record.get("error") else "; ".join(details) or "no verdict"
        if record["passed"] is False:
            lines.append(f"    <failure message={quoteattr(message[:2000])}/>")
        elif record["passed"] is None:
            lines.append(f"    <error message={quoteattr(message[:2000])}/>")
        lines.append("  </testcase>")
    for case_id in not_run:
        lines.append(f"  <testcase classname={quoteattr(suite_name)} name={quoteattr(case_id)}>")
        lines.append(f"    <error message={quoteattr(f'not run: {stopped}'[:2000])}/>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _judge_tokens(assessments: Any, judges: set[str]) -> list[int | None]:
    """The tokens of each judgment of one case that has a verdict, as the provider reported them
    in its feedback (None for one that reported none). A judgment that failed reports nothing."""
    from martian.evals.limits import reported_tokens

    found: list[int | None] = []
    for assessment in assessments if isinstance(assessments, list) else []:
        feedback = assessment.get("feedback") if isinstance(assessment, dict) else None
        if assessment.get("assessment_name") not in judges or not isinstance(feedback, dict):
            continue
        if feedback.get("error") or feedback.get("value") is None:
            continue
        found.append(reported_tokens(assessment.get("metadata") or {}))
    return found


def _no_reason() -> str | None:
    return None


def _turn_row(turn: Turn) -> dict[str, Any]:
    """A conversation turn as the runner reads it: its inputs, expectations and listed checks."""
    row: dict[str, Any] = {"inputs": dict(turn.inputs), "expectations": dict(turn.expectations)}
    if turn.checks is not None:
        row["checks"] = list(turn.checks)
    return row


def run_suite(
    path: str | Path,
    *,
    output: str | Path | None = None,
    concurrency: int | None = None,
    limit: int | None = None,
    resume: bool = True,
    progress: Callable[[str], None] | None = None,
    home: str | Path | None = None,
    repeats: int | None = None,
    max_seconds: float | None = None,
    max_judge_tokens: int | None = None,
) -> dict[str, Any]:
    """Run the suite and return its summary. Writes results.jsonl, summary.json and junit.xml, and
    records the run, its case results, scores and traces in the local edition's data folder
    (``home``, else ``MARTIAN_EVALS_HOME``, else ``~/.martian/evals``), where ``martian ui``
    shows it (:mod:`martian.evals.run_record`). The returned summary also names that run, under
    ``run``; summary.json does not.

    ``max_seconds`` and ``max_judge_tokens`` replace the suite file's ``limits``. A run that
    reaches one, or that is cancelled in ``martian ui``, calls no more targets: the cases it
    finished are kept, the others have no result (``not_run``), and running it again resumes
    after them. The summary's ``stopped`` says which limit it reached. With repeats, a case is
    finished only when all of its attempts ran: one the run stopped in the middle of is not run,
    the attempts it did run are dropped (their judge tokens still count towards the limit), and
    running again runs all of its attempts.

    The suite is read through the shared model (:func:`martian.evals.suite_file.load_suite_file`),
    so a field the model has no place for is refused with the field named, never ignored.
    ``repeats`` replaces the suite file's ``repeats``: each case runs that many times, and each
    check's attempts combine by its rule (:mod:`martian.evals.repeats`).

    A conversation case is one case: ``limit`` counts it once, and each of its attempts is a whole
    conversation of its own. Its turns are sent in order, every conversation of a batch at turn
    one before any at turn two, and each turn is graded by the turn checks it names; the
    conversation checks then grade the replies (:mod:`martian.evals.conversations`).
    """
    from martian.evals.limits import Budget
    from martian.evals.local import data_folder
    from martian.evals.model import RunLimits, suite_digest
    from martian.evals.run_record import GradedCase, RunRecorder
    from martian.evals.suite_file import load_suite_file

    model, model_cases = load_suite_file(path)
    suite, base = load_suite(path)
    if repeats is not None:
        if not 1 <= repeats <= MAX_ATTEMPTS:
            raise ConfigError(f"repeats must be from 1 to {MAX_ATTEMPTS}")
        suite = suite.model_copy(update={"repeats": repeats})
        model = model.model_copy(update={"repeats": model.repeats.model_copy(update={"attempts": repeats})})
        model = model.model_copy(update={"digest": suite_digest(model)})
    repeated = suite.repeats > 1
    numbers = range(1, suite.repeats + 1)
    repeat_plan = suite.repeat_plan()
    rules: dict[str, CombineRule] = {label: rule["combine"] for label, rule in repeat_plan["checks"].items()}
    cases: list[dict[str, Any]] = [
        {
            "id": case.id,
            "inputs": dict(case.inputs),
            "expectations": dict(case.expectations),
            **({} if case.outputs is None else {"outputs": case.outputs}),
            **({"turns": [_turn_row(turn) for turn in case.turns]} if case.turns else {}),
        }
        for case in model_cases
    ]
    total = len(cases)
    if limit is not None:
        cases = cases[:limit]
    stored = [case for case in cases if "outputs" in case]
    if suite.target is None and len(stored) != len(cases):
        raise ConfigError("without a target, every case needs an `outputs` (or `output`) value to grade")
    if suite.target is not None and stored:
        raise ConfigError("the data file has stored outputs and the suite also names a target; use one or the other")
    limits = RunLimits(
        max_seconds=suite.limits.max_seconds if max_seconds is None else max_seconds,
        max_judge_tokens=suite.limits.max_judge_tokens if max_judge_tokens is None else max_judge_tokens,
    )
    judges = {check.label for check in suite.checks if check.type in JUDGES}
    if limits.max_judge_tokens is not None and not judges:
        raise ConfigError("max_judge_tokens counts the tokens of LLM judge checks, and the suite has none")
    target = _load_target(suite.target, base, suite.input_key) if suite.target is not None else None
    scorers = _build_scorers(suite)
    labels = [check.label for check in suite.checks]
    scorer_of = dict(zip(labels, scorers, strict=True))
    scopes: dict[str, CheckScope] = {check.label: check.scope for check in suite.checks}
    turn_labels = [label for label in labels if scopes[label] == "turn"]
    whole_labels = [label for label in labels if scopes[label] == "conversation"]
    talking = any("turns" in case for case in cases)
    turn_target: Callable[..., Any] | None = None
    turn_reasons: dict[str, str] = {}
    accepted: tuple[str, ...] = ()
    if talking:
        assert suite.target is not None and target is not None  # the suite file loader refuses otherwise
        accepted = _conversation_inputs(suite.target, target)

    folder = Path(output) if output is not None else base / suite.output
    # Opened before the output folder changes, so a data folder that cannot be opened stops here.
    with RunRecorder(data_folder(home), model, limits=limits) as recorder:
        results = Results(folder, resume=resume)
        suite_print = _suite_fingerprint(suite, base)
        prints = {case["id"]: _case_fingerprint(suite_print, case) for case in cases}
        positions = {case["id"]: position for position, case in enumerate(cases)}
        # An earlier verdict is reused only when it was produced by exactly this suite and case.
        finished = {i: r for i, r in results.records().items() if r["passed"] is not None and i in prints}
        done = {i for i, r in finished.items() if r.get("fingerprint") == prints[i]}
        invalidated = len(finished) - len(done)
        todo = [case for case in cases if case["id"] not in done]
        say = progress or (lambda message: None)
        each = f" of {suite.repeats} attempts each" if repeated else ""
        message = f"{len(cases)} cases{each}, {len(cases) - len(todo)} already done"
        if talking:
            talk_count = sum("turns" in case for case in cases)
            message += f"; {talk_count} {'is a conversation' if talk_count == 1 else 'are conversations'}"
        if invalidated:
            plural = "result" if invalidated == 1 else "results"
            message += f", {invalidated} earlier {plural} discarded because the suite, the data or the target changed"
        say(message)

        import mlflow

        from martian.evals._mlflow import active_experiment_id, restore_active_experiment_id
        from martian.evals.evaluate import evaluate_rows
        from martian.evals.gate import _interpret
        from martian.evals.scorers import output_text

        def graded(
            case: dict[str, Any],
            record: dict[str, Any],
            traces: list[Any] | None = None,
            turn_traces: list[list[Any]] | None = None,
        ) -> GradedCase:
            found = tuple(trace if isinstance(trace, str) else None for trace in traces or [])
            return GradedCase(
                position=positions[case["id"]],
                record=record,
                expectations=case["expectations"],
                trace=found[0] if found and not repeated else None,
                attempt_traces=found if repeated else (),
                turn_traces=tuple(
                    tuple(trace if isinstance(trace, str) else None for trace in attempt)
                    for attempt in turn_traces or []
                ),
                turn_expectations=tuple(turn["expectations"] for turn in case.get("turns", [])),
            )

        def outcome(frame: Any, position: int, checked: list[str], why: Callable[[], str | None]) -> dict[str, Any]:
            """One call's verdict, the verdicts of the checks ``checked`` that graded it, its reply and
            its target error, from its row of the frame. ``why`` says why a failed call failed."""
            response = frame["response"].iloc[position] if "response" in frame.columns else None
            state = str(frame["state"].iloc[position]).upper() if "state" in frame.columns else "OK"
            broke = _missing(response) or "ERROR" in state
            # A function target's exception may hold anything, so only the SDK's own words
            # (TargetCallError, such as NOT_RUN or TIMED_OUT) give a reason.
            reason = (why() if broke else None) or ("the target call failed" if broke else None)
            verdicts: dict[str, dict[str, Any]] = {}
            for label in checked:
                value = frame[f"{label}/value"].iloc[position] if f"{label}/value" in frame.columns else None
                rationale = (
                    frame[f"{label}/rationale"].iloc[position] if f"{label}/rationale" in frame.columns else None
                )
                detail = (
                    frame[f"{label}/error_message"].iloc[position]
                    if f"{label}/error_message" in frame.columns
                    else None
                )
                passed = None if broke else _interpret(value, None)
                verdicts[label] = {
                    "passed": passed,
                    "rationale": None if _missing(rationale) else str(rationale),
                    "error": None
                    if passed is not None
                    else (
                        "the target failed" if broke else str(detail)[:300] if not _missing(detail) else "no verdict"
                    ),
                }
            values = [verdict["passed"] for verdict in verdicts.values()]
            return {
                "passed": None if any(v is None for v in values) else all(values),
                "checks": verdicts,
                "output": None if broke else output_text(response),
                "error": reason,
            }

        lost = f"no result was recorded for this {'attempt' if repeated else 'case'}"

        def unrecorded(reason: str | None) -> dict[str, Any]:
            """An attempt whose row MLflow left out, as it does a row whose trace it could not read
            back, such as a traced function's that had not ended. The attempt has no verdict, so it
            is an error now, with the target's reason when it gave one (NOT_RUN when the run had
            stopped before it), and its case is graded again on the next run."""
            detail = "the target failed" if reason else lost
            return {
                "passed": None,
                "checks": {label: {"passed": None, "rationale": None, "error": detail} for label in labels},
                "output": None,
                "error": reason,
            }

        def converse(talks: list[dict[str, Any]]) -> dict[tuple[str, int], tuple[dict[str, Any], list[Any]]]:
            """Every attempt of each conversation case: its record and its turns' traces, by case id
            and attempt number. Turn ``n`` of every conversation runs, in one evaluation per set of
            turn checks, before any turn ``n + 1``; a conversation whose turn did not complete stops."""
            assert turn_target is not None
            by_id = {case["id"]: case for case in talks}
            # Each attempt's conversation: its session id, the messages so far, the turns sent with
            # their traces, and the number of the turn that did not complete, if one did not.
            states: dict[tuple[str, int], dict[str, Any]] = {
                (case["id"], number): {
                    "session_id": str(uuid.uuid4()),
                    "history": [],
                    "turns": [],
                    "traces": [],
                    "broken": None,
                }
                for case in talks
                for number in numbers
            }
            for index in range(max(len(case["turns"]) for case in talks)):
                groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
                for case in talks:
                    if index >= len(case["turns"]):
                        continue
                    turn = case["turns"][index]
                    checked = tuple(turn_checks(turn.get("checks"), turn_labels))
                    for number in numbers:
                        state = states[(case["id"], number)]
                        if state["broken"] is not None:
                            continue
                        groups.setdefault(checked, []).append(
                            {
                                "inputs": {
                                    **turn["inputs"],
                                    SESSION_ID: state["session_id"],
                                    HISTORY: list(state["history"]),
                                },
                                "expectations": _with_suite_criteria(turn, suite),
                                "tags": {"martian_case_id": case["id"], "martian_attempt": str(number)},
                            }
                        )
                for checked, data in groups.items():
                    frame: Any = evaluate_rows(
                        data=data, scorers=[scorer_of[label] for label in checked], predict_fn=turn_target
                    ).result_df
                    answered: set[tuple[str, int]] = set()
                    for position in range(len(frame)):
                        tags = frame["tags"].iloc[position] or {}
                        key = (str(tags.get("martian_case_id")), int(tags.get("martian_attempt", 1)))
                        state, turn = states[key], by_id[key[0]]["turns"][index]
                        why = partial(turn_reasons.pop, state["session_id"], None)
                        found = outcome(frame, position, list(checked), why)
                        if found["error"] != NOT_RUN and "assessments" in frame.columns:
                            for tokens in _judge_tokens(frame["assessments"].iloc[position], judges):
                                budget.spend(tokens)
                        state["turns"].append({"number": index + 1, "inputs": turn["inputs"], **found})
                        state["traces"].append(frame["trace"].iloc[position] if "trace" in frame.columns else None)
                        if found["error"]:
                            state["broken"] = index + 1
                        else:
                            said = user_text(turn["inputs"], suite.input_key)
                            state["history"] += [user(said), assistant(found["output"])]
                        answered.add(key)
                    for row in data:
                        key = (row["tags"]["martian_case_id"], int(row["tags"]["martian_attempt"]))
                        if key in answered:
                            continue
                        state, turn = states[key], by_id[key[0]]["turns"][index]
                        # MLflow left the turn's row out; the target's reason, when it gave one,
                        # is the turn's error (NOT_RUN when the run had stopped before it).
                        reason = turn_reasons.pop(state["session_id"], None)
                        detail = "the target failed" if reason else "no result was recorded for this turn"
                        missing = {"passed": None, "rationale": None, "error": detail}
                        state["turns"].append(
                            {
                                "number": index + 1,
                                "inputs": turn["inputs"],
                                "passed": None,
                                "checks": {label: dict(missing) for label in checked},
                                "output": None,
                                "error": reason,
                            }
                        )
                        state["traces"].append(None)
                        state["broken"] = index + 1
            # The conversation checks grade each finished conversation's replies at once.
            wholes: dict[tuple[str, int], dict[str, Any]] = {}
            data = [
                {
                    "inputs": {MESSAGES: state["history"]},
                    "outputs": replies(state["turns"]),
                    "expectations": _with_suite_criteria(by_id[case_id], suite),
                    "tags": {"martian_case_id": case_id, "martian_attempt": str(number)},
                }
                for (case_id, number), state in states.items()
                if state["broken"] is None
            ]
            if whole_labels and data:
                frame = evaluate_rows(data=data, scorers=[scorer_of[label] for label in whole_labels]).result_df
                for position in range(len(frame)):
                    tags = frame["tags"].iloc[position] or {}
                    key = (str(tags.get("martian_case_id")), int(tags.get("martian_attempt", 1)))
                    wholes[key] = outcome(frame, position, whole_labels, _no_reason)["checks"]
            out: dict[tuple[str, int], tuple[dict[str, Any], list[Any]]] = {}
            for key, state in states.items():
                case = by_id[key[0]]
                graded_by = graded_turns([turn.get("checks") for turn in case["turns"]], turn_labels)
                record = conversation_record(
                    state["turns"],
                    labels=labels,
                    graded=graded_by,
                    conversation={label: wholes.get(key, {}).get(label) for label in whole_labels},
                    session_id=state["session_id"],
                    messages=state["history"],
                    broken=state["broken"],
                )
                out[key] = (record, state["traces"])
            return out

        recorder.start(
            planned=len(cases),
            total=total,
            limit=limit,
            reused=[graded(case, finished[case["id"]]) for case in cases if case["id"] in done],
            repeats=repeat_plan if repeated else None,
            conversations=sum("turns" in case for case in cases),
        )
        # Started once the run is recorded: its limits run from here, and a cancel in martian ui
        # reaches it from here.
        budget = Budget(limits)
        cancelled = recorder.watch_cancel()

        def stopping() -> bool:
            return cancelled.is_set() or budget.reached() is not None

        calls = _Calls(target, stopping) if target is not None else None
        if talking:
            assert target is not None
            turn_target, turn_reasons = _conversation_target(target, accepted, stopping)
        ran: set[str] = set()  # the cases graded in this run
        # Leave MLflow as it was found: another evaluation in this process must not inherit this
        # run's store, experiment or worker settings.
        previous_uri = mlflow.get_tracking_uri()
        previous_experiment = active_experiment_id()
        restored = ("MLFLOW_GENAI_EVAL_MAX_WORKERS", "MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "MLFLOW_EXPERIMENT_ID")
        previous_env = {name: os.environ.get(name) for name in restored}
        os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = str(concurrency or suite.concurrency)
        os.environ["MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION"] = "true"  # otherwise the first case runs twice
        started = time.monotonic()
        try:
            mlflow.set_tracking_uri(suite.mlflow.tracking_uri or f"sqlite:///{folder.resolve() / 'mlflow.db'}")
            mlflow.set_experiment(suite.mlflow.experiment or Path(path).stem)
            for start in range(0, len(todo), suite.batch):
                # Limits are checked before each batch, and before each target call within one:
                # a batch's judges finish once it has started.
                if stopping():
                    break
                batch = todo[start : start + suite.batch]
                singles = [case for case in batch if "turns" not in case]
                rows: dict[tuple[str, int], tuple[dict[str, Any], Any]] = {}
                if singles:
                    data = []
                    for case in singles:
                        # One row per attempt; with repeats each row names its attempt.
                        for number in numbers:
                            row: dict[str, Any] = {
                                "inputs": case["inputs"],
                                "expectations": _with_suite_criteria(case, suite),
                                "tags": {"martian_case_id": case["id"]}
                                | ({"martian_attempt": str(number)} if repeated else {}),
                            }
                            if "outputs" in case:
                                row["outputs"] = case["outputs"]
                            data.append(row)
                    frame: Any = evaluate_rows(
                        data=data, scorers=scorers, predict_fn=calls.predict_fn if calls else None
                    ).result_df  # never None: evaluate_rows raises
                    by_id = {case["id"]: case for case in singles}
                    for position in range(len(frame)):
                        tags = frame["tags"].iloc[position] or {}
                        case = by_id[str(tags.get("martian_case_id"))]
                        # A case's attempts send the same inputs, so each failed attempt takes one of
                        # the reasons its case's calls failed with; attempts that ran at once may swap
                        # theirs.
                        explain = partial(calls.failure_reason, case["inputs"]) if calls else _no_reason
                        found = outcome(frame, position, labels, explain)
                        if found["error"] != NOT_RUN and "assessments" in frame.columns:
                            for tokens in _judge_tokens(frame["assessments"].iloc[position], judges):
                                budget.spend(tokens)
                        trace = frame["trace"].iloc[position] if "trace" in frame.columns else None
                        rows[(case["id"], int(tags.get("martian_attempt", 1)))] = (found, trace)
                talks = [case for case in batch if "turns" in case]
                talked = converse(talks) if talks else {}
                recorded: list[GradedCase] = []
                for case in batch:
                    if "turns" in case:
                        conversations = [talked[(case["id"], number)] for number in numbers]
                        # The run stopped before this conversation, or before one of its turns in
                        # one of its attempts: it has no result, and the next run sends every turn
                        # of every attempt again.
                        if any(found["error"] == NOT_RUN for found, _ in conversations):
                            continue
                        attempts = [
                            {"number": number, **found}
                            for number, (found, _) in zip(numbers, conversations, strict=True)
                        ]
                        result = conversations[0][0]
                        if repeated:
                            # A check that grades nothing in this case has no verdict to combine.
                            used = {label: rule for label, rule in rules.items() if label in attempts[0]["checks"]}
                            result = combine_attempts(attempts, used)
                            chosen = attempts[representative([a["passed"] for a in attempts], result["passed"])]
                            result |= {"session_id": chosen["session_id"], "turns": chosen["turns"]}
                        record = {
                            "id": case["id"],
                            "passed": result["passed"],
                            "checks": result["checks"],
                            "session_id": result["session_id"],
                            "turns": result["turns"],
                            "output": result["output"],
                            "error": result["error"],
                            "fingerprint": prints[case["id"]],
                        }
                        if repeated:
                            record["attempts"] = attempts
                        results.append(record)
                        recorded.append(graded(case, record, turn_traces=[traces for _, traces in conversations]))
                        continue
                    tried = [
                        rows.get((case["id"], number))
                        or (unrecorded(calls.failure_reason(case["inputs"]) if calls else None), None)
                        for number in numbers
                    ]
                    # The run stopped before this case, or before some of its attempts: it has no
                    # result, and the next run runs all of its attempts, so a verdict always
                    # combines every attempt. The judge tokens of the attempts that ran still count.
                    if any(found["error"] == NOT_RUN for found, _ in tried):
                        continue
                    attempts = [{"number": number, **found} for number, (found, _) in zip(numbers, tried, strict=True)]
                    result = combine_attempts(attempts, rules) if repeated else tried[0][0]
                    record = {
                        "id": case["id"],
                        "passed": result["passed"],
                        "checks": result["checks"],
                        "inputs": case["inputs"],
                        "output": result["output"],
                        "error": result["error"],
                        "fingerprint": prints[case["id"]],
                    }
                    if repeated:
                        record["attempts"] = attempts
                    results.append(record)
                    recorded.append(graded(case, record, [trace for _, trace in tried]))
                recorder.graded(recorded)
                ran |= {item.record["id"] for item in recorded}
                say(f"{len(ran)}/{len(todo)} cases graded")
        finally:
            mlflow.set_tracking_uri(previous_uri)
            restore_active_experiment_id(previous_experiment)
            for name, value in previous_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        # The cases the run stopped before have no result, so the next run grades them.
        not_run = [case["id"] for case in todo if case["id"] not in ran]
        stopped = budget.reached() if not_run and not cancelled.is_set() else None
        final = results.records()
        ordered = [final[case["id"]] for case in cases if case["id"] in final and case["id"] not in not_run]
        groups = {case.id: case.tags[GROUP_TAG] for case in model_cases if GROUP_TAG in case.tags}
        refusals = _false_refusals(
            suite.checks,
            ordered,
            {case["id"]: case["expectations"] for case in cases},
            {case["id"]: case["turns"] for case in cases if "turns" in case},
        )
        summary = _summary(
            ordered,
            labels,
            time.monotonic() - started,
            invalidated,
            groups,
            repeat_plan if repeated else None,
            refusals,
            scopes if talking else None,
            not_run=len(not_run),
            stopped=stopped,
        )
        summary["output"] = str(folder)
        if not_run and cancelled.is_set():
            summary["cancelled"] = True
        (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        why = "the run was cancelled" if cancelled.is_set() else (stopped or {}).get("message", "")
        write_junit(
            ordered,
            folder / "junit.xml",
            suite_name=Path(path).stem,
            summary=summary,
            not_run=not_run,
            stopped=why,
        )
        if not not_run:
            recorder.finish()
        elif cancelled.is_set():
            recorder.cancel()
        else:
            assert stopped is not None  # only a limit or a cancel leaves a case not run
            recorder.stop(stopped)
    return {**summary, "run": recorder.reference()}
