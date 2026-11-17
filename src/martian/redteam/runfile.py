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

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from martian_redteam.config import NamedSpec, RunConfig

VERSION = 2

ObjectiveName = Literal["disclosure", "control", "action", "persistence"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class McpTarget(_Model):
    """An MCP server under test: the tool that takes a message, and the argument the message goes in."""

    url: str | None = None
    command: list[str] | None = None
    tool: str = Field(min_length=1)
    message_argument: str = Field(default="message", min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_transport(self) -> McpTarget:
        if (self.url is None) == (self.command is None):
            raise ValueError("an mcp target needs a url or a command, not both")
        if self.command is not None and not self.command:
            raise ValueError("an mcp target's command names a program")
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
        if self.mcp is not None:
            if suite_fields:
                raise ValueError(f"an mcp target takes none of {', '.join(sorted(suite_fields))}")
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

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    upstream: str = Field(min_length=1)
    mode: Literal["record", "inject", "block"] = "record"
    inject: str | None = None
    """``inject`` mode: the text added to the tool results."""
    block_reply: str | None = None
    """``block`` mode: what a blocked call answers."""

    @model_validator(mode="after")
    def _mode_settings(self) -> ProxiedServer:
        if self.mode == "inject" and not self.inject:
            raise ValueError("inject mode needs the text to inject")
        if self.mode != "inject" and self.inject is not None:
            raise ValueError("inject applies to inject mode")
        if self.mode != "block" and self.block_reply is not None:
            raise ValueError("block_reply applies to block mode")
        return self


class Tools(_Model):
    proxy: list[ProxiedServer] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _unique(self) -> Tools:
        names = [server.name for server in self.proxy]
        if len(set(names)) != len(names):
            raise ValueError("each proxied server has its own name")
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


class Gate(_Model):
    """When the run fails. ``fail_on: confirmed`` fails on a finding at level 2 or above;
    ``suspected`` also fails on a finding a model judged only."""

    fail_on: Literal["confirmed", "suspected"] = "confirmed"
    severity: Literal["critical", "high", "medium", "low", "info"] | None = None
    """Only findings of probes at this severity or above fail the gate."""
    fail_on_infrastructure_error: bool = True
    """Fail when the attacker, the judge, the platform or the harness failed."""
    ignore_target_errors: bool = False
    undetermined: Literal["fail", "ignore"] = "fail"
    """What an attempt no check could decide does to the gate. ``fail`` (the default) fails the run:
    an undetermined attempt is never a pass. ``ignore`` leaves it out, and the report still counts it."""


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
    output_folder: str = "results"
    environment: str | None = Field(default=None, min_length=1)
    """A Martian environment to run in. Needs a connection to a Martian server; a local run stops
    with a message that says so."""

    @model_validator(mode="after")
    def _consistent(self) -> RunFile:
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
    "Budget",
    "CanaryCheck",
    "Gate",
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
    "TextCheck",
    "ToolCallCheck",
    "Tools",
    "Vulnerability",
    "load",
    "problems",
]
