"""Customer-managed, task-bound Streamable HTTP MCP enforcement gateway.

Configuration is trusted operator input. Callers supply only MCP protocol requests;
they cannot mint tasks, change upstreams or obtain the gateway's service credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from martian_redteam.call_gate import CallBlocked, GateUnavailable, RuntimeCallGate
from martian_redteam.call_gate_schema import CallTarget, GateAssignment
from martian_redteam.integration import AssessmentSession, MCPConnection, bind_tools
from martian_redteam.mcp_binding import BoundMCPTools
from martian_redteam.runtime import RuntimeReporter
from martian_redteam.runtime_policy import RuntimeTask, RuntimeTaskClient
from martian_redteam.runtime_policy_schema import PolicyChannelAssignment, TaskAssignment
from martian_redteam.task_authorization import TaskAuthorization, TaskDenied, TaskGuard
from martian_redteam.tool_operations import ToolOperationPolicy

log = logging.getLogger(__name__)


class GatewayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactPin(GatewayModel):
    system: str
    tool: str
    public_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:/@+\-]{1,128}$")]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class GatewayTask(GatewayModel):
    route_id: UUID
    credential_file: Path
    authorization: TaskAuthorization = Field(repr=False)
    assignment: TaskAssignment
    connections: tuple[MCPConnection, ...] = Field(min_length=1, max_length=16, repr=False)
    artifacts: tuple[ArtifactPin, ...] = Field(min_length=1, max_length=128)
    operations: ToolOperationPolicy | None = None

    @model_validator(mode="after")
    def exact_scope(self) -> Self:
        selected = {(c.name, t) for c in self.connections for t in c.allowed_tools}
        pins = {(p.system, p.tool) for p in self.artifacts}
        if len({c.name for c in self.connections}) != len(self.connections):
            raise ValueError("Duplicate connection names")
        if selected != pins or len(pins) != len(self.artifacts):
            raise ValueError("Each selected tool requires exactly one metadata pin")
        if len({p.public_name for p in self.artifacts}) != len(self.artifacts):
            raise ValueError("Public tool names must be unique within a route")
        if any((g.system, g.tool) not in selected for g in self.authorization.grants):
            raise ValueError("Task grant references an unselected tool")
        if not self.credential_file.is_absolute():
            raise ValueError("Credential paths must be absolute")
        return self


class GatewayConfig(GatewayModel):
    schema_version: Literal[1] = 1
    backend_url: str
    allow_http: bool = False
    gate: GateAssignment
    gate_token_file: Path
    reporter_token_file: Path
    channel: PolicyChannelAssignment
    channel_token_file: Path
    action_review_targets: tuple[CallTarget, ...] = Field(default=(), max_length=32)
    kubernetes_token_file: Path | None = None
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=32)
    max_inflight: int = Field(default=8, ge=1, le=32)
    tasks: tuple[GatewayTask, ...] = Field(min_length=1, max_length=32)

    @field_validator("allowed_hosts")
    @classmethod
    def exact_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"[a-zA-Z0-9.\-]+(?::[0-9]{1,5})?", h) for h in value):
            raise ValueError("Hosts must be exact DNS/IPv4 authorities without wildcards")
        return value

    @model_validator(mode="after")
    def binding_scope(self) -> Self:
        if self.gate.binding_id != self.channel.binding_id:
            raise ValueError("Gate and task channel must share the deployment binding")
        if (self.tls_cert_file is None) != (self.tls_key_file is None):
            raise ValueError("TLS requires both certificate and private key files")
        if not self.allow_http and self.tls_cert_file is None:
            raise ValueError("The gateway listener requires TLS; HTTP needs explicit local configuration")
        if len({t.route_id for t in self.tasks}) != len(self.tasks):
            raise ValueError("Duplicate route IDs")
        if len({t.credential_file for t in self.tasks}) != len(self.tasks):
            raise ValueError("Each route needs its own credential file")
        if len({t.assignment.task_id for t in self.tasks}) != len(self.tasks):
            raise ValueError("A shared task may appear only once per gateway configuration")
        for path in (
            self.gate_token_file,
            self.reporter_token_file,
            self.channel_token_file,
            self.kubernetes_token_file,
            self.tls_cert_file,
            self.tls_key_file,
        ):
            if path is not None and not path.is_absolute():
                raise ValueError("Credential paths must be absolute")
        targets = set(self.gate.targets)
        task_targets = {op.target for task in self.tasks for op in task.assignment.operations}
        if any(t.kind != "tool" or t not in task_targets for t in self.action_review_targets):
            raise ValueError("Action review export targets must be explicitly assigned task operations")
        for task in self.tasks:
            for connection in task.connections:
                if not self.allow_http and not connection.url.startswith("https://"):
                    raise ValueError("HTTP upstreams require explicit local configuration")
                for name in connection.allowed_tools:
                    if CallTarget(kind="tool", system=connection.name, name=name) not in targets:
                        raise ValueError("Every selected raw tool must be enrolled in the Call Gate")
        return self

    @classmethod
    def from_file(cls, path: Path) -> GatewayConfig:
        try:
            if not path.is_absolute() or path.stat().st_size > 512 * 1024:
                raise ValueError("Invalid configuration path or size")
            return cls.model_validate_json(path.read_bytes())
        except (OSError, ValueError):
            raise ValueError("Invalid gateway configuration; details suppressed to protect private input") from None


def _credential(path: Path) -> bytes:
    if path.stat().st_size > 256:
        raise ValueError("Invalid gateway credential")
    value = path.read_bytes().strip()
    if not re.fullmatch(rb"mgw_[A-Za-z0-9_-]{40,128}", value):
        raise ValueError("Invalid gateway credential")
    return b"Bearer " + value


class Admission:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0


class CredentialBoundary:
    """Authenticate before MCP parsing, with one process-wide bounded admission count."""

    def __init__(self, app: ASGIApp, path: Path, admission: Admission) -> None:
        self.app, self.path, self.admission = app, path, admission

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        supplied = Headers(scope=scope).getlist("authorization")
        try:
            valid = len(supplied) == 1 and secrets.compare_digest(supplied[0].encode("ascii"), _credential(self.path))
        except (OSError, ValueError, UnicodeError):
            valid = False
        if not valid:
            await JSONResponse({"error": "gateway_unauthorized"}, status_code=401)(scope, receive, send)
            return
        if self.admission.active >= self.admission.limit:
            await JSONResponse({"error": "gateway_busy"}, status_code=503)(scope, receive, send)
            return
        self.admission.active += 1
        started = finished = False

        async def bounded_send(message: Message) -> None:
            nonlocal started, finished
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True
            await send(message)

        try:
            async with asyncio.timeout(105):
                await self.app(scope, receive, bounded_send)
        except TimeoutError:
            if not started:
                await JSONResponse({"error": "gateway_deadline"}, status_code=504)(scope, receive, send)
            elif not finished:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            self.admission.active -= 1


class TaskRoute:
    def __init__(self, spec: GatewayTask, authority: RuntimeTask, reporter: RuntimeReporter) -> None:
        self.spec, self.authority, self.reporter = spec, authority, reporter
        self.session = AssessmentSession(
            session_id=str(spec.route_id),
            campaign_id="customer-gateway",
            target_id=str(reporter.binding_id),
            expires_at=spec.authorization.expires_at,
            mcp_connections=list(spec.connections),
        )
        self.guard = TaskGuard(
            spec.authorization, session_id=self.session.session_id, issuer="gateway-operator", authority=authority
        )
        self.catalog: list[types.Tool] = []
        self.aliases: dict[str, str] = {}

    def verify_metadata(self, bound: BoundMCPTools) -> None:
        actual = {
            (tool.connection, tool.remote_name): bound.inspections[name]["artifact_sha256"]
            for name, tool in bound.tools.items()
        }
        expected = {(p.system, p.tool): p.sha256 for p in self.spec.artifacts}
        if actual != expected:
            raise ValueError("Upstream metadata does not match approved pins")

    async def prepare(self) -> None:
        await self.authority.before_turn()
        async with asyncio.timeout(100), bind_tools(self.session, operations=self.spec.operations) as bound:
            self.verify_metadata(bound)
            names = {(p.system, p.tool): p.public_name for p in self.spec.artifacts}
            for alias, tool in bound.tools.items():
                public_name = names[(tool.connection, tool.remote_name)]
                self.aliases[public_name] = alias
                self.catalog.append(types.Tool.model_validate({**tool.definition, "name": public_name}))

    async def list_tools(
        self, ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        try:
            await self.authority.before_turn()
            self.session.ensure_active()
        except (TaskDenied, ValueError):
            raise MCPError(code=-32001, message="gateway_task_unavailable") from None
        if params is not None and params.cursor is not None:
            raise MCPError(code=-32602, message="gateway_cursor_unsupported")
        return types.ListToolsResult(tools=self.catalog)

    async def call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        try:
            if params.name not in self.aliases:
                raise ValueError("Unselected tool")
            # Caller-selected metadata, headers and task IDs never enter upstream configuration.
            async with (
                asyncio.timeout(100),
                bind_tools(
                    self.session,
                    action_id=str(uuid4()),
                    operations=self.spec.operations,
                    authorization=self.guard,
                    runtime=self.reporter.trace(),
                ) as bound,
            ):
                self.verify_metadata(bound)
                # Return explicit denials inside the transport scope. Letting them
                # unwind MCP's task groups would wrap them in an ExceptionGroup.
                try:
                    value = await bound.call(self.aliases[params.name], params.arguments or {}, call_id=str(uuid4()))
                except (TaskDenied, CallBlocked, GateUnavailable) as exc:
                    return stopped(exc)
                return types.CallToolResult.model_validate(value)
        except Exception as exc:
            # Transport/schema errors may contain upstream values; never serialize them to a caller or log.
            log.warning("gateway_request_stopped error_type=%s", type(exc).__name__)
            reason = "gateway_request_stopped"
        return types.CallToolResult(is_error=True, content=[types.TextContent(type="text", text=reason)])


def stopped(exc: TaskDenied | CallBlocked | GateUnavailable) -> types.CallToolResult:
    reason = (
        exc.reason
        if isinstance(exc, TaskDenied)
        else ("call_gate_blocked" if isinstance(exc, CallBlocked) else "call_gate_unavailable")
    )
    return types.CallToolResult(is_error=True, content=[types.TextContent(type="text", text=reason)])


def build_app(config: GatewayConfig) -> Starlette:
    gate = RuntimeCallGate(
        config.backend_url,
        config.gate,
        config.gate_token_file,
        allow_http=config.allow_http,
        kubernetes_token_file=config.kubernetes_token_file,
    )
    reporter = RuntimeReporter(
        config.backend_url,
        config.gate.binding_id,
        config.reporter_token_file,
        allow_http=config.allow_http,
        security_decisions=True,
        call_gate=gate,
    )
    client = RuntimeTaskClient(
        config.backend_url,
        config.channel,
        config.channel_token_file,
        allow_http=config.allow_http,
        action_review_targets=config.action_review_targets,
    )
    admission = Admission(config.max_inflight)
    tasks = [TaskRoute(t, client.attach(t.authorization, t.assignment), reporter) for t in config.tasks]
    children: list[Starlette] = []
    routes: list[Mount | Route] = []
    for task in tasks:
        server: Server[Any] = Server(
            "martian-runtime-gateway",
            version="1",
            on_list_tools=task.list_tools,
            on_call_tool=task.call_tool,
        )
        child = server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            max_request_body_size=256 * 1024,
            transport_security=TransportSecuritySettings(allowed_hosts=list(config.allowed_hosts), allowed_origins=[]),
        )
        children.append(child)
        routes.append(
            Mount(
                f"/tasks/{task.spec.route_id}",
                app=CredentialBoundary(child, task.spec.credential_file, admission),
            )
        )

    async def health(request: Request) -> JSONResponse:
        live = request.url.path == "/healthz" or any(t.session.expires_at > datetime.now(UTC) for t in tasks)
        return JSONResponse({"ready": live}, status_code=200 if live else 503)

    routes.extend([Route("/healthz", health), Route("/readyz", health)])

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            try:
                values = [_credential(t.spec.credential_file) for t in tasks]
                if len(set(values)) != len(values):
                    raise ValueError("Route credentials must be distinct")
                stack.push_async_callback(reporter.close)
                stack.push_async_callback(client.close)
                await client.start()
                await reporter.start()
                for task in tasks:
                    await task.prepare()
                for child in children:
                    await stack.enter_async_context(child.router.lifespan_context(child))
            except Exception as exc:
                raise RuntimeError(
                    f"Gateway startup rejected ({type(exc).__name__}); private details suppressed"
                ) from None
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def create_app() -> Starlette:
    try:
        config = GatewayConfig.from_file(Path(os.environ["MARTIAN_GATEWAY_CONFIG"]))
        return build_app(config)
    except (OSError, ValueError, KeyError):
        raise RuntimeError("Gateway configuration rejected; private details suppressed") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="Customer-managed task-bound MCP gateway")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--print-schema", action="store_true")
    args = parser.parse_args()
    if args.print_schema:
        import json

        print(json.dumps(GatewayConfig.model_json_schema(), indent=2))
        return
    if args.check_config:
        config = GatewayConfig.from_file(Path(os.environ["MARTIAN_GATEWAY_CONFIG"]))
        print(f"Valid gateway schema: {len(config.tasks)} task routes; no network validation performed")
        return
    import uvicorn

    config = GatewayConfig.from_file(Path(os.environ["MARTIAN_GATEWAY_CONFIG"]))
    uvicorn.run(
        "martian_redteam.gateway:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("MARTIAN_GATEWAY_PORT", "8400")),
        workers=1,
        access_log=False,
        log_level="warning",
        proxy_headers=False,
        ssl_certfile=str(config.tls_cert_file) if config.tls_cert_file else None,
        ssl_keyfile=str(config.tls_key_file) if config.tls_key_file else None,
        limit_concurrency=64,
        timeout_keep_alive=5,
    )
