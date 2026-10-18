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
from mcp.types import CallToolResult
from referencing import Registry

from martian_redteam.artifact_inspection import inspect_artifact
from martian_redteam.call_gate import CallBlocked, GateUnavailable
from martian_redteam.call_gate_schema import CallTarget
from martian_redteam.integration import AssessmentSession, TraceRecorder
from martian_redteam.runtime import RuntimeTrace
from martian_redteam.task_authorization import TaskDenied, TaskGuard
from martian_redteam.tool_operations import ToolOperationPolicy, snapshot_arguments

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
    destination: str = ""
    host_header: str | None = None


class BoundMCPTools:
    def __init__(
        self,
        session: AssessmentSession,
        trace: TraceRecorder | None,
        action_id: str,
        runtime: RuntimeTrace | None = None,
        operations: ToolOperationPolicy | None = None,
        authorization: TaskGuard | None = None,
    ) -> None:
        self.session = session
        self.trace = trace
        self.action_id = action_id
        self.runtime = runtime
        self.operations = operations
        self.authorization = authorization
        self.runtime_parent_event_id: uuid.UUID | None = None
        self.tools: dict[str, BoundTool] = {}
        self.inspections: dict[str, dict[str, Any]] = {}

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition for tool in self.tools.values()]

    async def call(self, name: str, arguments: dict[str, Any], *, call_id: str) -> dict[str, Any]:
        self.session.ensure_active()
        if name not in self.tools:
            raise ValueError("model requested a tool outside the session allowlist")
        tool = self.tools[name]
        arguments = snapshot_arguments(arguments)
        resolved_name = (
            self.operations.resolve(tool.connection, tool.remote_name, arguments)
            if self.operations
            else tool.remote_name
        )
        Draft202012Validator(tool.definition["input_schema"], registry=Registry()).validate(arguments)
        started = perf_counter()
        metadata = {
            "session_id": self.session.session_id,
            "action_id": self.action_id,
            "call_id": call_id,
            "connection": tool.connection,
            "tool": tool.remote_name,
            "resolved_tool": resolved_name,
        }

        def report_task_decision(reason: str) -> None:
            if self.runtime is not None and self.runtime.reporter.security_decisions and self.authorization is not None:
                status = self.authorization.status()
                self.runtime.security_decision(
                    CallTarget(kind="tool", system=tool.connection, name=resolved_name),
                    reason,
                    task_ref=status["task_id"],
                    remote_task_ref=status["remote_task_id"],
                    policy_ref=status["policy_ref"],
                )

        def authorize(*, consume: bool) -> None:
            self.session.ensure_active()
            if self.authorization is not None:
                receipt = self.authorization.check(
                    session_id=self.session.session_id,
                    system=tool.connection,
                    tool=tool.remote_name,
                    resolved_tool=resolved_name,
                    arguments=arguments,
                    action_id=self.action_id,
                    call_id=call_id,
                    consume=consume,
                )
                if consume and self.trace:
                    self.trace.record("tool.authorized", **receipt, **metadata)

        def before_entry() -> None:
            authorize(consume=True)
            report_task_decision("scope_allowed")

        async def before_entry_async() -> None:
            authorize(consume=False)
            if self.authorization is not None and self.authorization.authority is not None:
                await self.authorization.authority.before_call(
                    CallTarget(kind="tool", system=tool.connection, name=resolved_name),
                    transport_name=tool.remote_name,
                    arguments=arguments,
                    destination=tool.destination,
                    host_header=tool.host_header,
                    tool_sha256=self.inspections[name]["artifact_sha256"],
                )

        if self.trace:
            self.trace.record("tool.start", payload=arguments, **metadata)
        observation = (
            self.runtime.guarded_call(
                kind="tool",
                system=tool.connection,
                name=resolved_name,
                transport_name=tool.remote_name if resolved_name != tool.remote_name else None,
                parent_event_id=self.runtime_parent_event_id,
                before_entry=before_entry,
                before_entry_async=before_entry_async,
            )
            if self.runtime
            else nullcontext()
        )
        try:
            authorize(consume=False)
            if self.runtime is None:
                await before_entry_async()
                before_entry()
            async with observation as span:
                try:
                    self.session.ensure_active()
                    # A protected action is sent once. The high-level MCP helper
                    # can drive interactive input rounds by reissuing tools/call.
                    result = await tool.client.session.call_tool(tool.remote_name, arguments, allow_input_required=True)
                    if not isinstance(result, CallToolResult):
                        raise ValueError("Interactive or extended MCP results require a separately authorized workflow")
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
        except (CallBlocked, GateUnavailable, TaskDenied) as exc:
            if isinstance(exc, TaskDenied):
                report_task_decision(exc.reason)
            if self.trace:
                self.trace.record(
                    "tool.blocked",
                    error_type=type(exc).__name__,
                    reason=exc.reason if isinstance(exc, TaskDenied) else "call_gate",
                    **metadata,
                )
            raise
        return value


@asynccontextmanager
async def open_tools(
    session: AssessmentSession,
    *,
    trace: TraceRecorder | None = None,
    action_id: str = "",
    runtime: RuntimeTrace | None = None,
    operations: ToolOperationPolicy | None = None,
    authorization: TaskGuard | None = None,
) -> AsyncIterator[BoundMCPTools]:
    session.ensure_active()
    bound = BoundMCPTools(session, trace, action_id, runtime, operations, authorization)
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
                    trust_env=False,
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
            if operations is not None:
                operations.validate_binding(connection.name, connection.allowed_tools, set(available))
            for remote_name in connection.allowed_tools:
                tool = available[remote_name]
                Draft202012Validator.check_schema(tool.input_schema)
                # Stable, provider-safe names even if remote names contain punctuation.
                digest = hashlib.sha256(remote_name.encode()).hexdigest()[:24]
                name = f"{connection.name}__{digest}"
                if name in bound.tools:
                    raise ValueError("MCP tool alias collision")
                inspection = inspect_artifact(
                    "mcp_tool",
                    {
                        "name": remote_name,
                        "description": tool.description or "",
                        "input_schema": tool.input_schema,
                    },
                )
                if inspection["status"] == "invalid":
                    raise ValueError("MCP tool has an invalid argument schema")
                bound.inspections[name] = inspection
                if trace:
                    trace.record("tool.inspected", connection=connection.name, tool=remote_name, **inspection)
                bound.tools[name] = BoundTool(
                    connection.name,
                    remote_name,
                    client,
                    {
                        "name": name,
                        "description": f"{connection.name}: {remote_name}. {tool.description or ''}"[:4096],
                        "input_schema": tool.input_schema,
                    },
                    destination=connection.url,
                    host_header=next((v for k, v in headers.items() if k.lower() == "host"), None),
                )
        if len(bound.tools) > 128:
            raise ValueError("session exceeds 128 selected tools")
        session.ensure_active()
        yield bound
