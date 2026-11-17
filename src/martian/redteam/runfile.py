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
            TargetRef.model_validate(suite_fields)
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


class TextCheck(_Model):
    type: Literal["text"]
    all_of: list[str] = Field(min_length=1, max_length=8)
    none_of: list[str] = Field(default_factory=list, max_length=12)
    ignore_case: bool = True


class CanaryCheck(_Model):
    type: Literal["canary"]
    value: str | None = Field(default=None, min_length=8)
    """The canary to plant. Without one, each run plants its own."""


class ToolCallCheck(_Model):
    type: Literal["tool_call"]
    tool: str = Field(min_length=1)
    where: dict[str, Any] = Field(default_factory=dict)
    executed: bool = False
    """``true``: only a call that ran counts. ``false``: a requested call counts too."""


class JudgeCheck(_Model):
    type: Literal["judge"]
    criteria: str | None = Field(default=None, min_length=1)


class ReceiptCheck(_Model):
    type: Literal["receipt"]


class StateCheck(_Model):
    type: Literal["state"]
    name: str = Field(min_length=1)
    """A business check of the environment, by name. Needs ``environment``."""


CheckDecl = Annotated[
    TextCheck | CanaryCheck | ToolCallCheck | JudgeCheck | ReceiptCheck | StateCheck, Field(discriminator="type")
]


class Vulnerability(NamedSpec):
    """A vulnerability to probe, as in version 0.2, with an optional goal, objective, checks and
    severity. Without checks, a probe is decided by the judge."""

    goal: Goal | None = None
    objective: ObjectiveName | None = None
    checks: list[CheckDecl] = Field(default_factory=list, max_length=10)
    severity: Literal["critical", "high", "medium", "low", "info"] | None = None


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


class Tools(_Model):
    """The local recording proxy: the agent's own tool servers it fronts, and where the agent is
    handed the session that binds them."""

    proxy: list[ProxiedServer] = Field(default_factory=list, max_length=20)
    session_api: str | None = None
    """The agent's session API, as the cloud hands a session over: the run posts the session to
    ``{session_api}/sessions`` before the first attack."""

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
        names = [server.name for server in self.proxy]
        if len(set(names)) != len(names):
            raise ValueError("each proxied server has its own name")
        if self.session_api is not None and not self.proxy:
            raise ValueError("session_api hands over the proxy's session; list the servers in proxy")
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


class AttackSpec(NamedSpec):
    """An attack method of a version 2 run: by ``name``, as in version 0.2, with the transforms to
    apply on top of what it sends (``layers``) and its own settings. ``id`` renames the method in
    probe ids, so one method can run twice with other settings. ``docs/redteam-methods.md`` lists
    each method and the settings it takes."""

    layers: list[str] = Field(default_factory=list, max_length=4)
    """Transforms applied, in order, to every attack the method sends, such as ``[Base64]``."""


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


class RunFile(_Model):
    """A version 2 red team run file."""

    version: Literal[2]
    target: Target
    models: Models = Field(default_factory=Models)
    vulnerabilities: list[Vulnerability] = Field(min_length=1)
    attacks: list[AttackSpec] = Field(min_length=1)
    tools: Tools = Field(default_factory=Tools)
    budget: Budget = Field(default_factory=Budget)
    gate: Gate = Field(default_factory=Gate)
    output_folder: str = "results"
    environment: str | None = Field(default=None, min_length=1)
    """A Martian environment to run in. Needs a connection to a Martian server; a local run stops
    with a message that says so."""

    @model_validator(mode="after")
    def _consistent(self) -> RunFile:
        from martian.redteam.methods.registry import MethodError, resolve

        for index, attack in enumerate(self.attacks):
            try:
                resolve(attack)
            except MethodError as error:
                raise ValueError(f"attacks[{index}]: {error}") from None
        for index, vulnerability in enumerate(self.vulnerabilities):
            for check in vulnerability.checks:
                if check.type == "state" and self.environment is None:
                    raise ValueError(f"vulnerabilities[{index}]: a state check reads an environment; set environment")
                if check.type == "judge" and self.models.judge is None and self.models.attacker is not None:
                    raise ValueError(
                        f"vulnerabilities[{index}]: a judge check needs models.judge when attacks run locally"
                    )
        return self

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
        return loaded


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
    "AttackSpec",
    "Budget",
    "CanaryCheck",
    "ErrorHandling",
    "Gate",
    "GateAction",
    "Goal",
    "JudgeCheck",
    "McpTarget",
    "ModelConfig",
    "Models",
    "ProxiedServer",
    "ReceiptCheck",
    "RunFile",
    "StateCheck",
    "Target",
    "TargetSession",
    "TextCheck",
    "ToolCallCheck",
    "Tools",
    "Vulnerability",
    "load",
    "problems",
    "threshold_level",
]
