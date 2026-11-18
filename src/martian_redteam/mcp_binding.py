"""Optional multi-server MCP client binding. Imported only when bind_tools is used."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx2
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from referencing import Registry

from martian_redteam.integration import AssessmentSession, TraceRecorder
from martian_redteam.runtime import RuntimeTrace

ACTION_HEADER = "X-Martian-Action-Id"
ACTION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
# A sandbox gateway can complete a tool after its upstream has responded. Keep
# this below the customer agent's 120-second turn limit so the tool has time to
# return an explicit outcome; an uncertain mutation is never retried here.
MCP_REQUEST_TIMEOUT_SECONDS = 90


@dataclass
class BoundTool:
    connection: str
    remote_name: str
    client: Client
    definition: dict[str, Any]


class BoundMCPTools:
    def __init__(
        self,
        session: AssessmentSession,
        trace: TraceRecorder | None,
        action_id: str,
        runtime: RuntimeTrace | None = None,
    ) -> None:
        self.session = session
        self.trace = trace
        self.action_id = action_id
        self.runtime = runtime
        self.runtime_parent_event_id: uuid.UUID | None = None
        self.tools: dict[str, BoundTool] = {}

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition for tool in self.tools.values()]

    async def call(self, name: str, arguments: dict[str, Any], *, call_id: str) -> dict[str, Any]:
        self.session.ensure_active()
        if name not in self.tools:
            raise ValueError("model requested a tool outside the session allowlist")
        tool = self.tools[name]
        Draft202012Validator(tool.definition["input_schema"], registry=Registry()).validate(arguments)
        started = perf_counter()
        metadata = {
            "session_id": self.session.session_id,
            "action_id": self.action_id,
            "call_id": call_id,
            "connection": tool.connection,
            "tool": tool.remote_name,
        }
        if self.trace:
            self.trace.record("tool.start", payload=arguments, **metadata)
        observation = (
            self.runtime.guarded_call(
                kind="tool",
                system=tool.connection,
                name=tool.remote_name,
                parent_event_id=self.runtime_parent_event_id,
            )
            if self.runtime
            else nullcontext()
        )
        async with observation as span:
            try:
                result = await tool.client.call_tool(tool.remote_name, arguments)
                value = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                if len(json.dumps(value).encode()) > 256000:
                    raise ValueError("MCP tool result exceeds 256 KB")
            except Exception as exc:
                if self.trace:
                    self.trace.record("tool.error", error_type=type(exc).__name__, **metadata)
                raise
            if self.trace:
                self.trace.record(
                    "tool.end",
                    payload=value,
                    is_error=result.is_error,
                    duration_ms=round((perf_counter() - started) * 1000),
                    **metadata,
                )
            if span is not None:
                span.outcome = "error" if result.is_error else "success"
                if self.runtime is not None:
                    span.resources = self.runtime.resources(tool.connection, tool.remote_name, value)
        return value


@asynccontextmanager
async def open_tools(
    session: AssessmentSession,
    *,
    trace: TraceRecorder | None = None,
    action_id: str = "",
    runtime: RuntimeTrace | None = None,
) -> AsyncIterator[BoundMCPTools]:
    session.ensure_active()
    bound = BoundMCPTools(session, trace, action_id, runtime)
    if trace:
        credentials = [
            value.get_secret_value() for connection in session.mcp_connections for value in connection.headers.values()
        ]
        # Also suppress the bare credential if a tool echoes an Authorization value without its scheme.
        credentials.extend(
            value.split(" ", 1)[1] for value in list(credentials) if value.lower().startswith(("bearer ", "basic "))
        )
        trace.protect_secrets(credentials)
    # Enter and exit transports in this task. No cross-request AnyIO cancel scopes.
    async with AsyncExitStack() as stack:
        for connection in session.mcp_connections:
            session.ensure_active()
            headers = {key: value.get_secret_value() for key, value in connection.headers.items()}
            # Tell the server which action (turn) these calls serve, so a gateway that
            # records them can attribute each call. Configured headers win on a clash.
            if ACTION_ID.fullmatch(action_id) and not any(k.lower() == ACTION_HEADER.lower() for k in headers):
                headers[ACTION_HEADER] = action_id
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers=headers,
                    timeout=MCP_REQUEST_TIMEOUT_SECONDS,
                    follow_redirects=False,
                )
            )
            client = await stack.enter_async_context(
                Client(
                    streamable_http_client(connection.url, http_client=http),
                    read_timeout_seconds=MCP_REQUEST_TIMEOUT_SECONDS,
                )
            )
            available = {}
            cursor = None
            seen_cursors: set[str] = set()
            for _ in range(100):
                page = await client.list_tools(cursor=cursor)
                for tool in page.tools:
                    if tool.name in available:
                        raise ValueError("MCP server advertised duplicate tool names")
                    available[tool.name] = tool
                cursor = page.next_cursor
                if cursor is None:
                    break
                if cursor in seen_cursors:
                    raise ValueError("MCP server repeated its pagination cursor")
                seen_cursors.add(cursor)
            else:
                raise ValueError("MCP tool discovery exceeded 100 pages")
            if set(connection.allowed_tools) - available.keys():
                raise ValueError(f"MCP connection {connection.name} lacks configured tools")
            for remote_name in connection.allowed_tools:
                tool = available[remote_name]
                Draft202012Validator.check_schema(tool.input_schema)
                # Stable, provider-safe names even if remote names contain punctuation.
                digest = hashlib.sha256(remote_name.encode()).hexdigest()[:24]
                name = f"{connection.name}__{digest}"
                if name in bound.tools:
                    raise ValueError("MCP tool alias collision")
                bound.tools[name] = BoundTool(
                    connection.name,
                    remote_name,
                    client,
                    {
                        "name": name,
                        "description": f"{connection.name}: {remote_name}. {tool.description or ''}"[:4096],
                        "input_schema": tool.input_schema,
                    },
                )
        if len(bound.tools) > 128:
            raise ValueError("session exceeds 128 selected tools")
        session.ensure_active()
        yield bound
