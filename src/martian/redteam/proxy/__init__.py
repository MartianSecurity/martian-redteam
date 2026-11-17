"""The recording MCP proxy: the open edition's tool boundary, between an agent and its own tool servers.

With no environment, the harness controls nothing the agent reads and sees nothing the agent does at
its tools. The proxy is the local equivalent of the cloud's run-scoped gateway: the agent reaches each
of its MCP tool servers through it (the run file's ``tools.proxy``), and every call is recorded with
its arguments and whether it ran. Each proxied server has a mode:

- ``record``: forward the call and record it.
- ``inject``: forward the call, add the ``inject`` text to the result the agent reads, and record the
  delivery. This is how an indirect prompt injection reaches the agent.
- ``block``: answer with ``block_reply`` and do not forward: the call never reaches the tool server,
  so the agent's real tool cannot carry out an attacker's goal. Its result is not a tool error.

``tools`` limits inject and block to the tools it names; without it they apply to every tool. A
blocked tool is listed without its output schema, so the canned text is a valid answer.

The proxy is not an environment. It simulates no application and holds no data: the only thing it
keeps is the call ledger, in memory, for the run. It listens on 127.0.0.1 only, one port per proxied
server, chosen by the system. Each call is recorded with the ``X-Martian-Action-Id`` header the
agent's binding sends (:func:`martian_redteam.bind_tools`), so an attempt's calls can be told apart
from another's; :class:`ProxiedTarget` adds them to a target's replies. Needs the ``mcp`` extra.

Usage::

    async with ToolProxy(run_file.tools.proxy) as proxy:
        proxy.url("commerce")    # http://127.0.0.1:PORT/mcp, in place of the upstream address
        ...
        proxy.calls()            # every call, in order
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal, Self

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, PaginatedRequestParams, TextContent, Tool
from pydantic import BaseModel, ConfigDict, Field

from martian.redteam._mcp import HeldClient, LoopbackServer, every_tool
from martian.redteam.model import Evidence, ToolCall
from martian.redteam.runfile import ProxiedServer
from martian.redteam.targets import Target, TargetReply

ACTION_HEADER = "x-martian-action-id"
"""The header :func:`martian_redteam.bind_tools` sends with the action (turn) its calls serve."""
BLOCK_REPLY = "This tool call was not run."
"""What a blocked call answers when its server sets no ``block_reply``."""
MAX_CALLS = 10_000
"""The most calls one proxy records. A call past it is refused, never left out of the ledger."""
RESULT_LIMIT = 4_000
"""The most characters of a result the ledger keeps."""
UPSTREAM_TIMEOUT_SECONDS = 90


class ProxyCall(BaseModel):
    """One call the agent made through the proxy. ``executed``: the call reached the tool server
    (``True``), was blocked (``False``), or failed on its way and may or may not have run (``None``).
    ``injected`` is the text inject mode added to the result: the delivery of a payload. ``result``
    is the start of what the agent received."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    server: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    action_id: str | None = None
    session_id: str | None = None
    mode: Literal["record", "inject", "block"]
    executed: bool | None
    injected: str | None = None
    result: str | None = None
    is_error: bool = False
    error: str = ""
    started_at: datetime
    duration_ms: float = Field(ge=0)

    @property
    def call_id(self) -> str:
        return f"proxy-{self.sequence}"

    def tool_call(self) -> ToolCall:
        """The call as a red team attempt keeps it."""
        return ToolCall(
            name=self.tool, arguments=self.arguments, call_id=self.call_id, executed=self.executed, result=self.result
        )

    def evidence(self) -> list[Evidence]:
        """The evidence the call gives: the call at the tool boundary, and, when a payload was
        added to its result, the payload's delivery (level 0)."""
        source = f"proxy:{self.server}"
        found = [
            Evidence(
                id=f"{self.call_id}-call",
                kind="tool_call",
                tier="tool",
                source=source,
                captured_at=self.started_at,
                metadata={
                    "tool": self.tool,
                    "executed": self.executed,
                    "action_id": self.action_id,
                    "arguments_captured": True,
                },
            )
        ]
        if self.injected is not None:
            found.append(
                Evidence(
                    id=f"{self.call_id}-payload",
                    kind="payload",
                    source=source,
                    content=self.injected,
                    captured_at=self.started_at,
                    metadata={"tool": self.tool, "action_id": self.action_id, "delivered": True},
                )
            )
        return found


