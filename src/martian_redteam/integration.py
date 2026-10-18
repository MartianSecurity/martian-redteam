"""Customer-side session configuration and bounded, opt-in execution traces."""

from __future__ import annotations

import json
from collections import deque
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, field_validator

if TYPE_CHECKING:
    from martian_redteam.mcp_binding import BoundMCPTools
    from martian_redteam.runtime import RuntimeTrace
    from martian_redteam.task_authorization import TaskGuard
    from martian_redteam.tool_operations import ToolOperationPolicy

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


class MCPConnection(BaseModel):
    """One externally supplied Streamable HTTP MCP server; no server implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")]
    url: str = Field(max_length=4096)
    headers: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    allowed_tools: list[str] = Field(min_length=1, max_length=128)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("MCP URL must be HTTP(S), without embedded user credentials")
        if parsed.fragment:
            raise ValueError("MCP URL must not have a fragment")
        return value

    @field_validator("allowed_tools")
    @classmethod
    def unique_tools(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not name.strip() for name in value):
            raise ValueError("allowed_tools must contain unique nonempty names")
        return value


class AssessmentSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: Identifier
    campaign_id: Identifier
    target_id: Identifier
    expires_at: AwareDatetime
    mcp_connections: list[MCPConnection] = Field(min_length=1, max_length=16)

    @field_validator("mcp_connections")
    @classmethod
    def unique_connections(cls, value: list[MCPConnection]) -> list[MCPConnection]:
        if len({connection.name for connection in value}) != len(value):
            raise ValueError("MCP connection names must be unique")
        return value

    def ensure_active(self) -> None:
        if datetime.now(UTC) >= self.expires_at:
            raise ValueError("assessment session has expired")


class TraceRecorder:
    """Local bounded trace buffer. No background export or application database."""

    def __init__(
        self, *, capture_payloads: bool = False, max_events: int = 1000, secrets: list[str] | None = None
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.capture_payloads = capture_payloads
        self._secrets = sorted((s for s in secrets or [] if s), key=len, reverse=True)
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._sequence = 0

    def protect_secrets(self, values: list[str]) -> None:
        self._secrets = sorted(set(self._secrets).union(value for value in values if value), key=len, reverse=True)

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]"
                if str(key).lower()
                in {
                    "authorization",
                    "password",
                    "secret",
                    "token",
                    "api_key",
                    "access_token",
                    "refresh_token",
                    "x-api-key",
                    "x-admin-token",
                }
                else self._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        return value

    def record(self, kind: str, *, payload: Any = None, **metadata: Any) -> None:
        self._sequence += 1
        event = {"sequence": self._sequence, "time": datetime.now(UTC).isoformat(), "kind": kind, **metadata}
        if self.capture_payloads and payload is not None:
            rendered = json.dumps(self._redact(payload), default=str, ensure_ascii=False)
            event["payload"] = rendered[:16000]
            event["payload_truncated"] = len(rendered) > 16000
        self._events.append(self._redact(event))

    def snapshot(self) -> dict[str, Any]:
        return {"events": list(self._events), "dropped_events": self._sequence - len(self._events)}


def parse_session_payload(payload: dict[str, Any] | AssessmentSession) -> AssessmentSession:
    """Parse and validate a run-scoped Martian assessment session payload."""
    if isinstance(payload, AssessmentSession):
        payload.ensure_active()
        return payload
    session = AssessmentSession.model_validate(payload)
    session.ensure_active()
    return session


def bind_tools(
    session: AssessmentSession,
    *,
    trace: TraceRecorder | None = None,
    action_id: str = "",
    runtime: RuntimeTrace | None = None,
    operations: ToolOperationPolicy | None = None,
    authorization: TaskGuard | None = None,
) -> AbstractAsyncContextManager[BoundMCPTools]:
    """Open all session MCP connections in one async scope. Requires the ``mcp`` extra.

    Usage: ``async with bind_tools(session) as tools: ...``. This discovers explicitly
    allowed tools; it does not patch arbitrary framework tools or remap API schemas.
    """
    from martian_redteam.mcp_binding import open_tools

    return open_tools(
        session, trace=trace, action_id=action_id, runtime=runtime, operations=operations, authorization=authorization
    )


def bind_sandbox_mcp(
    payload: dict[str, Any] | AssessmentSession,
    *,
    trace: TraceRecorder | None = None,
    action_id: str = "",
    runtime: RuntimeTrace | None = None,
    operations: ToolOperationPolicy | None = None,
    authorization: TaskGuard | None = None,
) -> AbstractAsyncContextManager[BoundMCPTools]:
    """Validate a run-scoped session payload and bind its expiring sandbox MCP gateways."""
    session = parse_session_payload(payload)
    return bind_tools(
        session, trace=trace, action_id=action_id, runtime=runtime, operations=operations, authorization=authorization
    )
