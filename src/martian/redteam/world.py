"""The world: the plug point through which an attempt's environment is reset, planted and read.

A run in a Martian environment checks what the agent did to the application's records, not only what
it said or called. The engine calls a :class:`World` at the boundaries of each attempt:

- :meth:`World.prepare` before the attempt's first message: reset the world, and plant content in it
  when the probe plants some (``Implant``).
- :meth:`World.observe` after a turn: the state checks now, against the attempt's baseline.
- :meth:`World.outcome` at the end of the attempt: its state result (:class:`WorldOutcome`), and the
  tool calls the environment's tool boundary recorded for it.

This package ships the interface and :class:`NullWorld`, the world of a run without an environment:
it resets nothing and decides nothing, so a state check stays undetermined. The only other world is
a Martian server's (:class:`martian.redteam.environment.ServerWorld`), which this package reaches
over HTTP. No environment code is here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from martian.redteam.model import CheckKind, Evidence, ToolCall


@dataclass(frozen=True)
class Implant:
    """Content to plant in the world before an attempt: an implant profile the environment's agent
    connection declares, and the text the profile's fields take."""

    profile: str
    text: str


@dataclass(frozen=True)
class StateResult:
    """One state check's result: ``violated`` is ``None`` when the world could not say."""

    violated: bool | None
    description: str = ""
    conditions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class WorldOutcome:
    """What the world showed at the end of an attempt: each state check's result by name, the tool
    calls its tool boundary recorded with their evidence, and whether planted content reached the
    agent (``None`` when nothing was planted or delivery is not known)."""

    checks: dict[str, StateResult] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    delivered: bool | None = None
    receipt: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class World(Protocol):
    """An attempt's environment. ``attempt`` is the attempt's id, one world each; ``action_id`` is
    what the agent sends its tool boundary in that attempt."""

    @property
    def name(self) -> str: ...

    async def prepare(self, attempt: str, action_id: str, *, implant: Implant | None = None) -> dict[str, Any]: ...

    async def observe(self, attempt: str, action_id: str) -> WorldOutcome: ...

    async def outcome(self, attempt: str, action_id: str) -> WorldOutcome: ...


class NullWorld:
    """No environment: nothing is reset, planted or read, and no state check is decided."""

    name = ""

    async def prepare(self, attempt: str, action_id: str, *, implant: Implant | None = None) -> dict[str, Any]:
        return {}

    async def observe(self, attempt: str, action_id: str) -> WorldOutcome:
        return WorldOutcome()

    async def outcome(self, attempt: str, action_id: str) -> WorldOutcome:
        return WorldOutcome()


class WorldUnavailable(RuntimeError):
    """The world could not be prepared or read. The attempt is an infrastructure error."""


class WorldStateVerifier:
    """The state verifier of a run in an environment, in the slot :class:`~martian.redteam.verify.
    verifiers.StateSlot` holds otherwise: a state check is decided by the world's outcome of the
    attempt (``Observation.extra["world"]``), the check it names. A name the world does not decide,
    or records it could not read, leave the check undetermined."""

    kind: CheckKind = "state"
    version = "state-world-v1"
    deterministic = True

    def __init__(self, world: str) -> None:
        self.world = world

    async def verify(self, check: Any, seen: Any, meter: Any = None) -> Any:
        from martian.redteam.verify.verifiers import result

        name = str(check.spec.get("name", ""))
        outcome = seen.extra.get("world")
        found = outcome.checks.get(name) if outcome is not None else None
        detail: dict[str, Any] = {"name": name, "conditions": found.conditions if found is not None else []}
        if outcome is not None and outcome.delivered is not None:
            detail["delivered"] = outcome.delivered
        if found is None:
            detail["reason"] = f"the environment has no state check named {name!r}"
            hit: bool | None = None
        elif found.violated is None:
            detail["reason"] = "the environment could not read the records this check compares"
            hit = None
        else:
            hit = bool(found.violated)
            detail["reason"] = found.description
        return result(self, check, seen, hit, detail, source=f"environment:{self.world}")


__all__ = ["Implant", "NullWorld", "StateResult", "World", "WorldOutcome", "WorldStateVerifier", "WorldUnavailable"]
