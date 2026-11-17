"""What the red team engine attacks: the interface every target implements.

The engine sends a message to a :class:`Target` and reads a :class:`TargetReply`: the text the
target answered and the tool calls it made or asked for. A target keeps one conversation per
``session``: the same session continues a conversation, a new one starts another. The function,
HTTP and MCP targets of the run file, the version 0.2 callback, and later a registered agent, each
implement it; the engine knows none of them.

- :func:`target_from_run_file` builds the target a run file's ``target`` block names
  (:mod:`martian.redteam.targets.factory`): :class:`~martian.redteam.targets.function.FunctionTarget`
  or :class:`~martian.redteam.targets.http.HttpTarget`, from the suite file's own target block, or
  :class:`~martian.redteam.targets.mcp.McpServerTarget` for an MCP server.
- :mod:`martian.redteam.targets.reply` is the reply contract: the text and the tool calls a target
  reports.
- :mod:`martian.redteam.targets.conversations` keeps each target's conversations.

``docs/redteam-targets.md`` describes each target and the session contract.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from martian.redteam.model import Evidence, ToolCall


class TargetReply(BaseModel):
    """One reply of the target. ``tool_calls`` are the calls the target reported or the harness
    observed, with ``ToolCall.executed`` saying whether each one ran. ``evidence`` is what a boundary
    the harness trusts captured about them: the recording proxy's ledger, or a trace's tool spans,
    each with its source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_calls_observed: bool = False
    """Whether the target reports its tool calls. When false, an empty ``tool_calls`` says nothing,
    and a tool-call check cannot find that no call was made."""
    session: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    evidence: list[Evidence] = Field(default_factory=list)


class TargetChanged(RuntimeError):
    """The target is not the one the run started with, such as an MCP tool whose input schema
    changed. The engine stops the run: it never attacks a target other than the one it pinned."""


@runtime_checkable
class Target(Protocol):
    """A target under attack."""

    @property
    def kind(self) -> str:
        """``function``, ``http``, ``mcp``, ``callback``, ``agent`` or ``browser``."""
        ...

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        """Send one message in ``session`` (a new conversation when ``None``) and return the reply.
        A target that fails raises; the engine records the attempt as a target error."""
        ...

    async def close(self) -> None:
        """Release what the target holds: connections, processes, sessions."""
        ...


def __getattr__(name: str) -> Any:
    # The factory imports the evals core's target code; it loads only when it is asked for.
    if name == "target_from_run_file":
        from martian.redteam.targets.factory import target_from_run_file

        return target_from_run_file
    raise AttributeError(name)


__all__ = ["Target", "TargetChanged", "TargetReply", "target_from_run_file"]
