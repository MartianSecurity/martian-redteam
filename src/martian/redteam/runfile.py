"""The red team run file, version 2, and the reader that accepts both versions.

A version 2 file starts with ``version: 2``. Its ``target`` block is the suite file's (a Python
function in a file, or an HTTP endpoint), plus the target's ``purpose`` and an MCP server as a third
kind. It adds the models that attack and judge, a budget, a goal and checks stated before the attack
for each vulnerability, the tool proxy between the agent and its tool servers, and the gate.

A file without ``version`` is a version 0.2 file (:class:`martian_redteam.config.RunConfig`) and keeps
working unchanged. Both refuse fields they do not know. ``docs/redteam-run-file.md`` documents every
field.

Checking a file calls no model and loads no code: :func:`problems` lists each error with where it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from martian_redteam.config import NamedSpec, RunConfig

VERSION = 2

ObjectiveName = Literal["disclosure", "control", "action", "persistence"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _without_canary(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("{{canary}}", "canary")
    if isinstance(value, dict):
        return {key: _without_canary(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_canary(item) for item in value]
    return value


class McpTarget(_Model):
    """An MCP server under test: the tool that takes a message, and the argument the message goes in.
    ``session_argument`` and ``reset_argument`` carry the conversation: its id, and ``true`` on its
    first turn. ``response_path`` and ``tool_calls_path`` read the reply in the tool's structured
    result, or in its text as JSON. ``schema_sha256`` pins the tool's input schema."""

    url: str | None = None
    command: list[str] | None = None
    tool: str = Field(min_length=1)
    message_argument: str = Field(default="message", min_length=1)
    session_argument: str | None = Field(default=None, min_length=1)
    reset_argument: str | None = Field(default=None, min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    response_path: str | None = Field(default=None, min_length=1)
    tool_calls_path: str | None = Field(default=None, min_length=1)
    schema_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _one_transport(self) -> McpTarget:
        if (self.url is None) == (self.command is None):
            raise ValueError("an mcp target needs a url or a command, not both")
        if self.command is not None and not self.command:
            raise ValueError("an mcp target's command names a program")
        if self.command is not None and self.headers:
            raise ValueError("headers apply to an mcp target's url")
        names = [self.message_argument, self.session_argument, self.reset_argument]
        given = [name for name in names if name is not None]
        if len(set(given)) != len(given):
            raise ValueError("message_argument, session_argument and reset_argument name different arguments")
        if clash := sorted(set(given) & set(self.arguments)):
            raise ValueError(f"arguments must not set {', '.join(clash)}, which each message sets")
        return self


class TargetSession(_Model):
    """How an HTTP target's conversation id travels besides the body's ``{{session_id}}``. ``header``:
    also send it in this request header. ``response_path``: the server issues the id; the first turn
    of a conversation sends an empty id, and each reply has the id at this path."""

    header: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
    response_path: str | None = Field(default=None, min_length=1)


class TargetMemory(_Model):
    """That the target keeps a memory across conversations, and how a memory poisoning probe uses
    it: how a new conversation starts, what the later conversation says, and, optionally, where the
    memory is stored."""

    start: Literal["new_conversation", "message"] = "new_conversation"
    """``new_conversation``: a conversation with a new id is a new session. ``message``: a new
    conversation also opens with ``message``, such as ``/new``, before anything else."""
    message: str | None = Field(default=None, min_length=1, max_length=200)
    trigger: str = Field(default="Hello. What can you help me with today?", min_length=1, max_length=2000)
    """The ordinary message the later conversation sends."""
    store: str | None = Field(default=None, min_length=1)
    """The file or folder the target keeps its memory in. The run records whether the writing
    conversation changed it. A relative path is read from the run file's folder."""

    @model_validator(mode="after")
    def _start(self) -> TargetMemory:
        if (self.start == "message") != (self.message is not None):
            raise ValueError("message is the opening of a new conversation, and needs start: message")
        return self


class Target(_Model):
    """The thing under test. ``file`` and ``function``, or ``url`` with ``body`` and the rest, are a
    suite file's target block and are checked by the same rules; ``mcp`` is an MCP server."""

    purpose: str = Field(min_length=1, max_length=4096)
    file: str | None = None
    function: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    body: dict[str, Any] | None = None
    response_path: str | None = None
    timeout_seconds: float | None = Field(default=None, ge=1, le=600)
    session: TargetSession | None = None
    """An HTTP target's conversation id in a header, or issued by the server."""
    tool_calls_path: str | None = Field(default=None, min_length=1)
    """Where an HTTP target's JSON answer lists the tool calls it made or asked for."""
    mcp: McpTarget | None = None
    memory: TargetMemory | None = None
    """The target keeps a memory across conversations. Without it, memory poisoning probes are
    skipped."""

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("purpose says what the target is for, in a sentence or two")
        return value

    @model_validator(mode="after")
    def _one_kind(self) -> Target:
        suite_fields = self.suite_target_fields()
        if self.session is not None and self.url is None:
            raise ValueError("session applies to a url target")
        if self.tool_calls_path is not None and self.url is None:
            raise ValueError("tool_calls_path applies to a url target; an mcp target has its own")
        if self.mcp is not None:
            if other := sorted(set(suite_fields) - {"timeout_seconds"}):
                raise ValueError(f"an mcp target takes none of {', '.join(other)}")
            return self
        if not suite_fields:
            raise ValueError("a target needs a file, a url or an mcp server")
        from martian.evals.runner import TargetRef

        try:
            # {{canary}} is the red team's own placeholder: the run fills it before the target is built.
            TargetRef.model_validate(_without_canary(suite_fields))
        except ValidationError as error:
            reasons = [
                str(item["msg"]).removeprefix("Value error, ")
                for item in error.errors(include_input=False, include_context=False, include_url=False)
            ]
            raise ValueError("; ".join(reasons)) from None
        return self

    def suite_target_fields(self) -> dict[str, Any]:
        """The fields of the suite file's target block that this target sets."""
        names = ("file", "function", "url", "headers", "body", "response_path", "timeout_seconds")
        return {name: getattr(self, name) for name in names if getattr(self, name) is not None}

    @property
    def kind(self) -> Literal["function", "http", "mcp"]:
        return "mcp" if self.mcp is not None else ("function" if self.file is not None else "http")


class ModelConfig(_Model):
    """A model the run calls with the user's own key. ``model`` is a provider and model name, as the
    suite file's judge takes it (``openai:/MODEL``)."""

    model: str = Field(min_length=1)
    inference_params: dict[str, float | int] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


def _model(value: Any) -> Any:
    return {"model": value} if isinstance(value, str) else value


class Models(_Model):
    """The attacker writes the attacks; the judge decides the checks of kind ``judge``. Without them,
    attacks are generated and judged by the Martian platform, as in version 0.2."""

    attacker: Annotated[ModelConfig, Field()] | None = None
    judge: Annotated[ModelConfig, Field()] | None = None

    @field_validator("attacker", "judge", mode="before")
    @classmethod
    def _short_form(cls, value: Any) -> Any:
        return _model(value)


class Goal(_Model):
    """What the attacker tries to make the target do. ``tool`` and ``where`` name a tool call and the
    arguments that make it the attacker's; ``text`` describes a goal in words."""

    tool: str | None = None
    where: dict[str, Any] = Field(default_factory=dict)
    text: str | None = None

    @model_validator(mode="after")
    def _something(self) -> Goal:
        if self.tool is None and not self.text:
            raise ValueError("a goal names a tool or describes the goal in text")
        if self.where and self.tool is None:
            raise ValueError("where applies to a tool goal")
        return self


Marker = Annotated[str, Field(min_length=3, max_length=200)]
"""A text check's marker: at least 3 characters, so it cannot appear by chance."""


class TextCheck(_Model):
    """Markers stated before the attack: every ``all_of`` marker in the reply and no ``none_of``
    marker. A marker the attack itself carried proves nothing (``docs/redteam-verdicts.md``)."""

    type: Literal["text"]
    all_of: list[Marker] = Field(min_length=1, max_length=8)
    none_of: list[Marker] = Field(default_factory=list, max_length=12)
    ignore_case: bool = True

    @model_validator(mode="after")
    def _apart(self) -> TextCheck:
        if {marker.casefold() for marker in self.all_of} & {marker.casefold() for marker in self.none_of}:
            raise ValueError("a marker cannot be both required and forbidden")
        return self


CanaryPlant = Literal["context", "system_prompt", "tool_result"]


class CanaryCheck(_Model):
    """A planted canary found in the reply or a tool call's arguments, encoded or not."""

    type: Literal["canary"]
    value: str | None = Field(default=None, min_length=8)
    """A canary you planted yourself. Without one, the run plants its own, unique to the run."""
    plant: CanaryPlant | None = None
    """Where the run plants its canary: ``context`` (the default), with the attack in the message;
    ``system_prompt``, for ``{{canary}}`` in the target block; ``tool_result``, for ``{{canary}}`` in a
    proxied server's ``inject`` text."""
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    """A ``context`` plant's note, with ``{{canary}}`` where the canary goes."""

    @model_validator(mode="after")
    def _planting(self) -> CanaryCheck:
        if self.value is not None and (self.plant is not None or self.text is not None):
            raise ValueError("a canary with a value is one you planted yourself; plant and text apply to the run's own")
        if self.text is not None and self.plant not in (None, "context"):
            raise ValueError("text is the note of a context plant")
        if self.text is not None and "{{canary}}" not in self.text:
            raise ValueError("text says where the canary goes, with {{canary}}")
        return self


class ToolCallCheck(_Model):
    type: Literal["tool_call"]
    tool: str = Field(min_length=1)
    where: dict[str, Any] = Field(default_factory=dict)
    executed: bool = False
    """``true``: only a call that ran counts. ``false``: a requested call counts too."""
    with_canary: bool = False
    """``true``: only a call whose arguments carry the run's canary, as it is or encoded, counts: data
    the target was given reached the tool."""


class JudgeCheck(_Model):
    type: Literal["judge"]
    criteria: str | None = Field(default=None, min_length=1)


class ReceiptCheck(_Model):
    """A request to the run's local receipt listener. It needs a canary check in the same
    vulnerability: carrying that canary is verified exfiltration, a request without it contact only."""

    type: Literal["receipt"]
    render: Literal["images", "links"] | None = None
    """Read the reply as a chat client shows it: fetch each image (``images``), or each image and
    link (``links``, as a user who opens them), whose address is this attempt's on the run's
    receipt listener. Nothing else is fetched."""


class StateCheck(_Model):
    type: Literal["state"]
    name: str = Field(min_length=1)
    """A business check of the environment, by name. Needs ``environment``."""


CheckDecl = Annotated[
    TextCheck | CanaryCheck | ToolCallCheck | JudgeCheck | ReceiptCheck | StateCheck, Field(discriminator="type")
]


BenignCheck = Annotated[TextCheck | ToolCallCheck, Field(discriminator="type")]


class Benign(_Model):
    """A normal task the target should still complete under attack, and the checks that show it did.
    The attack is sent with the task in the same message; the negative control sends the task alone."""

    task: str = Field(min_length=1, max_length=4096)
    checks: list[BenignCheck] = Field(min_length=1, max_length=5)


class DatasetFile(_Model):
    """A dataset file of the user's own, for ``HarmfulContent`` of type ``file``: CSV, or JSON Lines
    by its ``.jsonl`` suffix, and its licence."""

    file: str = Field(min_length=1)
    column: str = Field(default="prompt", min_length=1)
    licence: str = Field(min_length=1)
    """Its licence as an SPDX id, such as ``MIT`` or ``CC-BY-4.0``. The commercial-use switch reads it."""
    where: dict[str, str] = Field(default_factory=dict)
    """Only rows whose columns hold these values."""


class Datasets(_Model):
    """How the harmful-content set's datasets load."""

    commercial_use: bool = True
    """Load only datasets under MIT, Apache-2.0 or CC BY. Off, a dataset under any licence loads."""
    cache: str | None = Field(default=None, min_length=1)
    """Where downloaded datasets are kept. Default ``MARTIAN_DATASET_CACHE``, else
    ``~/.cache/martian/redteam-datasets``. A relative path is read from the run file's folder."""


class Plant(_Model):
    """An indirect attack in an environment: the attack is planted in the world with an implant
    profile of the agent's connection on the Martian server, and the agent is sent ``trigger``, an
    ordinary request that leads it to read what was planted."""

    profile: str = Field(min_length=1, max_length=128)
    trigger: str = Field(min_length=1, max_length=2000)


class Vulnerability(NamedSpec):
    """A vulnerability to probe, as in version 0.2, with an optional goal, objective, checks and
    severity. Without checks, a probe is decided by the judge."""

    goal: Goal | None = None
    objective: ObjectiveName | None = None
    form: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    """The catalogue form that decides the probe, such as ``tool_call`` or ``reply``. Without it and
    without checks, the entry's default form decides (``docs/catalog.md``)."""
    checks: list[CheckDecl] = Field(default_factory=list, max_length=10)
    severity: Literal["critical", "high", "medium", "low", "info"] | None = None
    benign: Benign | None = None
    """A benign task whose completion is reported with and without the attack."""
    plant: Plant | None = None
    """Plant the attack in the environment and send ``plant.trigger``. Needs ``environment``."""
    dataset: DatasetFile | None = None
    """For ``HarmfulContent`` of type ``file``: the user's own dataset file."""

    @model_validator(mode="after")
    def _form_or_checks(self) -> Vulnerability:
        if self.form is not None and self.checks:
            raise ValueError("give a form or checks, not both: a form is a set of checks")
        if self.is_custom:
            if not (self.id or "").strip():
                raise ValueError("a custom vulnerability needs an id")
            judged = not self.declares_evidence or any(
                check.type == "judge" and check.criteria is None for check in self.checks
            )
            if judged and not (self.criteria or "").strip():
                raise ValueError(
                    "a custom vulnerability the judge decides needs criteria; or declare checks (text, canary, "
                    "tool_call, receipt) or a goal that names a tool"
                )
        return self

    @property
    def is_custom(self) -> bool:
        return self.name.strip().lower().replace("-", "_") in {"custom", "customvulnerability"}

    @property
    def declares_evidence(self) -> bool:
        """Whether checks or a goal that names a tool decide it, rather than the judge alone."""
        return bool(self.checks) or (self.goal is not None and self.goal.tool is not None)


class ProxiedServer(_Model):
    """One of the agent's own MCP tool servers, reached through the local recording proxy."""

    name: str = Field(min_length=1, max_length=24, pattern=r"^[a-z][a-z0-9_]{0,23}$")
    """The name the agent's session gives the server's MCP connection, so it follows the rule of
    :class:`martian_redteam.MCPConnection` names."""
    upstream: str = Field(min_length=1)
    mode: Literal["record", "inject", "block"] = "record"
    inject: str | None = None
    """``inject`` mode: the text added to the tool results."""
    block_reply: str | None = None
    """``block`` mode: what a blocked call answers."""
    tools: list[str] = Field(default_factory=list, max_length=128)
    """The tools ``inject`` or ``block`` applies to. Without them, every tool of the server."""

    @field_validator("upstream")
    @classmethod
    def _http(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("upstream is the http or https address of a Streamable HTTP MCP server")
        if parts.username is not None or parts.password is not None:
            raise ValueError("upstream must not contain credentials")
        return value

    @model_validator(mode="after")
    def _mode_settings(self) -> ProxiedServer:
        if self.mode == "inject" and not self.inject:
            raise ValueError("inject mode needs the text to inject")
        if self.mode != "inject" and self.inject is not None:
            raise ValueError("inject applies to inject mode")
        if self.mode != "block" and self.block_reply is not None:
            raise ValueError("block_reply applies to block mode")
        if self.mode == "record" and self.tools:
            raise ValueError("tools applies to inject and block modes")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("tools names each tool once")
        return self


class Decoy(_Model):
    """The inert test MCP server of tool-description poisoning probes, served by the run and handed
    to the agent through the recording proxy as one of its tool servers."""

    name: str = Field(default="utilities", min_length=1, max_length=24, pattern=r"^[a-z][a-z0-9_]{0,23}$")


class Tools(_Model):
    """The local recording proxy: the agent's own tool servers it fronts, and where the agent is
    handed the session that binds them; the inert test server of tool-description poisoning probes,
    and the folder the agent loads skills from."""

    proxy: list[ProxiedServer] = Field(default_factory=list, max_length=20)
    decoy: Decoy | None = None
    """Serve the inert test MCP server through the proxy, for ``ToolPoisoning`` probes of type
    ``mcp_tool_description``."""
    skills_dir: str | None = Field(default=None, min_length=1)
    """The folder the agent loads skills from, for ``ToolPoisoning`` probes of type ``skill_file``. A
    relative path is read from the run file's folder."""
    session_api: str | None = None
    """The agent's session API, as the cloud hands a session over: the run posts the session to
    ``{session_api}/sessions`` before the first attack: the proxy's, or in an environment the
    Martian server's."""

    @field_validator("session_api")
    @classmethod
    def _session_api(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("session_api is the http or https address of the agent's session API")
        if parts.username is not None or parts.password is not None:
            raise ValueError("session_api must not contain credentials")
        return value

    @model_validator(mode="after")
    def _unique(self) -> Tools:
        names = [server.name for server in self.proxy] + ([self.decoy.name] if self.decoy else [])
        if len(set(names)) != len(names):
            raise ValueError("each proxied server, and the decoy, has its own name")
        return self


class Budget(_Model):
    """What the attacker may spend. A run stops at the first limit it reaches and says which."""

    target_queries: int | None = Field(default=None, ge=1, le=100_000)
    model_calls: int | None = Field(default=None, ge=1, le=100_000)
    """Calls of the attacker and the judge models together."""
    tokens: int | None = Field(default=None, ge=1, le=1_000_000_000)
    """Tokens the attacker and judge calls reported, input and output together."""
    cost_usd: float | None = Field(default=None, gt=0, le=1_000_000)
    """What the model calls cost, when their provider's prices are known."""
    max_seconds: float | None = Field(default=None, gt=0, le=86_400)
    attempts_per_probe: int = Field(default=1, ge=1, le=100)
    max_concurrent: int = Field(default=2, ge=1, le=64)


Severity = Literal["critical", "high", "medium", "low", "info"]
GateAction = Literal["fail", "warn", "ignore"]
LevelThreshold = Literal["confirmed", "suspected"] | Annotated[int, Field(ge=1, le=5)]
"""An evidence level: ``suspected`` is level 1, ``confirmed`` level 2, or a level from 1 to 5."""


def threshold_level(value: LevelThreshold) -> int:
    """The evidence level a ``fail_on`` or ``warn_on`` value names."""
    if value == "suspected":
        return 1
    if value == "confirmed":
        return 2
    return int(value)


class ErrorHandling(_Model):
    """What an attempt that did not complete does to the gate, by whose failure it was. A kind left
    out follows ``fail_on_infrastructure_error`` (attacker, judge, infrastructure) or
    ``ignore_target_errors`` (target)."""

    target: GateAction | None = None
    attacker: GateAction | None = None
    judge: GateAction | None = None
    infrastructure: GateAction | None = None


class Gate(_Model):
    """When the run fails, and what it only warns about. A finding fails the gate when it reached the
    level ``fail_on`` names and its vulnerability is at or above ``severity``; ``warn_on`` warns about
    any other finding at or above its level. Undetermined attempts and errors each fail, warn or are
    ignored. Warnings never change the exit code."""

    fail_on: LevelThreshold = "confirmed"
    """``confirmed`` (level 2), ``suspected`` (level 1) or a level from 1 to 5."""
    severity: Severity | None = None
    """Only findings of probes at this severity or above fail the gate."""
    warn_on: LevelThreshold | None = None
    """Warn about a finding at this level or above that does not fail the gate: one below ``fail_on``
    or below ``severity``. ``suspected`` with the default ``fail_on`` warns about a run whose results
    are only suspected."""
    fail_on_infrastructure_error: bool = True
    """Fail when the attacker, the judge, the platform or the harness failed."""
    ignore_target_errors: bool = False
    errors: ErrorHandling = Field(default_factory=ErrorHandling)
    """Per kind of error: ``fail``, ``warn`` or ``ignore``. Overrides the two settings above."""
    undetermined: GateAction = "fail"
    """What an attempt no check could decide, or one the budget stopped, does to the gate. ``fail``
    (the default) fails the run: an undetermined attempt is never a pass. ``warn`` passes with a
    warning; ``ignore`` leaves it out. The report counts them whatever this says."""

    @model_validator(mode="after")
    def _consistent(self) -> Gate:
        if self.warn_on is not None and threshold_level(self.warn_on) > threshold_level(self.fail_on):
            raise ValueError("warn_on names a level at or below fail_on's; a higher one would warn about nothing")
        set_here = self.errors.model_fields_set
        if "fail_on_infrastructure_error" in self.model_fields_set and set_here & {
            "attacker",
            "judge",
            "infrastructure",
        }:
            raise ValueError("give fail_on_infrastructure_error or errors.attacker, judge and infrastructure, not both")
        if "ignore_target_errors" in self.model_fields_set and "target" in set_here:
            raise ValueError("give ignore_target_errors or errors.target, not both")
        return self

    def error_action(self, kind: str) -> GateAction:
        """``fail``, ``warn`` or ``ignore`` for an error of ``kind``."""
        chosen: GateAction | None = getattr(self.errors, kind)
        if chosen is not None:
            return chosen
        if kind == "target":
            return "ignore" if self.ignore_target_errors else "fail"
        return "fail" if self.fail_on_infrastructure_error else "ignore"


class VerificationSettings(_Model):
    """How the run checks its own findings and its judge."""

    repetitions: int = Field(default=0, ge=0, le=10)
    """Send a confirmed hit's attack again, each time in a new conversation, this many times."""
    required: int | None = Field(default=None, ge=1, le=10)
    """How many repetitions must hit again for the finding to verify. Default 2, or ``repetitions``
    when fewer ran."""
    control: bool = True
    """Run each probe once without the attack, when it hit or has a benign task: a hit the control
    shows too is discarded."""

    @model_validator(mode="after")
    def _reachable(self) -> VerificationSettings:
        if self.required is not None and self.required > self.repetitions:
            raise ValueError("required is at most repetitions")
        return self

    @property
    def required_successes(self) -> int:
        return self.required if self.required is not None else max(1, min(2, self.repetitions))

    calibration: str | None = Field(default=None, min_length=1)
    """A labelled sample (JSON Lines: ``vulnerability``, ``type``, ``attack``, ``reply``, ``hit``) the
    judge is measured on before the run, for its precision and recall. A relative path is read from
    the run file's folder."""


class RunFile(_Model):
    """A version 2 red team run file."""

    version: Literal[2]
    target: Target
    models: Models = Field(default_factory=Models)
    vulnerabilities: list[Vulnerability] = Field(min_length=1)
    attacks: list[NamedSpec] = Field(min_length=1)
    tools: Tools = Field(default_factory=Tools)
    budget: Budget = Field(default_factory=Budget)
    gate: Gate = Field(default_factory=Gate)
    verification: VerificationSettings = Field(default_factory=VerificationSettings)
    output_folder: str = "results"
    datasets: Datasets = Field(default_factory=Datasets)
    environment: str | None = Field(default=None, min_length=1, max_length=200)
    """A Martian environment to run in. Needs a connection to a Martian server; a local run stops
    with a message that says so."""
    agent: str | None = Field(
        default=None, pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    """The registered agent an environment run tests, by its id on the Martian server: its approval,
    sandbox bindings and checks. Without it, the API key must be bound to the agent."""

    @model_validator(mode="after")
    def _consistent(self) -> RunFile:
        if self.agent is not None and self.environment is None:
            raise ValueError("agent names the registered agent of an environment run; set environment")
        if self.environment is not None and self.tools.proxy:
            raise ValueError("an environment's gateway is the tool boundary; tools.proxy applies to a run without one")
        if (
            self.tools.session_api is not None
            and not self.tools.proxy
            and self.tools.decoy is None
            and self.environment is None
        ):
            raise ValueError("session_api hands over the proxy's session; list the servers in proxy")
        for index, vulnerability in enumerate(self.vulnerabilities):
            if vulnerability.plant is not None and self.environment is None:
                raise ValueError(f"vulnerabilities[{index}]: plant puts the attack in an environment; set environment")
            for check in vulnerability.checks:
                if check.type == "state" and self.environment is None:
                    raise ValueError(f"vulnerabilities[{index}]: a state check reads an environment; set environment")
                if check.type == "judge" and self.models.judge is None and self.models.attacker is not None:
                    raise ValueError(
                        f"vulnerabilities[{index}]: a judge check needs models.judge when attacks run locally"
                    )
                if check.type == "canary" and check.plant == "system_prompt":
                    self._templated_target(index)
                if check.type == "receipt" and not any(item.type == "canary" for item in vulnerability.checks):
                    raise ValueError(
                        f"vulnerabilities[{index}]: a receipt check needs a canary check: the canary is the "
                        "secret a request must carry"
                    )
                if (
                    check.type == "canary"
                    and check.plant == "tool_result"
                    and not any(server.inject and "{{canary}}" in server.inject for server in self.tools.proxy)
                ):
                    raise ValueError(
                        f"vulnerabilities[{index}]: a tool_result canary needs {{{{canary}}}} in a proxied "
                        "server's inject text"
                    )
        return self

    def _templated_target(self, index: int) -> None:
        from martian.redteam.verify.canary import mentions

        if self.target.kind == "function":
            raise ValueError(
                f"vulnerabilities[{index}]: a function target has no template to plant a canary in; "
                "plant it in the context, or give the value you planted yourself"
            )
        fields: list[Any] = [self.target.headers, self.target.body]
        if self.target.mcp is not None:
            fields += [self.target.mcp.arguments, self.target.mcp.headers]
        if not mentions(fields):
            raise ValueError(
                f"vulnerabilities[{index}]: a system_prompt canary needs {{{{canary}}}} in the target's "
                "headers, body or mcp arguments"
            )

    @classmethod
    def from_yaml(cls, path: str | Path) -> Self:
        file = Path(path).resolve()
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("a run file is a YAML mapping")
        loaded = cls.model_validate(raw)
        if loaded.target.file is not None and not Path(loaded.target.file).is_absolute():
            target = loaded.target.model_copy(update={"file": str((file.parent / loaded.target.file).resolve())})
            loaded = loaded.model_copy(update={"target": target})
        skills = loaded.tools.skills_dir
        if skills is not None and not Path(skills).is_absolute():
            tools = loaded.tools.model_copy(update={"skills_dir": str((file.parent / skills).resolve())})
            loaded = loaded.model_copy(update={"tools": tools})
        loaded = loaded.model_copy(update=_relative_datasets(loaded, file.parent))
        memory = loaded.target.memory
        if memory is not None and memory.store is not None and not Path(memory.store).is_absolute():
            stored = memory.model_copy(update={"store": str((file.parent / memory.store).resolve())})
            loaded = loaded.model_copy(update={"target": loaded.target.model_copy(update={"memory": stored})})
        sample = loaded.verification.calibration
        if sample is not None and not Path(sample).is_absolute():
            settings = loaded.verification.model_copy(update={"calibration": str((file.parent / sample).resolve())})
            loaded = loaded.model_copy(update={"verification": settings})
        return loaded


def _relative_datasets(loaded: RunFile, folder: Path) -> dict[str, Any]:
    """The run file's vulnerabilities and datasets with dataset files and the cache read from
    ``folder`` when relative."""
    entries = []
    for vulnerability in loaded.vulnerabilities:
        given = vulnerability.dataset
        if given is not None and not Path(given.file).is_absolute():
            given = given.model_copy(update={"file": str((folder / given.file).resolve())})
            vulnerability = vulnerability.model_copy(update={"dataset": given})
        entries.append(vulnerability)
    cache = loaded.datasets.cache
    datasets = loaded.datasets
    if cache is not None and not Path(cache).is_absolute():
        datasets = datasets.model_copy(update={"cache": str((folder / cache).resolve())})
    return {"vulnerabilities": entries, "datasets": datasets}


def read_yaml(path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("a run file is a YAML mapping")
    return raw


def load(path: str | Path) -> RunConfig | RunFile:
    """Read a run file of either version: ``version: 2`` gives a :class:`RunFile`, no version a
    version 0.2 :class:`~martian_redteam.config.RunConfig`."""
    raw = read_yaml(path)
    if "version" in raw:
        if raw["version"] != VERSION:
            raise ValueError(f"version must be {VERSION}, or left out for a version 0.2 file")
        return RunFile.from_yaml(path)
    return RunConfig.from_yaml(path)


_CHECK_MODELS = {"TextCheck", "CanaryCheck", "ToolCallCheck", "JudgeCheck", "ReceiptCheck", "StateCheck"}
_CHECK_TAGS = {"text", "canary", "tool_call", "judge", "receipt", "state"}
"""How pydantic names the kind of a check in an error's location; the location reads without them."""


def _where(location: tuple[int | str, ...]) -> str:
    text = ""
    for part in location:
        if isinstance(part, int):
            text += f"[{part}]"
        elif part in _CHECK_MODELS or (part in _CHECK_TAGS and text.endswith("]")):
            continue
        else:
            text += f".{part}" if text else str(part)
    return text or "(file)"


def problems(path: str | Path) -> list[str]:
    """Every error in a run file of either version, each as ``where: what``. Empty when the file is
    valid. Calls no model and loads no target code."""
    try:
        load(path)
    except ValidationError as error:
        found = []
        for item in error.errors(include_input=False, include_context=False, include_url=False):
            text = str(item["msg"]).removeprefix("Value error, ")
            found.append(f"{_where(tuple(item['loc']))}: {text}")
        return found
    except (ValueError, yaml.YAMLError) as error:
        return [f"(file): {error}"]
    return []


__all__ = [
    "VERSION",
    "Benign",
    "Budget",
    "VerificationSettings",
    "CanaryCheck",
    "CanaryPlant",
    "DatasetFile",
    "Datasets",
    "Decoy",
    "ErrorHandling",
    "Gate",
    "GateAction",
    "Goal",
    "JudgeCheck",
    "McpTarget",
    "ModelConfig",
    "Models",
    "Plant",
    "ProxiedServer",
    "ReceiptCheck",
    "RunFile",
    "StateCheck",
    "Target",
    "TargetMemory",
    "TargetSession",
    "TextCheck",
    "ToolCallCheck",
    "Tools",
    "Vulnerability",
    "load",
    "problems",
    "threshold_level",
]