def _result_text(result: CallToolResult) -> str:
    text = "\n".join(item.text for item in result.content if item.type == "text")
    if not text and result.structured_content is not None:
        text = json.dumps(result.structured_content, ensure_ascii=False, sort_keys=True, default=str)
    return text[:RESULT_LIMIT]


def _header(context: Any, name: str) -> str | None:
    request = getattr(context, "request", None)
    headers = getattr(request, "headers", None)
    value = headers.get(name) if headers is not None else None
    return value if isinstance(value, str) and value else None


@dataclass
class _Proxied:
    config: ProxiedServer
    upstream: HeldClient
    loopback: LoopbackServer | None = None

    def applies(self, tool: str) -> bool:
        return self.config.mode != "record" and (not self.config.tools or tool in self.config.tools)


class ToolProxy:
    """The proxy for ``servers``, the run file's ``tools.proxy``. Start it before the agent binds its
    tools and stop it when the run ends; ``async with`` does both."""

    def __init__(self, servers: Sequence[ProxiedServer], *, timeout_seconds: float = UPSTREAM_TIMEOUT_SECONDS) -> None:
        names = [server.name for server in servers]
        if len(set(names)) != len(names):
            raise ValueError("each proxied server has its own name")
        self._timeout = timeout_seconds
        self._servers = {server.name: _Proxied(server, HeldClient(self._upstream(server))) for server in servers}
        self._calls: list[ProxyCall] = []
        self._numbers = itertools.count(1)
        self._lock = asyncio.Lock()
        self._reserved = 0
        self._started = False

    def _upstream(self, server: ProxiedServer) -> Any:
        @asynccontextmanager
        async def connect() -> AsyncIterator[Client]:
            async with (
                httpx2.AsyncClient(timeout=self._timeout, follow_redirects=False) as http,
                Client(streamable_http_client(server.upstream, http_client=http), cache=None) as client,
            ):
                yield client

        return connect

    async def start(self) -> None:
        """Connect to every upstream server and start listening. A server that cannot be reached
        stops the start and names the proxied server, never its address."""
        try:
            for name, proxied in self._servers.items():
                try:
                    await every_tool(await proxied.upstream.open())
                except Exception as error:
                    raise ConnectionError(
                        f"the proxy could not reach tools.proxy {name!r}: {type(error).__name__}; check its upstream"
                    ) from None
                proxied.loopback = LoopbackServer(self._app(proxied))
                await proxied.loopback.start()
        except BaseException:
            await self.stop()
            raise
        self._started = True

    async def stop(self) -> None:
        """Stop listening and close the upstream connections. The ledger stays readable."""
        self._started = False
        for proxied in self._servers.values():
            if proxied.loopback is not None:
                await proxied.loopback.stop()
                proxied.loopback = None
            await proxied.upstream.close()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        await self.stop()

    def url(self, name: str) -> str:
        """The address the agent uses in place of the server ``name``'s upstream."""
        proxied = self._servers[name]
        if proxied.loopback is None:
            raise RuntimeError("the proxy is not running")
        return f"http://127.0.0.1:{proxied.loopback.port}/mcp"

    def addresses(self) -> dict[str, tuple[str, int]]:
        """Where each proxied server listens: always 127.0.0.1."""
        return {name: p.loopback.address for name, p in self._servers.items() if p.loopback is not None}

    def calls(self, *, action_id: str | None = None, session_id: str | None = None, after: int = 0) -> list[ProxyCall]:
        """The ledger, in order: every call, or those of one action or session, after a sequence number."""
        return [
            call
            for call in self._calls
            if call.sequence > after
            and (action_id is None or call.action_id == action_id)
            and (session_id is None or call.session_id == session_id)
        ]

    def _app(self, proxied: _Proxied) -> Any:
        async def list_tools(context: Any, params: PaginatedRequestParams | None) -> ListToolsResult:
            tools = await every_tool(await proxied.upstream.open())
            return ListToolsResult(tools=[self._listed(proxied, tool) for tool in tools.values()])

        async def call_tool(context: Any, params: CallToolRequestParams) -> CallToolResult:
            return await self._call(proxied, context, params)

        server = Server(f"martian-proxy-{proxied.config.name}", on_list_tools=list_tools, on_call_tool=call_tool)
        return server.streamable_http_app(host="127.0.0.1")

    @staticmethod
    def _listed(proxied: _Proxied, tool: Tool) -> Tool:
        if proxied.config.mode == "block" and proxied.applies(tool.name):
            return tool.model_copy(update={"output_schema": None})
        return tool

    async def _call(self, proxied: _Proxied, context: Any, params: CallToolRequestParams) -> CallToolResult:
        config = proxied.config
        async with self._lock:
            if self._reserved >= MAX_CALLS:
                return CallToolResult(
                    content=[TextContent(type="text", text="The proxy's call limit for this run is reached.")],
                    is_error=True,
                )
            self._reserved += 1
            sequence = next(self._numbers)
        arguments = dict(params.arguments or {})
        record: dict[str, Any] = {
            "server": config.name,
            "tool": params.name,
            "arguments": arguments,
            "action_id": _header(context, ACTION_HEADER),
            "session_id": self._session_of(context),
            "mode": config.mode if proxied.applies(params.name) else "record",
            "started_at": datetime.now(UTC),
        }
        started = time.perf_counter()
        if record["mode"] == "block":
            result = CallToolResult(content=[TextContent(type="text", text=config.block_reply or BLOCK_REPLY)])
            record["executed"] = False
        else:
            try:
                upstream = await proxied.upstream.open()
                result = await asyncio.wait_for(upstream.call_tool(params.name, arguments), self._timeout)
                record["executed"] = True
            except Exception as error:  # the call may or may not have run; say so, in our words only
                record.update(executed=None, error=type(error).__name__)
                result = CallToolResult(
                    content=[TextContent(type="text", text="The tool server could not complete this call.")],
                    is_error=True,
                )
            if record["mode"] == "inject" and record["executed"] and config.inject:
                result = result.model_copy(
                    update={"content": [*result.content, TextContent(type="text", text=config.inject)]}
                )
                record["injected"] = config.inject
        record.update(
            result=_result_text(result),
            is_error=bool(result.is_error),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        call = ProxyCall(sequence=sequence, **record)
        async with self._lock:
            self._calls.append(call)
            self._calls.sort(key=lambda found: found.sequence)
        return result

    def _session_of(self, context: Any) -> str | None:
        return None


class ProxiedTarget:
    """``target`` with the proxy's calls added to each reply: the calls whose action id is the
    reply's session. The agent passes its conversation's id (the body's ``{{session_id}}``, a
    function's ``session_id``) as ``action_id`` to :func:`martian_redteam.bind_tools`. A call the
    target reported itself and the proxy recorded, by its ``call_id`` or by its name and arguments,
    is kept once, with what the proxy saw."""

    def __init__(self, target: Target, proxy: ToolProxy) -> None:
        self._target = target
        self._proxy = proxy
        self._seen: dict[str, int] = {}

    @property
    def kind(self) -> str:
        return self._target.kind

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        reply = await self._target.send(message, session=session)
        if reply.session is None:
            return reply
        after = self._seen.get(reply.session, 0)
        recorded = self._proxy.calls(action_id=reply.session, after=after)
        if not recorded:
            return reply
        self._seen[reply.session] = recorded[-1].sequence
        observed = [call.tool_call() for call in recorded]
        reported = [call for call in reply.tool_calls if not any(_same(call, seen) for seen in observed)]
        return reply.model_copy(update={"tool_calls": [*reported, *observed]})

    async def close(self) -> None:
        await self._target.close()


def _same(reported: ToolCall, observed: ToolCall) -> bool:
    if reported.call_id is not None and reported.call_id == observed.call_id:
        return True
    return reported.name == observed.name and reported.arguments == observed.arguments


__all__ = [
    "ACTION_HEADER",
    "BLOCK_REPLY",
    "MAX_CALLS",
    "ProxiedTarget",
    "ProxyCall",
    "ToolProxy",
]
