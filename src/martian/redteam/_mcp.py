"""What the MCP target and the recording proxy share: an MCP client held open by a task of its own,
a tool's schema digest, a listing of every tool, and an ASGI app served on loopback. Needs the ``mcp``
extra; nothing imports this module until an MCP target or the proxy is built.

An MCP client's transport holds AnyIO cancel scopes, which must be entered and left in one task. A
target is sent messages from many tasks (one per probe), and closed from yet another, so
:class:`HeldClient` enters the client in a task of its own and leaves it there when it is closed; any
task may call through it in between.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

import uvicorn
from mcp import Client
from mcp.types import Tool

MAX_TOOL_PAGES = 100


def schema_digest(tool: Tool) -> str:
    """The SHA-256 of a tool's input schema as canonical JSON, as the hosted engine pins an MCP
    target's entry tool."""
    schema = tool.input_schema or {}
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def every_tool(client: Client) -> dict[str, Tool]:
    """Every tool a server lists, by name, page after page. A server that repeats a name or a page
    cursor, or lists more than :data:`MAX_TOOL_PAGES` pages, is refused."""
    found: dict[str, Tool] = {}
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_TOOL_PAGES):
        page = await client.list_tools(cursor=cursor, cache_mode="bypass")
        for tool in page.tools:
            if tool.name in found:
                raise ValueError("the MCP server lists one tool name twice")
            found[tool.name] = tool
        cursor = page.next_cursor
        if cursor is None:
            return found
        if cursor in seen:
            raise ValueError("the MCP server repeated its pagination cursor")
        seen.add(cursor)
    raise ValueError(f"the MCP server lists more than {MAX_TOOL_PAGES} pages of tools")


class HeldClient:
    """An MCP client entered and left by a task of its own. ``connect`` opens the client and
    whatever it needs, such as its HTTP client, as one context."""

    def __init__(self, connect: Callable[[], AbstractAsyncContextManager[Client]]) -> None:
        self._connect = connect
        self._client: Client | None = None
        self._error: BaseException | None = None
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._opening = asyncio.Lock()

    async def _hold(self) -> None:
        try:
            async with self._connect() as client:
                self._client = client
                self._ready.set()
                await self._closing.wait()
        except BaseException as error:  # noqa: BLE001 - handed to the task that waits for the client
            self._error = error
        finally:
            self._client = None
            self._ready.set()

    async def open(self) -> Client:
        """The client, entered on the first call. A client that could not be entered raises its error
        here, every time."""
        async with self._opening:
            if self._task is None:
                self._task = asyncio.create_task(self._hold(), name="martian-redteam-mcp-client")
            await self._ready.wait()
        if self._client is None:
            raise self._error or RuntimeError("the MCP client is closed")
        return self._client

    async def close(self) -> None:
        self._closing.set()
        if self._task is not None:
            await self._task


class _QuietServer(uvicorn.Server):
    """A uvicorn server that leaves the process's signal handlers alone: it runs inside the caller's
    event loop, beside the caller's own work."""

    def install_signal_handlers(self) -> None:
        pass

    def capture_signals(self) -> Any:
        return nullcontext()


class LoopbackServer:
    """An ASGI app served on 127.0.0.1, on a port the system picks, in the caller's event loop."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._server: _QuietServer | None = None
        self._task: asyncio.Task[None] | None = None
        self.port = 0

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        sock.setblocking(False)
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(self._app, log_level="warning", lifespan="on", access_log=False)
        server = _QuietServer(config)
        self._server = server
        self._task = asyncio.create_task(server.serve(sockets=[sock]), name="martian-redteam-loopback")
        while not server.started:
            if self._task.done():
                await self._task
                raise RuntimeError("the loopback server did not start")
            await asyncio.sleep(0.01)

    @property
    def address(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)

    async def stop(self) -> None:
        if self._server is not None and self._task is not None:
            self._server.should_exit = True
            await self._task
            self._server = self._task = None


__all__ = ["MAX_TOOL_PAGES", "HeldClient", "LoopbackServer", "every_tool", "schema_digest"]
