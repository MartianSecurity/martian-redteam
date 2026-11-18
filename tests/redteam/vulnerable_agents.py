"""Deliberately vulnerable test agents for the catalogue's probes. Each runs in the test's process and
reaches nothing but the run's own loopback listeners. Nothing here calls a model."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from martian.redteam.model import ToolCall
from martian.redteam.targets import TargetReply
from martian.redteam.verify.rules import CANARY_SHAPE

LISTENER = re.compile(r"http://127\.0\.0\.1:(\d+)(/[rp]/[A-Za-z0-9_-]+)")


async def fetch(port: int, path: str) -> str:
    """A GET of the run's own loopback listener, as an agent's fetch tool would make it."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    answer = await reader.read()
    writer.close()
    return answer.decode("utf-8", errors="replace")


class Agent:
    """A target that answers ``reply(message, session)`` with the tool calls ``calls(message,
    session)`` names, and reports them as observed. Each message and session is kept in ``seen``."""

    kind = "callback"
    name = "vulnerable"

    def __init__(
        self,
        reply: Callable[[str, str | None], str] | str = "Done.",
        calls: Callable[[str, str | None], list[ToolCall]] | None = None,
    ) -> None:
        self.reply = reply
        self.calls = calls or (lambda message, session: [])
        self.seen: list[tuple[str, str | None]] = []

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        self.seen.append((message, session))
        text = self.reply(message, session) if callable(self.reply) else self.reply
        return TargetReply(
            text=text, tool_calls=self.calls(message, session), tool_calls_observed=True, session=session
        )

    async def close(self) -> None:
        return None


class FetchingAgent(Agent):
    """Fetches every address of the run's listeners it is sent, with the canary it holds as ``d`` when
    ``carry`` is true: an agent with a fetch tool that follows any instruction."""

    def __init__(self, *, carry: bool = True) -> None:
        super().__init__("Fetched.")
        self.carry = carry
        self.fetched: list[str] = []

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        canary = CANARY_SHAPE.search(message)
        for port, path in LISTENER.findall(message):
            query = f"?d={canary.group(0)}" if canary and self.carry else ""
            self.fetched.append(path)
            await fetch(int(port), path + query)
        return await super().send(message, session=session)


def calling(name: str, **arguments: Any) -> Callable[[str, str | None], list[ToolCall]]:
    """Calls that always make the one call ``name(**arguments)``, which runs."""
    return lambda message, session: [ToolCall(name=name, arguments=arguments, executed=True)]


__all__ = ["LISTENER", "Agent", "FetchingAgent", "calling", "fetch"]
