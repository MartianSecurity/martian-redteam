"""Run an eval suite from one YAML file, locally, with only a model key.

    martian evals run evals.yaml

The suite names a data file (JSONL or CSV), the checks to apply, and optionally the target under
test: a Python function, or an HTTP endpoint. Nothing here contacts the Martian platform: a
function target runs in a process of its own, given each case's inputs only, which cannot reach
the expectations, checks, scorers or results (:mod:`martian.evals.isolation`); a URL target is
called from this process with a body filled from the inputs; LLM judges call the model you
configure, and MLflow's evaluation harness uses a local store in the output folder unless you point
it at your own server. The run, its case results, scores and traces are also recorded in the
local Evals UI's data folder, so ``martian ui`` shows them. A run can be stopped and started again;
a finished case is skipped while nothing that produced its verdict has changed. A suite can run each
case several times and combine the attempts' verdicts by a rule (:mod:`martian.evals.repeats`),
and a suite with a ``matrix`` runs once per combination of its prompts, models or other values
(:mod:`martian.evals.matrix`), each combination in its own folder and recorded as its own run.
A case can be a conversation of several user turns, sent in order to a target that keeps the
conversation and graded per turn and as a whole (:mod:`martian.evals.conversations`), and its
user can be simulated: a model plays a goal and a persona, writing each turn from the conversation
so far (:mod:`martian.evals.simulation`). Use ``martian ci`` when a result must be recorded on the
platform.

Every target call has a time limit, ``target.timeout_seconds``: a URL target's client stops the
call, and a function target's process runs each call on a thread of its own, which the run stops
waiting for at the limit. A run has limits too (``limits``: ``max_seconds``, ``max_judge_tokens``,
which counts the tokens of its LLM judges and its simulated users): once it reaches one, it calls
no more targets, records the cases it finished, and ends ``stopped``; running it again resumes
after them. A case is the unit a run stops at and resumes from: with repeats, a case whose
attempts did not all run has no result, and so has a conversation whose turns did not all run;
running again runs all of its attempts and turns. Cancelling the run in ``martian ui`` stops it
the same way, as ``cancelled``.

With the cache on (``cache`` in the suite file, or ``--cache``), the target's and the judges'
replies are stored by their full request and replayed when a later run sends the same one
(:mod:`martian.evals.cache`). A replayed judgment used no tokens, so it spends nothing of
``max_judge_tokens``. A suite whose data has conversation cases is refused with the cache on: the
cache does not hold a conversation's turns.
"""

from __future__ import annotations

import contextvars
import csv
import functools
import hashlib
import inspect
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from martian._version import __version__
from martian.config import ConfigError
from martian.evals import _custom, _numeric, _policy, _tool_calls
from martian.evals._digits import ascii_digits
from martian.evals._endpoint_errors import names_unknown_model
from martian.evals._json_schema import check_schema
from martian.evals.cache import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    CacheError,
    ModelCallCache,
    exact_json,
    key_address,
    key_headers,
)
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
from martian.evals.matrix import Combination, MatrixAxis, combinations
from martian.evals.model import MAX_ATTEMPTS, MAX_TURNS, CheckScope, CombineRule, SimulatedUser, Turn
from martian.evals.panel import PREFIX as PANEL_PREFIX
from martian.evals.panel import check_models
from martian.evals.repeats import combine_attempts, passed_every_attempt, plan, reliability, representative
from martian.evals.simulation import GOAL, SIMULATOR_VERSION, SimulatedUsers, Step, goal_verdict
from martian.evals.stats import proportion_interval

