"""What the red team engine attacks: the interface every target implements.

The engine sends a message to a :class:`Target` and reads a :class:`TargetReply`: the text the
target answered and the tool calls it made or asked for. A target keeps one conversation per
``session``: the same session continues a conversation, a new one starts another. The function,
HTTP and MCP targets of the run file, the version 0.2 callback, and later a registered agent, each
implement it; the engine knows none of them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from martian.redteam.model import ToolCall


class TargetReply(BaseModel):
    """One reply of the target. ``tool_calls`` are the calls the target reported or the harness
    observed, with ``ToolCall.executed`` saying whether each one ran."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_calls_observed: bool = False
    """Whether the target reports its tool calls. When false, an empty ``tool_calls`` says nothing,
    and a tool-call check cannot find that no call was made."""
    session: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)


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


__all__ = ["Target", "TargetReply"]
