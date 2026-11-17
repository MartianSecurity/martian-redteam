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


class JudgeCheck(_Model):
    type: Literal["judge"]
    criteria: str | None = Field(default=None, min_length=1)


class ReceiptCheck(_Model):
    """A request to the run's local receipt listener. It needs a canary check in the same
    vulnerability: carrying that canary is verified exfiltration, a request without it contact only."""

    type: Literal["receipt"]


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


class Vulnerability(NamedSpec):
    """A vulnerability to probe, as in version 0.2, with an optional goal, objective, checks and
    severity. Without checks, a probe is decided by the judge."""

    goal: Goal | None = None
    objective: ObjectiveName | None = None
    checks: list[CheckDecl] = Field(default_factory=list, max_length=10)
    severity: Literal["critical", "high", "medium", "low", "info"] | None = None
    benign: Benign | None = None
    """A benign task whose completion is reported with and without the attack."""


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


class VerificationSettings(_Model):
    """How the run checks its own findings and its judge."""

    control: bool = True
    """Run each probe once without the attack, when it hit or has a benign task: a hit the control
    shows too is discarded."""
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
        sample = loaded.verification.calibration
        if sample is not None and not Path(sample).is_absolute():
            settings = loaded.verification.model_copy(update={"calibration": str((file.parent / sample).resolve())})
            loaded = loaded.model_copy(update={"verification": settings})
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
    "Benign",
    "Budget",
    "VerificationSettings",
    "CanaryCheck",
    "CanaryPlant",
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
    "TargetSession",
    "TextCheck",
    "ToolCallCheck",
    "Tools",
    "Vulnerability",
    "load",
    "problems",
]