GROUP_TAG = "martian.group"
"""The case tag that holds a data file case's ``group``."""
# LLM judges graded by criteria the suite or the case gives, under the judge's own type name.
CRITERIA_JUDGES = ("rubric", "guidelines", "conditions")
# LLM judges of the case's documents (inputs.context) that take no criteria.
GROUNDING_JUDGES = ("groundedness", "context_relevance", "context_sufficiency")
# LLM judges that read a whole conversation (martian.evals.conversation_judges): they grade
# conversation cases only, at scope: conversation.
CONVERSATION_JUDGES = ("task_completion", "policy_adherence", "knowledge_retention")
JUDGES = CRITERIA_JUDGES + GROUNDING_JUDGES + CONVERSATION_JUDGES
CRITERIA_KEYS = {
    "rubric": "rubric",
    "guidelines": "guidelines",
    "conditions": "conditions",
    "policy_adherence": "guidelines",
}
"""The judges whose criteria a suite file can give once for every case, and the expectation key each
reads them from. A ``policy_adherence`` judge reads ``guidelines``, as a ``guidelines`` judge does: a
conversation's guidelines are the case's, and each turn's its own."""
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
    "tool_called",
    "tool_not_called",
    "tool_trajectory",
    "step_count",
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
# Checks of the agent's tool calls, read from the trace of the target's call or from its reply.
TOOL_CHECKS = _tool_calls.TOOL_CHECKS
# A team's own rule: a Python function the suite file names (martian.evals._custom).
CODE_CHECKS = _custom.CODE_TYPES
# A check a worker installs by name (martian.evals.worker.hooks); this runner installs none.
INSTALLED_CHECKS = _custom.INSTALLED_TYPES
NOT_INSTALLED_HERE = (
    "check {label}: an installed check runs in a worker that installs a check under that name, "
    "and this command installs none"
)
SECURITY_CHECKS = ("not_contains", "not_matches", *REFUSAL_PASSING, "tool_not_called")
"""Check types that fail a reply for holding something it must never hold, such as a leaked secret,
personal data, a prohibited phrase or an injected instruction's marker, or an agent for a tool call
it must never make. A case's attempts combine by ``all`` for these unless the check names its own
rule, so a leak or a forbidden call in one attempt of five is never outvoted. ``no_refusal`` is not
one: it fails a reply for refusing a benign request, which measures over-refusal rather than a leak,
so its attempts combine by the suite's rule. A new check type that fails a reply for a leak belongs
here. A ``python`` check is not one by its type: it is one when it says so (``security: true``),
since only its author knows what it guards (:attr:`CheckSpec.security_check`)."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# In a URL target's body: {{input}}, {{inputs.NAME}} and {{uuid}} are filled in for each case,
# {{matrix.NAME}} for each combination of the suite's matrix, and {{session_id}}, {{history}} and
# {{messages}} with the conversation (martian.evals.conversations).
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_PLACEHOLDER_NAME = re.compile(r"input|uuid|session_id|history|messages|inputs\.[A-Za-z_][A-Za-z0-9_]*")
# A suite file's {{matrix.NAME}}, which only its own URL target takes (check_body's matrix).
_MATRIX_NAME = re.compile(r"matrix\.[A-Za-z_][A-Za-z0-9_]*")
_MATRIX = "matrix."
# The validation context key that holds the axes of the suite file's matrix (parse_suite), so a
# URL target's {{matrix.NAME}} names one of them. A target validated without it has no matrix.
MATRIX_AXES = "martian.matrix_axes"
# Placeholders that name what grades the target rather than what it is given.
_GRADER_PLACEHOLDER = re.compile(r"expect|output", re.IGNORECASE)
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


def check_body(body: Any, setting: str, *, matrix: bool = False) -> None:
    """Refuse a body template whose placeholders name anything but the case's inputs: a name that
    starts like ``expectations``, ``expected`` or ``outputs`` because a target is never sent the
    answers it is graded against, any other unknown name because nothing would fill it in. With
    ``matrix``, a suite file's own URL target, ``{{matrix.NAME}}`` is one of the inputs too (the
    suite file checks NAME is an axis); anywhere else, such as a worker's target, nothing fills it.
    Raises ``ValueError`` naming ``setting``, before any case is sent."""
    names = _placeholders(body)
    allowed = (
        "{{input}}, {{inputs.NAME}}, "
        + ("{{matrix.NAME}}, " if matrix else "")
        + "{{uuid}}, {{session_id}}, {{history}} or {{messages}}"
    )
    unknown = sorted(
        {
            name
            for name in names
            if not _PLACEHOLDER_NAME.fullmatch(name) and not (matrix and _MATRIX_NAME.fullmatch(name))
        }
    )
    if graded := [name for name in unknown if _GRADER_PLACEHOLDER.match(name)]:
        raise ValueError(
            f"{setting} placeholder(s) {', '.join(graded)} name the case's expectations or outputs, which are never "
            f"sent to the target; use {allowed}"
        )
    if unknown:
        raise ValueError(f"unknown {setting} placeholder(s) {', '.join(unknown)}; use {allowed}")


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
        if step.isdigit() and not ascii_digits(step):
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
    def one_kind(self, info: ValidationInfo) -> TargetRef:
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
        check_body(self.body, "body", matrix=True)
        names = _placeholders(self.body)
        axes = (info.context or {}).get(MATRIX_AXES, ())
        if strays := sorted({name for name in names if name.startswith(_MATRIX) and name[len(_MATRIX) :] not in axes}):
            shown = ", ".join("{{" + name + "}}" for name in strays)
            raise ValueError(f"{shown} names no axis of the suite file's matrix")
        if not any(name in ("input", MESSAGES) or name.startswith("inputs.") for name in names):
            raise ValueError(
                "a url target's body must use {{input}}, {{inputs.NAME}} or {{messages}}, or every case sends the same"
            )
        return self


class JudgeConfig(_Model):
    # The model of every LLM judge check that names no panel of its own.
    model: str | None = None
    inference_params: dict[str, float | int] = Field(default_factory=dict)
    # Added to every judge request; ${NAME} in a value is read from the environment.
    headers: dict[str, str] = Field(default_factory=dict)
    # Server errors and connection failures are retried this many times.
    max_retries: int = Field(default=5, ge=0, le=10)
    # Rate limits are retried for up to this many seconds, as long as the provider asks.
    rate_limit_wait_seconds: float = Field(default=300, ge=0, le=3600)


class SimulatorConfig(JudgeConfig):
    # The simulator has the judge's settings, and always names its model: it has no panel.
    model: str


class CheckSpec(_Model):
    type: Literal[
        "rubric",
        "guidelines",
        "conditions",
        "groundedness",
        "context_relevance",
        "context_sufficiency",
        "task_completion",
        "policy_adherence",
        "knowledge_retention",
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
        "tool_called",
        "tool_not_called",
        "tool_trajectory",
        "step_count",
        "python",
        "installed",
    ]
    name: str | None = None
    # Criteria for an LLM judge, applied to every row that does not carry its own; a
    # policy_adherence judge takes guidelines too.
    rubric: str | None = None
    guidelines: list[str] | None = None
    conditions: list[dict[str, str]] | None = None
    # An LLM judge's panel: the judge models that each grade it, in place of judge.model; the
    # strict majority of those that give a verdict decides (martian.evals.panel).
    panel: list[str] | None = None
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
    # Tool-call checks: the calls (a tool's name, or {name, arguments}) or, for tool_trajectory,
    # the steps (a call, or a list of the calls one model turn makes), how arguments match, the
    # order, the bounds, and where the calls are read (the trace, or the reply).
    calls: list[Any] | None = None
    arguments_match: str | None = None
    order: str | None = None
    source: str | None = None
    min_calls: int | None = None
    max_calls: int | None = None
    min_turns: int | None = None
    max_turns: int | None = None
    # A python check: the function `function` in `file` (relative to the suite file), the bounds a
    # number it returns must keep, and whether it is a security check for repeated attempts.
    file: str | None = None
    function: str | None = None
    min_score: float | None = Field(default=None, allow_inf_nan=False)
    max_score: float | None = Field(default=None, allow_inf_nan=False)
    security: bool = False
    # An installed check: the options the worker's check is given, as written. Its name is `name`.
    options: dict[str, Any] | None = None
    # How this check's verdicts over a case's attempts combine, when the suite repeats its cases.
    combine: CombineRule | None = None
    # What it grades in a conversation case: each turn's reply, or the whole conversation once.
    scope: CheckScope = "turn"

    @model_validator(mode="after")
    def complete(self) -> CheckSpec:
        if self.panel is not None:
            if self.type not in JUDGES:
                raise ValueError(f"a panel names judge models, and a {self.type} check is no LLM judge")
            check_models(self.panel)
        if self.type in CONVERSATION_JUDGES and self.scope != "conversation":
            raise ValueError(f"a {self.type} judge reads a whole conversation; give it scope: conversation")
        if self.scope == "conversation" and self.type in JUDGES and self.type not in CONVERSATION_JUDGES:
            raise ValueError(
                f"a {self.type} judge grades each turn's reply; scope: conversation is for deterministic checks, "
                f"python checks and the conversation judges ({', '.join(CONVERSATION_JUDGES)})"
            )
        if (
            self.type == "policy_adherence"
            and self.guidelines is not None
            and (not self.guidelines or not all(guideline.strip() for guideline in self.guidelines))
        ):
            raise ValueError("a policy_adherence check's guidelines must be a nonempty list of nonempty strings")
        if self.scope == "conversation" and self.type in CALL_CHECKS:
            raise ValueError(f"a {self.type} check measures one target call; scope: conversation does not apply to it")
        if self.scope == "conversation" and self.type in TOOL_CHECKS:
            raise ValueError(
                f"a {self.type} check reads the tool calls of one target call, so it grades each turn's call "
                "(scope: turn); scope: conversation does not apply to it"
            )
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
        if self.type in TOOL_CHECKS:
            self.tool_options()
        if self.type in CODE_CHECKS:
            # Only the shape: the file is imported when a run starts, never when the suite loads.
            if not self.file or not self.function:
                raise ValueError("a python check needs a file and a function, the scorer to call")
            if not self.function.isidentifier():
                raise ValueError("function must be the name of a function in the file")
            _custom.bounds(self.min_score, self.max_score)
        if self.type in INSTALLED_CHECKS and not self.name:
            raise ValueError("an installed check needs a name: the name a worker installs the check under")
        return self

    def tool_options(self) -> _tool_calls.ToolOptions:
        """The options of a tool-call check, read as the check reads them, so a bad one is
        refused when the suite loads."""
        given: dict[str, Any] = {"source": self.source, "max_calls": self.max_calls}
        if self.min_calls is not None:
            given["min_calls"] = self.min_calls
        if self.type == "step_count":
            return _tool_calls.tool_options(self.type, min_turns=self.min_turns, max_turns=self.max_turns, **given)
        given.update(
            calls=self.calls or (), expectation=self.expectation, arguments_match=self.arguments_match or "subset"
        )
        if self.type == "tool_trajectory":
            given["order"] = self.order or "exact"
        return _tool_calls.tool_options(self.type, **given)

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
    def security_check(self) -> bool:
        """Whether this is a security check, whose attempts combine by ``all`` unless it says
        otherwise: a type in :data:`SECURITY_CHECKS`, or a python check that says it is one."""
        return self.type in SECURITY_CHECKS or (self.type in CODE_CHECKS and self.security)


class MlflowConfig(_Model):
    tracking_uri: str | None = None
    experiment: str | None = None


class CacheConfig(_Model):
    """The cache of model calls (:mod:`martian.evals.cache`). ``folder`` is relative to the suite
    file, and without one the cache is the local data folder's ``cache`` folder. A function
    target's calls are cached only with ``functions``: a function is your code, and it may do
    more than answer."""

    folder: str | None = Field(default=None, min_length=1)
    ttl_seconds: float = Field(default=DEFAULT_TTL_SECONDS, gt=0, le=MAX_TTL_SECONDS, allow_inf_nan=False)
    functions: bool = False


class LimitsConfig(_Model):
    """When the run stops before its last case: after ``max_seconds`` of wall time, or once its
    LLM judges and simulated users have used ``max_judge_tokens`` tokens (``RunLimits``)."""

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
    # Axes of values the suite runs every combination of (martian.evals.matrix).
    matrix: dict[str, MatrixAxis] = Field(default_factory=dict)
    # The cache of model calls; a `cache:` block, even an empty one, turns it on.
    cache: CacheConfig | None = None
    # The model that plays the data's simulated users (martian.evals.simulation), with the judge's
    # settings.
    simulator: SimulatorConfig | None = None

    @field_validator("cache", mode="before")
    @classmethod
    def _empty_cache_block(cls, value: Any) -> Any:
        """``cache:`` with nothing under it turns the cache on with its defaults."""
        return {} if value is None else value

    @model_validator(mode="after")
    def consistent(self) -> Suite:
        labels = [check.label for check in self.checks]
        if len(set(labels)) != len(labels):
            raise ValueError("check names must be unique; give repeated check types a name")
        kinds = [check.type for check in self.checks if check.type in JUDGES]
        if len(set(kinds)) != len(kinds):
            raise ValueError(f"use at most one check of each judge type ({', '.join(JUDGES)})")
        single = [check for check in self.checks if check.type in JUDGES and check.panel is None]
        if single and (self.judge is None or self.judge.model is None):
            raise ValueError("LLM judge checks need a judge.model, or a panel of their own")
        if self.limits.max_judge_tokens is not None and not kinds and self.simulator is None:
            raise ValueError(
                "limits.max_judge_tokens counts the tokens of LLM judge checks and simulated users, "
                "and the suite has neither"
            )
        calls = [check.label for check in self.checks if check.type in CALL_CHECKS]
        if calls and self.target is None:
            raise ValueError(
                f"{', '.join(calls)} measures the target's call, and a suite without a target grades stored "
                "outputs, which no call produced; name a target or remove the check"
            )
        tools = [check for check in self.checks if check.type in TOOL_CHECKS]
        traced = [check.label for check in tools if check.source == "trace"]
        if traced and self.target is None:
            raise ValueError(
                f"{', '.join(traced)} reads the trace of the target's call, and a suite without a target grades "
                "stored outputs, which no call produced; read the tool calls in the stored reply (source: reply)"
            )
        untraced = [check.label for check in tools if check.source != "reply"]
        if untraced and self.target is not None and self.target.url is not None:
            raise ValueError(
                f"{', '.join(untraced)} reads the tool calls in the trace of the target's call, and a URL target's "
                "call records none; set source: reply to read them in its response"
            )
        costs = [check.label for check in self.checks if check.type == "cost"]
        if costs and self.target is not None and self.target.url is not None:
            raise ValueError(
                f"{', '.join(costs)} reads the token usage and cost the target's MLflow tracing records, and a "
                "URL target's call records neither; use a function target or remove the check"
            )
        if "matrix" in self.model_fields_set and not self.matrix:
            raise ValueError("a matrix needs at least one axis; name its axes or remove it")
        if self.matrix:
            self._matrix_fits()
        functions = self.cache is not None and "functions" in self.cache.model_fields_set
        if functions and (self.target is None or self.target.file is None):
            raise ValueError("cache.functions only applies to a function target")
        return self

    def _matrix_fits(self) -> None:
        """A matrix runs every combination of its values against the target: a URL target's body
        takes each axis as ``{{matrix.NAME}}``, and a function target each as a keyword argument."""
        combinations(self.matrix)
        if self.target is None:
            raise ValueError(
                "a matrix varies the target, and a suite without one grades the outputs stored in its cases; "
                "name a target or remove the matrix"
            )
        if self.target.url is not None:
            used = {name[len(_MATRIX) :] for name in _placeholders(self.target.body) if name.startswith(_MATRIX)}
            if unused := [axis for axis in self.matrix if axis not in used]:
                raise ValueError(
                    f"target.body has no {{{{matrix.{unused[0]}}}}}, so every value of matrix.{unused[0]} sends the "
                    "same request; use it in the body or remove the axis"
                )

    def security_checks(self) -> list[str]:
        """The labels of the security checks, in suite order. Their attempts combine by ``all``
        unless they name a rule, and compared with a baseline a case that newly fails one fails
        the gate whatever the noise label (:mod:`martian.evals.baseline`). A check is one by
        :attr:`CheckSpec.security_check`: its type, or a python check's own ``security``."""
        return [check.label for check in self.checks if check.security_check]

    def repeat_plan(self) -> dict[str, Any]:
        """The attempts of each case, the suite's rule, and each check's rule and where it came from."""
        security = self.security_checks()
        checks = [(check.label, check.combine, check.label in security) for check in self.checks]
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
    axes = list(raw["matrix"]) if isinstance(raw.get("matrix"), dict) else []
    try:
        return Suite.model_validate(raw, context={MATRIX_AXES: axes})
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
    ``expectations`` and ``checks`` (the names of the turn checks that grade it). A simulated
    conversation case has ``simulated_user`` instead: an object with ``goal``, ``persona``,
    ``max_turns`` and ``stop_when``.
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
        simulated = _simulated_user(record, identifier) if "simulated_user" in record else None
        turns = _turns(record, identifier, input_key) if "turns" in record else None
        inputs = record.get("inputs")
        if inputs is None and isinstance(record.get("input"), str):
            inputs = {input_key: record["input"]}
        if turns is not None or simulated is not None:
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
        if simulated is not None:
            case["simulated_user"] = simulated
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


SIMULATED_USER_FIELDS = ("goal", "persona", "max_turns", "stop_when")
"""The fields of a case's ``simulated_user``, every one required."""


def _simulated_user(record: dict[str, Any], identifier: str) -> dict[str, Any]:
    """A simulated conversation case's user: its goal, persona, turn budget and stop condition."""
    where = f"case {identifier}: simulated_user"
    given = record["simulated_user"]
    if not isinstance(given, dict):
        raise ConfigError(f"{where} must be an object with {', '.join(SIMULATED_USER_FIELDS)}")
    if unknown := sorted(set(given) - set(SIMULATED_USER_FIELDS)):
        raise ConfigError(
            f"{where} has no field {', '.join(unknown)}; its fields are {', '.join(SIMULATED_USER_FIELDS)}"
        )
    for given_too in ("turns", "input", "inputs"):
        if given_too in record:
            raise ConfigError(
                f"case {identifier}: a simulated user writes the conversation's turns; remove {given_too}"
            )
    if {"output", "outputs"} & set(record):
        raise ConfigError(f"case {identifier}: a conversation runs against a target, so it has no stored output")
    for name in ("goal", "persona", "stop_when"):
        if not isinstance(given.get(name), str) or not given[name].strip():
            raise ConfigError(f"{where}.{name} must be a nonempty string")
    budget = given.get("max_turns")
    if type(budget) is not int or not 1 <= budget <= MAX_TURNS:
        raise ConfigError(f"{where}.max_turns must be a whole number of turns from 1 to {MAX_TURNS}")
    return {name: given[name] for name in SIMULATED_USER_FIELDS}


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


def _case_values(
    inputs: dict[str, Any], input_key: str, matrix: Mapping[str, Any] | None = None
) -> Callable[[str], Any]:
    """What each body placeholder of one case stands for: ``{{input}}`` is the input named
    ``input_key``, ``{{inputs.NAME}}`` the input named NAME, ``{{matrix.NAME}}`` the value of the
    axis NAME in ``matrix``, a combination's values, and ``{{uuid}}`` a new UUID."""

    def lookup(name: str) -> Any:
        if name == "uuid":
            return str(uuid.uuid4())
        if name.startswith(_MATRIX):
            axis = name[len(_MATRIX) :]
            if matrix is None or axis not in matrix:
                raise TargetCallError(f"the suite's matrix has no axis named {axis!r}")
            return matrix[axis]
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
    matrix: Mapping[str, Any] | None = None,
    keyed: bool = False,
) -> Any:
    """One request's body from a URL target's ``template``: ``{{input}}``, ``{{inputs.NAME}}``,
    ``{{matrix.NAME}}`` (``matrix`` is one combination's values) and ``{{uuid}}`` as
    :func:`_case_values` gives them, and the conversation's ``{{session_id}}``, ``{{history}}``
    (the earlier turns as messages) and ``{{messages}}`` (the history and this turn's user
    message). ``conversation`` is the session id and history of a conversation turn; without one,
    the request is a conversation of one turn: a new session id, no history. ``prompt``, when
    given, is what ``{{input}}`` and the user message are, as the evals core's worker sends a
    case's prompt; otherwise the user message is the input under ``input_key``. ``keyed`` gives
    the body as the cache's key holds it (:func:`_http_request`): ``{{uuid}}`` and
    ``{{session_id}}`` as written, since each call sends new ones."""
    case = _case_values(inputs, input_key, matrix)
    said = prompt if prompt is not None else user_text(inputs, input_key)
    session_id, history = conversation if conversation is not None else (str(uuid.uuid4()), [])

    def lookup(name: str) -> Any:
        if keyed and name in ("uuid", SESSION_ID):
            return "{{" + name + "}}"
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
        elif isinstance(reply, list) and ascii_digits(step) and int(step) < len(reply):
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


def _http_target(reference: TargetRef, input_key: str, matrix: Mapping[str, Any] | None = None) -> Callable[..., Any]:
    """Call the endpoint once per case: no retry, no redirect, no proxy from the environment.
    ``matrix``, one combination's values, fills the body's ``{{matrix.NAME}}``, as case text is
    filled: inserted as data and never expanded itself.

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
        body = request_body(reference.body, inputs, input_key, conversation=conversation, matrix=matrix)
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


def _relative(path: str, base: Path) -> Path:
    file = Path(path)
    return file if file.is_absolute() else base / file


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
    conversation gives it those. One that takes neither cannot keep a conversation. A function
    target in a process of its own (:class:`_ProcessTarget`) is never imported here: its process
    reads the function's parameters."""
    # Through the matrix's and the cache's wrappers to the function, which may run in a process.
    inner = inspect.unwrap(function)
    try:
        signature = inner.signature() if isinstance(inner, _ProcessTarget) else inspect.signature(function)
        parameters = signature.parameters.values()
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


def _target_file(reference: TargetRef, base: Path) -> Path:
    """A function target's file, relative to the suite file unless it is absolute."""
    return _relative(reference.file or "", base)


class _ProcessTarget:
    """A function target as MLflow's evaluation harness calls it, in a process of its own
    (:class:`martian.evals.isolation.TargetProcess`), started at once. Each call opens the case's
    span here, named after the function, with the case's inputs, sends the inputs, the only part of
    the case the target is given, and adds the spans the target recorded under it. A call that
    outlasts ``timeout_seconds`` is the case's error (``TIMED_OUT``) and the run goes on. The
    target is never imported in this process, which holds the run's expectations and scorers."""

    def __init__(self, reference: TargetRef, base: Path, private: list[Path]) -> None:
        from martian.evals.isolation import TargetProcess

        self.__name__ = self.__qualname__ = reference.function
        self.__mlflow_traced__ = True  # it opens the case's span itself
        self._timeout = reference.timeout_seconds
        self._process = TargetProcess(
            _relative(reference.file or "", base), reference.function, private=private, trace=True
        )

    def ready(self) -> str | None:
        """Wait for the target's process; None when the private paths are hidden from it, or why not."""
        return self._process.ready()

    def signature(self) -> inspect.Signature:
        """The target function's keyword parameters, which its process read once it imported it."""
        return self._process.signature()

    def __call__(self, **inputs: Any) -> Any:
        from martian.evals._mlflow import add_spans, start_span
        from martian.evals.isolation import TargetRaised

        with start_span(self.__name__) as span:
            span.set_inputs(inputs)
            try:
                value, trace = self._process.call(inputs, self._timeout)
            except TargetRaised as raised:
                if raised.trace is not None:
                    add_spans(raised.trace, span)
                raise
            if trace is not None:
                add_spans(trace, span)
            span.set_outputs(value)
            return value

    def close(self) -> None:
        self._process.close()


def _private_paths(suite: Suite, suite_file: Path, base: Path, output: Path, home: Path) -> list[Path]:
    """What a function target must not reach, because it grades the target or holds what grading
    found: the suite file, which holds the checks and their criteria; the data file, which holds the
    expectations; each python check's file and the compiled copies beside it; the output folder,
    with the results and MLflow's local store; MLflow's store when the suite names a local SQLite
    or file one; and the local Evals UI's data folder. A target whose own file is among them cannot
    be run, and is a ``ConfigError`` naming the setting that holds it."""
    named: list[tuple[str, Path]] = [
        ("the suite file", suite_file),
        ("data", _relative(suite.data, base)),
        ("the output folder", output),
        ("the local Evals UI's data folder", home),
    ]
    for check in suite.checks:
        if check.type in CODE_CHECKS:
            scorer = _relative(check.file or "", base)
            named += [(f"check {check.label}", scorer), (f"check {check.label}", scorer.parent / "__pycache__")]
    uri = suite.mlflow.tracking_uri or ""
    if uri.startswith("sqlite:///"):
        named.append(("mlflow.tracking_uri", Path(uri.removeprefix("sqlite:///"))))
    elif uri.startswith("file://"):
        named.append(("mlflow.tracking_uri", Path(urlsplit(uri).path)))
    elif uri and "://" not in uri:  # a folder, which MLflow reads as a file store
        named.append(("mlflow.tracking_uri", Path(uri)))
    assert suite.target is not None and suite.target.file is not None
    target = os.path.realpath(_relative(suite.target.file, base))
    for setting, path in named:
        found = os.path.realpath(path)
        if target == found or target.startswith(found.rstrip(os.sep) + os.sep):
            raise ConfigError(
                f"target.file is inside {setting}, which the target may not read; keep the target's file elsewhere"
            )
    return [path for _, path in named]


def _load_target(
    reference: TargetRef, base: Path, input_key: str = "message", private: list[Path] | None = None
) -> Callable[..., Any]:
    """The target's call, which never takes much longer than ``timeout_seconds``, whatever it is. A
    function target is a :class:`_ProcessTarget`, in a process of its own that cannot reach
    ``private``; the caller waits for it with ``ready()`` and ends it with ``close()``."""
    if reference.url is not None:
        call = _http_target(reference, input_key)
        limit = reference.timeout_seconds + CLIENT_SLACK_SECONDS
        timed = _function_target(call, limit)
        # A conversation's turn has the same time limit as a case's call.
        timed.converse = _function_target(call.converse, limit)  # type: ignore[attr-defined]
        return timed
    return _ProcessTarget(reference, base, private or [])


def _with_arguments(function: Callable[..., Any], values: Mapping[str, Any]) -> Callable[..., Any]:
    """``function`` called with each axis of one combination (``values``) as a keyword argument,
    beside the case's inputs. The wrapper keeps the function's name, so the case's trace is named
    as without a matrix, and a function MLflow already traces is not traced twice."""
    if inspect.iscoroutinefunction(function):

        async def waited(**inputs: Any) -> Any:
            return await function(**inputs, **values)

        call: Callable[..., Any] = waited
    else:

        def call(**inputs: Any) -> Any:
            return function(**inputs, **values)

    call.__name__ = getattr(function, "__name__", "target")
    call.__qualname__ = getattr(function, "__qualname__", call.__name__)
    # inspect.signature reads the function's own parameters through the wrapper, so a conversation
    # gives it only the history or session id it takes (_conversation_parameters).
    call.__wrapped__ = function  # type: ignore[attr-defined]
    if getattr(function, "__mlflow_traced__", False):
        call.__mlflow_traced__ = True  # type: ignore[attr-defined]
    return call


def _http_request(
    reference: TargetRef, input_key: str, matrix: Mapping[str, Any] | None = None
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """What decides a URL target's reply to one case's inputs, as the cache's key holds it: the
    address as called but its credential query parameters, the headers but credential ones
    (:func:`martian.evals.cache.key_headers`), the body with the case's and the combination's
    values filled in as :func:`request_body` fills them, with ``{{uuid}}`` and ``{{session_id}}`` as
    written, since each call sends new ones, and ``response_path``. A suite with conversation cases
    runs without the cache (:func:`run_suite`), so a key's ``{{history}}`` is always empty."""
    url = key_address(_from_environment(reference.url or "", "target.url"))
    headers = key_headers(
        {name: _from_environment(value, f"target.headers.{name}") for name, value in reference.headers.items()}
    )
    steps = _response_steps(reference.response_path, "target.response_path")

    def request(inputs: dict[str, Any]) -> dict[str, Any]:
        body = request_body(reference.body, inputs, input_key, matrix=matrix, keyed=True)
        return {"url": url, "headers": headers, "body": body, "response_path": steps}

    return request


def _cached_target(
    target: Callable[..., Any], cache: ModelCallCache, request_of: Callable[[dict[str, Any]], dict[str, Any]]
) -> Callable[..., Any]:
    """``target`` with its replies stored in ``cache`` under ``request_of(inputs)``, the request
    that decides each reply. It wraps the target's time limit, so a call that timed out is never
    stored, even when it ends later. A failed call raises as it does without the cache and is
    never stored, nor is a reply JSON cannot hold exactly, or None. A failure to store a reply
    is a ``TargetCallError`` naming ``cache.folder``.

    The wrapper keeps the target's signature, which MLflow's evaluation harness reads, but is never
    marked as traced, so MLflow records a trace of every call, also of a replayed one; a function
    that traces itself has its own span under that trace when it runs."""

    def stored(reply: Any) -> Any:
        return reply if reply is not None and exact_json(reply) else None

    @functools.wraps(target)
    def call(**inputs: Any) -> Any:
        try:
            return cache.call("target", request_of(inputs), lambda: target(**inputs), store=stored)
        except CacheError as error:
            raise TargetCallError(str(error)) from None

    call.__dict__.pop("__mlflow_traced__", None)
    return call


def _targets(
    suite: Suite,
    base: Path,
    matrix: list[Combination],
    cache: ModelCallCache | None = None,
    function: _ProcessTarget | None = None,
) -> list[Callable[..., Any] | None]:
    """The target of each combination, in order, or of the suite once when it has no matrix. A
    function target is ``function``, its one process (:func:`_load_target`), which every
    combination calls with its values; a URL target's settings are read from the
    environment for each, so a missing variable stops the run before any case. With ``cache``,
    a URL target's calls are cached, and a function target's when the cache's ``functions`` is on:
    by its file's SHA-256, its name and its arguments, which with a matrix hold the combination's
    values (:func:`_cached_target`). A URL target has ``converse``, which sends one turn of a
    conversation (:func:`_http_target`) under the same time limit; a suite with conversation cases
    runs without the cache (:func:`run_suite`)."""
    reference = suite.target
    if reference is None:
        return [None] * max(len(matrix), 1)
    if reference.url is not None:
        # A URL target's call never takes much longer than timeout_seconds, as a function's.
        limit = reference.timeout_seconds + CLIENT_SLACK_SECONDS
        built: list[Callable[..., Any] | None] = []
        each: list[Mapping[str, Any] | None] = [combination.values for combination in matrix] or [None]
        for values in each:
            call = _http_target(reference, suite.input_key, values)
            target = _function_target(call, limit)
            if cache is not None:
                target = _cached_target(target, cache, _http_request(reference, suite.input_key, values))
            # A conversation's turn has the same time limit as a case's call.
            target.converse = _function_target(call.converse, limit)  # type: ignore[attr-defined]
            built.append(target)
        return built
    assert function is not None  # run_suite starts a function target's process first
    called: Callable[..., Any] = function
    if cache is not None and cache.functions:
        written = {
            "file_sha256": hashlib.sha256(_target_file(reference, base).read_bytes()).hexdigest(),
            "function": reference.function,
        }
        called = _cached_target(called, cache, lambda inputs: written | {"arguments": inputs})
    if not matrix:
        return [called]
    return [_with_arguments(called, combination.values) for combination in matrix]


def _python_check(check: CheckSpec, base: Path, modules: dict[Path, Any], *, replayed: bool = False) -> Any:
    """A python check's scorer: its function, imported from its file in this process, and checked
    as ``python_check`` checks it. ``modules`` holds the files this run imported already, so two
    checks that name one file share one import of it. ``replayed`` says the cache holds the
    target's calls: a replayed reply's trace records none of the call's spans, so a function that
    reads ``trace`` is refused. A conversation check is called once, after the turns, with no
    target call of its own, so one that reads ``trace`` is refused too."""
    from martian.evals import checks as deterministic

    file = _relative(check.file or "", base).resolve()
    try:
        if file not in modules:
            modules[file] = _custom.load_module(file, what="scorer", module=_custom.module_name(file))
        function = _custom.function_of(modules[file], check.function or "", what="scorer")
        if replayed and "trace" in _custom.arguments(function):
            raise ValueError(
                "it reads the trace of the target's call, and with the cache a call can be a reply an earlier run "
                "stored, whose trace records none of the call's spans; run it without the cache (--no-cache), or "
                "for a function target set cache.functions to false"
            )
        if check.scope == "conversation" and "trace" in _custom.arguments(function):
            raise ValueError(
                "a conversation check is called once, after the turns, and no target call of its own has a trace; "
                "read the conversation in inputs['messages'] and outputs, or read each turn's trace (scope: turn)"
            )
        return deterministic.python_check(
            function, name=check.label, min_score=check.min_score, max_score=check.max_score
        )
    except ValueError as error:
        raise ConfigError(f"check {check.label}: {error}") from None


def _build_scorers(suite: Suite, base: Path, cache: ModelCallCache | None = None) -> list[Any]:
    """The scorer of each check, its LLM judges with ``cache`` when the cache is on, a panel's
    members each with it. A python check's file is imported here, as the run starts, and never
    when the suite loads; a python check is the team's own code, not a model call, so the cache
    never holds its verdicts. Nor does it hold a conversation judge's: a run with the cache has no
    conversation cases (:func:`run_suite`), and they grade only those."""
    from martian.evals import conversation_judges, grounding, judges
    from martian.evals.panel import judge_panel

    built = []
    modules: dict[Path, Any] = {}
    replayed = cache is not None and _target_cached(suite, cache.functions)
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
        "task_completion": conversation_judges.task_completion,
        "policy_adherence": conversation_judges.policy_adherence,
        "knowledge_retention": conversation_judges.knowledge_retention,
    }
    settings = suite.judge or JudgeConfig()
    for check in suite.checks:
        if check.type in JUDGES:
            options: dict[str, Any] = {
                "name": check.label,
                "inference_params": dict(settings.inference_params) or None,
                "max_retries": settings.max_retries,
                "rate_limit_wait_seconds": settings.rate_limit_wait_seconds,
                "headers": judge_headers,
            }
            factory = factories[check.type]
            # A conversation judge reads the conversation a conversation check is given, not an input.
            if check.type not in CONVERSATION_JUDGES:
                options["input_key"] = suite.input_key
                options["cache"] = cache
            if check.panel is not None:
                built.append(judge_panel(factory, models=check.panel, **options))
            else:
                built.append(factory(model=settings.model, **options))
        elif check.type in CODE_CHECKS:
            built.append(_python_check(check, base, modules, replayed=replayed))
        else:
            built.append(_build_check(check))
    return built


def _build_check(check: CheckSpec) -> Any:
    """The deterministic check a suite file's entry names. ``CheckSpec.complete`` has made sure the
    options each type needs are there. An installed check is refused: only a worker that installs
    one can run it."""
    from martian.evals import checks as deterministic

    if check.type in INSTALLED_CHECKS:
        raise ConfigError(NOT_INSTALLED_HERE.format(label=check.label))
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
    tools: dict[str, Any] = {"source": check.source, "name": name}
    calls = {"expectation": check.expectation, "arguments_match": check.arguments_match or "subset", **tools}
    match check.type:
        case "tool_called":
            least = 1 if check.min_calls is None else check.min_calls
            return deterministic.tool_called(*(check.calls or ()), min_calls=least, max_calls=check.max_calls, **calls)
        case "tool_not_called":
            return deterministic.tool_not_called(*(check.calls or ()), **calls)
        case "tool_trajectory":
            return deterministic.tool_trajectory(*(check.calls or ()), order=check.order or "exact", **calls)
        case "step_count":
            return deterministic.step_count(
                min_turns=check.min_turns,
                max_turns=check.max_turns,
                min_calls=check.min_calls,
                max_calls=check.max_calls,
                **tools,
            )
    raise AssertionError(f"{check.type} is not a deterministic check")


def _with_suite_criteria(case: dict[str, Any], suite: Suite, scope: CheckScope | None = None) -> dict[str, Any]:
    """Fill in criteria given once in the suite file for rows that do not carry their own. ``scope``
    is what the row is graded as in a conversation case, a turn or the whole conversation, so only
    the checks of that scope give their criteria; a case of one input (None) takes every check's.
    In a suite with a ``task_completion`` judge, a simulated conversation's row for its conversation
    checks also has its user's ``goal`` when the case's expectations give none."""
    expectations = dict(case["expectations"])
    for check in suite.checks:
        key = CRITERIA_KEYS.get(check.type)
        if key is None or scope not in (None, check.scope):
            continue
        if (value := getattr(check, key)) is not None and key not in expectations:
            expectations[key] = value
    completes = any(check.type == "task_completion" for check in suite.checks)
    if completes and scope == "conversation" and "simulated_user" in case and "goal" not in expectations:
        expectations["goal"] = case["simulated_user"]["goal"]
    return expectations


def _missing(value: Any) -> bool:
    return value is None or value != value  # NaN


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _suite_fingerprint(suite: Suite, base: Path, matrix: Mapping[str, Any] | None = None) -> str:
    """Everything in the suite a verdict depends on: the checks and their criteria, the judge model
    and its parameters, the target, the values of the combination of the suite's matrix that
    ``matrix`` gives, and the SDK version that holds the judge prompts.

    Settings that change how a run is carried out but not what it grades (concurrency, batch size,
    output folder, retries and the rate-limit wait, timeout, the judge's headers, MLflow location)
    are left out, so changing them keeps earlier results. The judge's headers, like its key and
    address in the environment, say how its endpoint is reached. A function target and a python
    check count by the contents of their files; modules those files import and the service behind a
    URL target cannot be seen from here.

    With repeats, the attempts per case and the rule each check's attempts combine by count too.
    With one attempt every rule gives the same verdict, so neither counts and the fingerprint is
    the one a suite without repeats has. Without a matrix the fingerprint is the one it was before
    matrices existed. A check's scope counts only when it is ``conversation``, so a suite that
    names none keeps the fingerprint it had before scopes existed.
    """
    target: dict[str, Any] | None = None
    if suite.target is not None:
        target = suite.target.model_dump(mode="json", exclude={"timeout_seconds"})
        if suite.target.file is not None:
            file = Path(suite.target.file)
            file = file if file.is_absolute() else base / file
            target["file_sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
    checks = []
    for check in suite.checks:
        # A check without a panel, or of the default scope, leaves the field out, so its fingerprint
        # is the one it had before panels and scopes existed.
        left_out = ({"panel"} if check.panel is None else set()) | ({"scope"} if check.scope == "turn" else set())
        written = check.model_dump(mode="json", exclude={"combine"} | left_out)
        if check.type in CODE_CHECKS:
            written["file_sha256"] = hashlib.sha256(_relative(check.file or "", base).read_bytes()).hexdigest()
        checks.append(written)
    definition: dict[str, Any] = {
        "sdk": __version__,
        "checks": checks,
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
    if matrix is not None:
        definition["matrix"] = dict(matrix)
    return _digest(definition)


def _case_fingerprint(suite_fingerprint: str, case: dict[str, Any], suite: Suite | None = None) -> str:
    """What a case's verdict depends on besides the suite: its inputs, expectations and stored
    output, and a conversation's turns. A simulated conversation's depends on its simulated user
    and the suite's simulator too: the simulator's model and inference parameters and, with
    repeats, the suite's rule its goal's attempts combine by. A case without a simulated user
    keeps its fingerprint when a simulator is added."""
    found = {
        "suite": suite_fingerprint,
        "inputs": case["inputs"],
        "expectations": case["expectations"],
        "outputs": case.get("outputs"),
    }
    if "turns" in case:
        found["turns"] = case["turns"]
    if "simulated_user" in case:
        assert suite is not None and suite.simulator is not None  # the suite file loader refuses otherwise
        found["simulated_user"] = case["simulated_user"]
        found["simulator"] = {
            "model": suite.simulator.model,
            "inference_params": suite.simulator.inference_params,
            "version": SIMULATOR_VERSION,
            "combine": suite.combine if suite.repeats > 1 else None,
        }
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
    simulator: dict[str, Any] | None = None,
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
    nothing in, and a check that gave a turn no verdict is counted under that turn's reason.
    ``simulator`` (the simulator's setup, given when the run has simulated users: its model,
    inference parameters and version, and the data file its personas came from) adds ``simulator``:
    that setup, and the simulated cases' own counts and pass rate, so a reader sees how many of the
    run's verdicts a simulated user stands behind; the goal verdict
    (:data:`martian.evals.simulation.GOAL`) is then among ``labels``, a conversation check."""
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
    if simulator is not None:
        played = [(str(r["id"]), r["passed"]) for r in records if r.get("simulated") is True]
        summary["simulator"] = {
            **simulator,
            "cases": len(played),
            **_counts([passed for _, passed in played]),
            **_pass_rate(played, groups),
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


def _build_simulator(suite: Suite) -> SimulatedUsers:
    """The suite's simulator, its headers read from the environment as the judge's are."""
    assert suite.simulator is not None  # the suite file loader refuses a simulated user without one
    config = suite.simulator
    headers = {name: _from_environment(value, f"simulator.headers.{name}") for name, value in config.headers.items()}
    try:
        return SimulatedUsers(
            model=config.model,
            inference_params=dict(config.inference_params),
            max_retries=config.max_retries,
            rate_limit_wait_seconds=config.rate_limit_wait_seconds,
            headers=headers,
        )
    except ValueError as error:
        raise ConfigError(f"simulator: {error}") from None


def _conversation(case: dict[str, Any]) -> bool:
    """Whether a case is a conversation: scripted turns, or a simulated user."""
    return "turns" in case or "simulated_user" in case


def _turn_row(turn: Turn) -> dict[str, Any]:
    """A conversation turn as the runner reads it: its inputs, expectations and listed checks."""
    row: dict[str, Any] = {"inputs": dict(turn.inputs), "expectations": dict(turn.expectations)}
    if turn.checks is not None:
        row["checks"] = list(turn.checks)
    return row


MATRIX_SUMMARY = "matrix.json"
"""A matrix run's summary at the top of its output folder: one entry per combination."""


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
    baseline: str | Path | None = None,
    max_seconds: float | None = None,
    max_judge_tokens: int | None = None,
    cache: bool | None = None,
) -> dict[str, Any]:
    """Run the suite and return its summary. Writes results.jsonl, summary.json, junit.xml and
    verdicts.json, and records the run, its case results, scores and traces in the local edition's
    data folder (``home``, else ``MARTIAN_EVALS_HOME``, else ``~/.martian/evals``), where
    ``martian ui`` shows it (:mod:`martian.evals.run_record`). The returned summary also names that
    run, under ``run``; summary.json does not.

    ``max_seconds`` and ``max_judge_tokens`` replace the suite file's ``limits``.
    ``max_judge_tokens`` counts the tokens of the LLM judges and of the simulated users' replies
    together. A run that reaches one, or that is cancelled in ``martian ui``, calls no more
    targets: the cases it finished are kept, the others have no result (``not_run``), and running
    it again resumes after them. The summary's ``stopped`` says which limit it reached, and its
    ``tokens_this_run`` the tokens the judges and the simulator used. With repeats, a case is
    finished only when all of its attempts ran: one the run stopped in the middle of is not run,
    the attempts it did run are dropped (their judge and simulator tokens still count towards the
    limit), and running again runs all of its attempts.

    The suite is read through the shared model (:func:`martian.evals.suite_file.load_suite_file`),
    so a field the model has no place for is refused with the field named, never ignored.
    ``repeats`` replaces the suite file's ``repeats``: each case runs that many times, and each
    check's attempts combine by its rule (:mod:`martian.evals.repeats`).

    ``baseline`` names an earlier run's output folder. Its verdicts.json is read before any case
    runs, the run is compared with it case by case (:mod:`martian.evals.baseline`), and the
    comparison is written to comparison.json and returned under ``comparison``. Without one, a
    comparison.json left by an earlier run in the output folder is removed.

    A suite with a ``matrix`` runs once per combination (:mod:`martian.evals.matrix`), each in the
    folder of its id under the output folder, with that folder's files as above, recorded as its own
    run and compared with the baseline's result of the same combination. It then writes
    matrix.json, the summary of every combination (:func:`_matrix_summary`), and a verdicts.json
    that holds every combination's verdicts, for a later run's baseline, and returns the matrix
    summary with each combination's run under ``run``. A suite without a matrix removes a
    matrix.json an earlier run left in the output folder.

    ``cache`` turns the cache of model calls on (True) or off (False) for this run; None leaves it
    to the suite file, on when it has a ``cache`` block (:func:`_open_cache`). A run with the
    cache on says so in its summary's ``cache`` (:func:`_cache_report`), with how many replies
    came from it, and in its recorded run's ``provenance.cache``, also when it stopped at a limit.
    A replayed judgment made no call, so it spends none of ``max_judge_tokens``
    (:func:`martian.evals.limits.reported_tokens`). A suite whose cases include a conversation is
    refused with the cache on, before any case runs: the cache does not hold a conversation's turns.

    A conversation case is one case: ``limit`` counts it once, and each of its attempts is a whole
    conversation of its own. Its turns are sent in order, every conversation of a batch at turn
    one before any at turn two, and each turn is graded by the turn checks it names; the
    conversation checks then grade the replies (:mod:`martian.evals.conversations`). A simulated
    conversation's turns are written by the suite's simulator as it goes, each graded by every turn
    check against the case's expectations, and its goal verdict sits beside the conversation
    checks' (:mod:`martian.evals.simulation`); its record is marked ``simulated``.
    """
    from martian.evals import baseline as baselines
    from martian.evals.local import data_folder
    from martian.evals.model import RunLimits, suite_digest
    from martian.evals.suite_file import load_suite_file

    earlier = baselines.load(baseline) if baseline is not None else None
    model, model_cases = load_suite_file(path)
    suite, base = load_suite(path)
    if repeats is not None:
        if not 1 <= repeats <= MAX_ATTEMPTS:
            raise ConfigError(f"repeats must be from 1 to {MAX_ATTEMPTS}")
        suite = suite.model_copy(update={"repeats": repeats})
        model = model.model_copy(update={"repeats": model.repeats.model_copy(update={"attempts": repeats})})
        model = model.model_copy(update={"digest": suite_digest(model)})
    cases: list[dict[str, Any]] = [
        {
            "id": case.id,
            "inputs": dict(case.inputs),
            "expectations": dict(case.expectations),
            **({} if case.outputs is None else {"outputs": case.outputs}),
            **({"turns": [_turn_row(turn) for turn in case.turns]} if case.turns else {}),
            **({"simulated_user": case.simulated_user.model_dump()} if case.simulated_user else {}),
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
    if limits.max_judge_tokens is not None and not judges and not any("simulated_user" in case for case in cases):
        raise ConfigError(
            "max_judge_tokens counts the tokens of LLM judge checks and simulated users, and the run has neither"
        )
    matrix = combinations(suite.matrix)
    if matrix and suite.target is not None and suite.target.file is not None:
        for case in cases:
            if clash := sorted(set(case["inputs"]) & set(suite.matrix)):
                raise ConfigError(
                    f"case {case['id']} has an input named {clash[0]}, and the function target also takes the "
                    f"matrix's axis {clash[0]} as a keyword argument; rename the axis or the input"
                )
    cached = suite.cache is not None if cache is None else cache
    if cached and any(_conversation(case) for case in cases):
        raise ConfigError(
            "the data has conversation cases, and the cache of model calls does not hold a conversation's turns; "
            "run the suite without the cache (--no-cache)"
        )
    calls = _open_cache(suite, base, home) if cached else None
    folder = Path(output) if output is not None else base / suite.output
    say = progress or (lambda message: None)
    # A function target runs in one process of its own, which every combination of the matrix
    # calls with its values, and which cannot reach the suite file, its data or the results.
    process: _ProcessTarget | None = None
    if suite.target is not None and suite.target.file is not None:
        private = _private_paths(suite, Path(path).resolve(), base, folder, data_folder(home))
        loaded = _load_target(suite.target, base, suite.input_key, private)
        assert isinstance(loaded, _ProcessTarget)
        process = loaded
    try:
        targets = _targets(suite, base, matrix, calls, process)
        scorers = _build_scorers(suite, base, calls)
        hidden = process.ready() if process is not None else None
        if hidden is not None:
            say(f"{hidden}: the target can read the suite file, its data and its results")
        isolation = None if process is None else {"files_hidden": hidden is None, "reason": hidden}
        plan = _Plan(
            path=Path(path),
            base=base,
            suite=suite,
            model=model,
            groups={case.id: case.tags[GROUP_TAG] for case in model_cases if GROUP_TAG in case.tags},
            cases=cases,
            total=total,
            limit=limit,
            scorers=scorers,
            concurrency=concurrency,
            resume=resume,
            home=home,
            source=None if baseline is None else str(baseline),
            limits=limits,
            judges=frozenset(judges),
            cache=calls,
            isolation=isolation,
        )
        return _run_combinations(plan, matrix, targets, folder=folder, say=say, earlier=earlier)
    finally:
        if process is not None:
            process.close()


def _run_combinations(
    plan: _Plan,
    matrix: list[Combination],
    targets: list[Callable[..., Any] | None],
    *,
    folder: Path,
    say: Callable[[str], None],
    earlier: dict[str, Any] | None,
) -> dict[str, Any]:
    """The run of the suite into ``folder``, or of each combination of its matrix into a folder of
    its own, with matrix.json and the verdicts of every combination; the summary
    :func:`run_suite` returns."""
    from martian.evals import baseline as baselines

    suite = plan.suite
    if not matrix:
        found, _ = _run_one(
            plan,
            folder=folder,
            target=targets[0],
            say=say,
            earlier=None if earlier is None else baselines.result(earlier, None),
        )
        (folder / MATRIX_SUMMARY).unlink(missing_ok=True)
        return found

    from martian.evals import matrix as matrices

    count = len(matrix)
    say(f"{count} combinations of {_listed([axis['name'] for axis in matrices.axes(suite.matrix)])}")
    invocation = str(uuid.uuid4())
    started = time.monotonic()
    ran: list[tuple[Combination, dict[str, Any]]] = []
    verdicts: dict[str, dict[str, Any]] = {}
    for position, (combination, target) in enumerate(zip(matrix, targets, strict=True), 1):

        def told(message: str, prefix: str = f"{combination.id} ({position} of {count}): ") -> None:
            say(prefix + message)

        found, verdicts[combination.id] = _run_one(
            plan,
            folder=folder / combination.folder,
            target=target,
            say=told,
            earlier=None if earlier is None else baselines.result(earlier, combination.id),
            combination=combination,
            provenance=matrices.provenance(combination, suite.matrix, invocation),
        )
        ran.append((combination, found))
    summary = _matrix_summary(suite, ran, folder=folder, seconds=time.monotonic() - started)
    (folder / MATRIX_SUMMARY).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    bundle = json.dumps(baselines.matrix_verdicts(verdicts), indent=2) + "\n"
    (folder / baselines.VERDICTS).write_text(bundle, encoding="utf-8")
    runs = [found["run"] for _, found in ran]
    return summary | {
        "combinations": [entry | {"run": run} for entry, run in zip(summary["combinations"], runs, strict=True)]
    }


def _listed(names: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def combination_gate(summary: Mapping[str, Any], comparison: Mapping[str, Any] | None) -> str:
    """How one run of a suite, or one combination of its matrix, ends: ``error`` when a case has
    no verdict or the run stopped before one (``not_run``), else the comparison's gate when it was
    compared with a baseline, else ``failed`` when a case failed and ``passed`` otherwise.
    ``martian evals run`` exits 2, 1 or 0 by it."""
    if summary["errors"] or summary.get("not_run"):
        return "error"
    if comparison is not None:
        return str(comparison["gate"])
    return "failed" if summary["failed"] else "passed"


def _matrix_summary(
    suite: Suite, ran: list[tuple[Combination, dict[str, Any]]], *, folder: Path, seconds: float
) -> dict[str, Any]:
    """matrix.json: the matrix's ``axes`` (:func:`martian.evals.matrix.axes`), and one entry per
    combination in order with its ``id``, its ``values`` (each axis's value name), its ``folder``
    under the output folder, its ``gate`` (:func:`combination_gate`), its ``summary`` (its
    summary.json) and its ``comparison`` (its comparison.json, or None when it was not compared)."""
    from martian.evals import matrix as matrices

    entries = []
    for combination, found in ran:
        comparison = found.get("comparison")
        own = {key: value for key, value in found.items() if key not in ("run", "comparison")}
        entries.append(
            {
                "id": combination.id,
                "values": dict(combination.names),
                "folder": combination.folder,
                "gate": combination_gate(own, comparison),
                "summary": own,
                "comparison": comparison,
            }
        )
    return {
        "axes": matrices.axes(suite.matrix),
        "combinations": entries,
        "seconds_this_run": round(seconds, 1),
        "output": str(folder),
    }


@dataclass(frozen=True)
class _Plan:
    """What every combination of a run shares: the suite, its cases and checks, and the options."""

    path: Path
    base: Path
    suite: Suite
    model: Any  # martian.evals.model.Suite
    groups: dict[str, str]
    cases: list[dict[str, Any]]
    total: int
    limit: int | None
    scorers: list[Any]
    concurrency: int | None
    resume: bool
    home: str | Path | None
    source: str | None
    limits: Any  # martian.evals.model.RunLimits, the same for every combination
    judges: frozenset[str]
    cache: ModelCallCache | None = None
    # Whether a function target's process could hide the files that grade it (summary.json's
    # target_isolation), None for any other target.
    isolation: dict[str, Any] | None = None


def _target_cached(suite: Suite, functions: bool) -> bool:
    """Whether the cache holds the target's calls: a URL target's always, a function target's
    with ``functions``. A suite without a target calls none."""
    return suite.target is not None and (suite.target.url is not None or functions)


def _open_cache(suite: Suite, base: Path, home: str | Path | None) -> ModelCallCache:
    """The cache of model calls, as the suite file's ``cache`` sets it, or with its defaults: the
    folder ``cache`` in the local data folder, entries that live seven days, function targets left
    out. A check that measures the target's call is refused when the target's calls are cached,
    since a reply from the cache took no time and spent no tokens, and so is a tool-call check that
    reads the call's trace, since a replayed reply's trace records no tool call."""
    from martian.evals.cache import FOLDER
    from martian.evals.local import data_folder

    settings = suite.cache or CacheConfig()
    measured = [check.label for check in suite.checks if check.type in CALL_CHECKS]
    if measured and suite.target is not None and _target_cached(suite, settings.functions):
        url = suite.target.url is not None
        fix = "run it without the cache (--no-cache)" if url else "set cache.functions to false"
        raise ConfigError(
            f"{', '.join(measured)} measures the target's call, and with the cache a call can be a reply an "
            f"earlier run stored; {fix}"
        )
    traced = [check.label for check in suite.checks if check.type in TOOL_CHECKS and check.source != "reply"]
    if traced and suite.target is not None and _target_cached(suite, settings.functions):
        # Only a function target's: a URL target's tool-call checks read its reply (Suite).
        raise ConfigError(
            f"{', '.join(traced)} reads the tool calls in the trace of the target's call, and with the cache a call "
            "can be a reply an earlier run stored, whose trace records none; set cache.functions to false, or read "
            "the tool calls in the reply (source: reply)"
        )
    if settings.folder is None:
        folder = data_folder(home) / FOLDER
    else:
        folder = Path(settings.folder) if Path(settings.folder).is_absolute() else base / settings.folder
    return ModelCallCache(folder.resolve(), ttl_seconds=settings.ttl_seconds, functions=settings.functions)


def _cache_report(suite: Suite, cache: ModelCallCache) -> dict[str, Any]:
    """summary.json's ``cache``, and the recorded run's ``provenance.cache``: the cache's
    ``folder``, ``ttl_seconds`` and ``functions``, and this run's ``hits``, ``calls`` and
    ``stored`` for the ``target`` and the ``judge`` calls, each None when the suite makes no such
    call that the cache holds (no target or a function target it leaves out; no LLM judge)."""
    counts = cache.counts()
    judged = any(check.type in JUDGES for check in suite.checks)
    return cache.settings() | {
        "target": counts["target"] if _target_cached(suite, cache.functions) else None,
        "judge": counts["judge"] if judged else None,
    }


def combined_cache(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Several runs' cache reports as one, each kind's counts added up: a matrix's combinations."""
    combined = dict(reports[0])
    for kind in ("target", "judge"):
        if combined[kind] is not None:
            combined[kind] = {name: sum(report[kind][name] for report in reports) for name in combined[kind]}
    return combined


def cache_line(found: dict[str, Any]) -> str:
    """A run's use of the cache as one line, printed under the results table: of each kind of call,
    how many were replayed from the cache, and how many calls went to a model."""
    kinds = [kind for kind in ("target", "judge") if found[kind] is not None]
    if not kinds:
        return "Cache: on, but this suite makes no model call that the cache holds."
    shown = " and ".join(
        f"{found[kind]['hits']} of {found[kind]['hits'] + found[kind]['calls']} {kind} calls" for kind in kinds
    )
    sent = sum(found[kind]["calls"] for kind in kinds)
    made = f"{sent} model {'call' if sent == 1 else 'calls'} made"
    return f"Cache: {shown} replayed replies stored by an earlier run; {made}."


def _run_one(
    plan: _Plan,
    *,
    folder: Path,
    target: Callable[..., Any] | None,
    say: Callable[[str], None],
    earlier: dict[str, Any] | None,
    combination: Combination | None = None,
    provenance: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One run of the suite, or of one combination of its matrix, into ``folder``: its summary,
    with its run and its comparison, and its verdicts. ``earlier`` is the baseline's verdicts of
    this suite or combination, if the baseline has any."""
    from martian.evals import baseline as baselines
    from martian.evals.limits import Budget
    from martian.evals.local import data_folder
    from martian.evals.run_record import GradedCase, RunRecorder

    suite, base, cases, path = plan.suite, plan.base, plan.cases, plan.path
    repeated = suite.repeats > 1
    numbers = range(1, suite.repeats + 1)
    repeat_plan = suite.repeat_plan()
    rules: dict[str, CombineRule] = {label: rule["combine"] for label, rule in repeat_plan["checks"].items()}
    scorers = plan.scorers
    limits, judges = plan.limits, set(plan.judges)
    isolation = plan.isolation
    labels = [check.label for check in suite.checks]
    panelled = {check.label for check in suite.checks if check.panel is not None}
    scorer_of = dict(zip(labels, scorers, strict=True))
    scopes: dict[str, CheckScope] = {check.label: check.scope for check in suite.checks}
    turn_labels = [label for label in labels if scopes[label] == "turn"]
    whole_labels = [label for label in labels if scopes[label] == "conversation"]
    talking = any(_conversation(case) for case in cases)
    turn_target: Callable[..., Any] | None = None
    turn_reasons: dict[str, str] = {}
    accepted: tuple[str, ...] = ()
    if talking:
        assert suite.target is not None and target is not None  # the suite file loader refuses otherwise
        accepted = _conversation_inputs(suite.target, target)
    # A simulated conversation's goal verdict is a conversation-level result beside its checks',
    # combined over attempts by the suite's rule. Only conversations have one.
    simulator = _build_simulator(suite) if any("simulated_user" in case for case in cases) else None
    graded_labels = [*labels, GOAL] if simulator is not None else labels
    talk_rules = dict(rules)
    if simulator is not None:
        scopes[GOAL] = "conversation"
        repeat_plan["checks"][GOAL] = {"combine": suite.combine, "from": "suite"}
        talk_rules[GOAL] = suite.combine
    workers = plan.concurrency or suite.concurrency

    # Opened before the output folder changes, so a data folder that cannot be opened stops here.
    cache = plan.cache
    with RunRecorder(
        data_folder(plan.home),
        plan.model,
        limits=limits,
        matrix=provenance,
        cache=None if cache is None else cache.settings(),
    ) as recorder:
        if cache is not None:
            cache.begin()
        results = Results(folder, resume=plan.resume)
        suite_print = _suite_fingerprint(suite, base, None if combination is None else combination.values)
        prints = {case["id"]: _case_fingerprint(suite_print, case, suite) for case in cases}
        positions = {case["id"]: position for position, case in enumerate(cases)}
        # An earlier verdict is reused only when it was produced by exactly this suite and case.
        finished = {i: r for i, r in results.records().items() if r["passed"] is not None and i in prints}
        done = {i for i, r in finished.items() if r.get("fingerprint") == prints[i]}
        invalidated = len(finished) - len(done)
        todo = [case for case in cases if case["id"] not in done]
        each = f" of {suite.repeats} attempts each" if repeated else ""
        message = f"{len(cases)} cases{each}, {len(cases) - len(todo)} already done"
        if talking:
            talk_count = sum(_conversation(case) for case in cases)
            message += f"; {talk_count} {'is a conversation' if talk_count == 1 else 'are conversations'}"
        if simulator is not None:
            played = sum("simulated_user" in case for case in cases)
            message += f", {played} with a simulated user played by {simulator.model}"
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
                # A simulated turn's expectations are its case's.
                turn_expectations=tuple(turn["expectations"] for turn in case["turns"])
                if "turns" in case
                else (dict(case["expectations"]),) * case["simulated_user"]["max_turns"]
                if "simulated_user" in case
                else (),
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
            # A panel check's votes, from its feedback's metadata (martian.evals.panel).
            votes: dict[str, dict[str, str]] = {}
            assessments = frame["assessments"].iloc[position] if "assessments" in frame.columns else None
            for item in assessments if isinstance(assessments, list) else []:
                found = (item.get("metadata") or {}) if isinstance(item, dict) else {}
                panel = {key: str(value) for key, value in found.items() if str(key).startswith(PANEL_PREFIX)}
                if panel and item.get("assessment_name") in panelled:
                    votes[str(item["assessment_name"])] = panel
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
                if label in votes and not broke:
                    verdicts[label]["metadata"] = votes[label]
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
            turn checks, before any turn ``n + 1``; a conversation whose turn did not complete stops.
            A simulated user writes its turn ``n`` first, every one at once, and stops when it says
            so or when its budget is spent; then it says whether it reached its goal."""
            assert turn_target is not None
            by_id = {case["id"]: case for case in talks}
            # Each attempt's conversation: its session id, the messages so far, the turns sent with
            # their traces, and the number of the turn that did not complete, if one did not. A
            # simulated conversation also has the turn its user wrote next, the simulator's reply
            # that ended it, and why a turn or the goal verdict could not be had.
            states: dict[tuple[str, int], dict[str, Any]] = {
                (case["id"], number): {
                    "session_id": str(uuid.uuid4()),
                    "history": [],
                    "turns": [],
                    "traces": [],
                    "broken": None,
                    "next": None,
                    "ended": None,
                    "error": None,
                    "goal_error": None,
                }
                for case in talks
                for number in numbers
            }

            def most_turns(case: dict[str, Any]) -> int:
                """The most turns a conversation sends: its turns, or its simulated user's budget."""
                return case["simulated_user"]["max_turns"] if "simulated_user" in case else len(case["turns"])

            def live(key: tuple[str, int]) -> bool:
                """Whether a conversation goes on: no turn failed, and its simulated user has not ended it."""
                state = states[key]
                return state["broken"] is None and state["ended"] is None

            def simulated(final: bool) -> list[tuple[tuple[str, int], Step | str]]:
                """Each simulated conversation still talking that has sent fewer turns than its budget
                (or, ``final``, all of them), and its user's reply or why there is none."""
                assert simulator is not None
                asked = [
                    key
                    for key in states
                    if "simulated_user" in by_id[key[0]]
                    and live(key)
                    and (final or len(states[key]["turns"]) < most_turns(by_id[key[0]]))
                ]

                def ask(key: tuple[str, int]) -> Step | str:
                    user = SimulatedUser(**by_id[key[0]]["simulated_user"])
                    try:
                        return simulator.step(user, states[key]["history"], final=final)
                    except ValueError as error:  # the SDK's own message, naming the setting to check
                        return str(error)

                if not asked:
                    return []
                with ThreadPoolExecutor(max_workers=min(workers, len(asked))) as pool:
                    found = list(zip(asked, pool.map(ask, asked), strict=True))
                # Each reply's tokens count towards max_judge_tokens, as a judgment's do; a failed
                # call counts nothing.
                for _, step in found:
                    if isinstance(step, Step):
                        budget.spend(step.tokens, "simulator")
                return found

            def turn_at(key: tuple[str, int], index: int) -> dict[str, Any]:
                case = by_id[key[0]]
                found: dict[str, Any] = states[key]["next"] if "simulated_user" in case else case["turns"][index]
                return found

            for index in range(max(most_turns(case) for case in talks)):
                for key, step in simulated(final=False) if simulator is not None else []:
                    state = states[key]
                    if isinstance(step, str):
                        state["broken"], state["error"] = index + 1, step
                    elif step.done:
                        state["ended"] = step
                    else:
                        case = by_id[key[0]]
                        state["next"] = {
                            "inputs": {suite.input_key: step.message},
                            "expectations": case["expectations"],
                        }
                groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
                for case in talks:
                    if index >= most_turns(case):
                        continue
                    for number in numbers:
                        key = (case["id"], number)
                        if not live(key):
                            continue
                        turn = turn_at(key, index)
                        checked = tuple(turn_checks(turn.get("checks"), turn_labels))
                        groups.setdefault(checked, []).append(
                            {
                                "inputs": {
                                    **turn["inputs"],
                                    SESSION_ID: states[key]["session_id"],
                                    HISTORY: list(states[key]["history"]),
                                },
                                "expectations": _with_suite_criteria(turn, suite, "turn"),
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
                        state, turn = states[key], turn_at(key, index)
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
                        state, turn = states[key], turn_at(key, index)
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
            # A simulated user whose budget is spent says whether it reached its goal. Without that
            # verdict the goal has none, and the conversation's checks keep theirs.
            for key, step in simulated(final=True) if simulator is not None else []:
                if isinstance(step, str):
                    states[key]["goal_error"] = step
                else:
                    states[key]["ended"] = step
            # The conversation checks grade each finished conversation's replies at once.
            wholes: dict[tuple[str, int], dict[str, Any]] = {}
            data = [
                {
                    "inputs": {MESSAGES: state["history"]},
                    "outputs": replies(state["turns"]),
                    "expectations": _with_suite_criteria(by_id[case_id], suite, "conversation"),
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
                    # A conversation judge's tokens count towards max_judge_tokens as a turn judge's do.
                    if "assessments" in frame.columns:
                        for tokens in _judge_tokens(frame["assessments"].iloc[position], judges):
                            budget.spend(tokens)
            out: dict[tuple[str, int], tuple[dict[str, Any], list[Any]]] = {}
            for key, state in states.items():
                case = by_id[key[0]]
                conversation = {label: wholes.get(key, {}).get(label) for label in whole_labels}
                if "simulated_user" in case:
                    # Every turn check grades every simulated turn; a conversation that broke before
                    # any turn is graded by none, as its first turn did not complete.
                    graded_by = graded_turns([None] * max(1, len(state["turns"])), turn_labels)
                    conversation[GOAL] = goal_verdict(
                        state["ended"], sent=len(state["turns"]), budget=most_turns(case), error=state["goal_error"]
                    )
                else:
                    graded_by = graded_turns([turn.get("checks") for turn in case["turns"]], turn_labels)
                record = conversation_record(
                    state["turns"],
                    labels=graded_labels if "simulated_user" in case else labels,
                    graded=graded_by,
                    conversation=conversation,
                    session_id=state["session_id"],
                    messages=state["history"],
                    broken=state["broken"],
                    error=state["error"],
                )
                out[key] = (record, state["traces"])
            return out

        recorder.start(
            planned=len(cases),
            total=plan.total,
            limit=plan.limit,
            reused=[graded(case, finished[case["id"]]) for case in cases if case["id"] in done],
            repeats=repeat_plan if repeated else None,
            # Scripted and simulated alike: the Evals UI reads the turns route of a run with any.
            conversations=sum(_conversation(case) for case in cases),
            simulator=None if simulator is None else simulator.definition,
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
        os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = str(plan.concurrency or suite.concurrency)
        os.environ["MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION"] = "true"  # otherwise the first case runs twice
        started = time.monotonic()
        try:
            mlflow.set_tracking_uri(suite.mlflow.tracking_uri or f"sqlite:///{folder.resolve() / 'mlflow.db'}")
            mlflow.set_experiment(suite.mlflow.experiment or path.stem)
            for start in range(0, len(todo), suite.batch):
                # Limits are checked before each batch, and before each target call within one:
                # a batch's judges finish once it has started.
                if stopping():
                    break
                batch = todo[start : start + suite.batch]
                singles = [case for case in batch if not _conversation(case)]
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
                talks = [case for case in batch if _conversation(case)]
                talked = converse(talks) if talks else {}
                recorded: list[GradedCase] = []
                for case in batch:
                    if _conversation(case):
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
                            used = {label: rule for label, rule in talk_rules.items() if label in attempts[0]["checks"]}
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
                        if "simulated_user" in case:
                            record["simulated"] = True
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
        groups = plan.groups
        refusals = _false_refusals(
            suite.checks,
            ordered,
            {case["id"]: case["expectations"] for case in cases},
            {case["id"]: case["turns"] for case in cases if "turns" in case},
        )
        summary = _summary(
            ordered,
            graded_labels,
            time.monotonic() - started,
            invalidated,
            groups,
            repeat_plan if repeated else None,
            refusals,
            scopes if talking else None,
            None
            if simulator is None
            else {
                "model": simulator.model,
                "parameters": dict(simulator.params),
                "version": SIMULATOR_VERSION,
                # The persona source: the data file the simulated users came from, by the
                # dataset's digest the run's provenance has.
                "personas": {"data": plan.model.dataset_location, "digest": plan.model.dataset_digest},
            },
            not_run=len(not_run),
            stopped=stopped,
        )
        cached = None if cache is None else _cache_report(suite, cache)
        if cached is not None:
            summary["cache"] = cached
        # The tokens this run's model calls used, by who used them: None when one of the calls
        # reported none. The simulator's only in a run with simulated users.
        summary["tokens_this_run"] = {
            "judges": budget.used["judges"],
            **({} if simulator is None else {"simulator": budget.used["simulator"]}),
        }
        summary["output"] = str(folder)
        summary["target_isolation"] = isolation
        if not_run and cancelled.is_set():
            summary["cancelled"] = True
        (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        why = "the run was cancelled" if cancelled.is_set() else (stopped or {}).get("message", "")
        # Each combination is its own testsuite, named after the suite and the combination.
        name = path.stem if combination is None else f"{path.stem}[{combination.id}]"
        write_junit(ordered, folder / "junit.xml", suite_name=name, summary=summary, not_run=not_run, stopped=why)
        found = _verdicts(suite, ordered, cases, repeat_plan if repeated else None)
        (folder / baselines.VERDICTS).write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8")
        comparison = None
        if earlier is not None:
            comparison = baselines.compare(earlier, found, source=plan.source or "")
            text = json.dumps(comparison, indent=2) + "\n"
            (folder / baselines.COMPARISON).write_text(text, encoding="utf-8")
        else:
            (folder / baselines.COMPARISON).unlink(missing_ok=True)
        if cached is not None:
            recorder.used_cache(cached)
        if not not_run:
            recorder.finish()
        elif cancelled.is_set():
            recorder.cancel()
        else:
            assert stopped is not None  # only a limit or a cancel leaves a case not run
            recorder.stop(stopped)
    return {**summary, "run": recorder.reference(), **({"comparison": comparison} if comparison else {})}, found


def _verdicts(
    suite: Suite, records: list[dict[str, Any]], cases: list[dict[str, Any]], repeats: dict[str, Any] | None
) -> dict[str, Any]:
    """The run's verdicts file (:func:`martian.evals.baseline.verdicts`). A check's definition counts
    its judge for an LLM judge (for a panel, its members and the judge's settings but not
    ``judge.model``, which it does not use) and, with repeats, the rule its attempts combine by; a
    simulated user's goal verdict counts the simulator. A conversation case's digest counts its
    turns or its simulated user. The file names the suite's security checks, which a comparison
    with it as the candidate gates on case by case."""
    from martian.evals import baseline as baselines

    judge = (
        suite.judge.model_dump(mode="json", exclude={"max_retries", "rate_limit_wait_seconds", "headers"})
        if suite.judge
        else None
    )

    def judged_by(check: CheckSpec) -> dict[str, Any] | None:
        if check.type not in JUDGES or judge is None:
            return None
        return judge if check.panel is None else {key: value for key, value in judge.items() if key != "model"}

    checks = {
        check.label: baselines.digest(
            {
                # A check without a panel, or of the default scope, leaves the field out, as in the
                # suite's fingerprint, so a check keeps the digest it had before panels and scopes
                # existed.
                "check": check.model_dump(
                    mode="json",
                    exclude={"combine"}
                    | ({"panel"} if check.panel is None else set())
                    | ({"scope"} if check.scope == "turn" else set()),
                ),
                "judge": judged_by(check),
                "combine": repeats["checks"][check.label]["combine"] if repeats else None,
            }
        )
        for check in suite.checks
    }
    if any("simulated_user" in case for case in cases):
        assert suite.simulator is not None  # the suite file loader refuses a simulated user without one
        # The goal verdict is the simulator's, decided by its model, parameters and version.
        checks[GOAL] = baselines.digest(
            {
                "simulator": suite.simulator.model_dump(mode="json", include={"model", "inference_params"}),
                "version": SIMULATOR_VERSION,
                "combine": suite.combine if repeats else None,
            }
        )
    # With repeats a check's reason is how its attempts went under its rule, the SDK's own words.
    plain = {check.label for check in suite.checks if repeats or check.type in baselines.PLAIN_REASONS}
    digests = {
        case["id"]: baselines.case_digest(
            case["inputs"],
            case["expectations"],
            {key: case[key] for key in ("turns", "simulated_user") if key in case},
        )
        for case in cases
    }
    return baselines.verdicts(
        records,
        digests,
        checks=checks,
        plain=plain,
        security=suite.security_checks(),
        attempts=repeats["attempts"] if repeats else 1,
    )
