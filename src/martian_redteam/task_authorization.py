"""Local, application-issued task capabilities for an explicitly bound MCP session.

This is an in-process enforcement boundary, not a signature/verifier for untrusted
issuers. Keep TaskGuard and its configuration outside the model's tool surface.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from martian_redteam.task_authorization_schema import (
    ActionGrant as ActionGrant,
)
from martian_redteam.task_authorization_schema import (
    DecimalArgument as DecimalArgument,
)
from martian_redteam.task_authorization_schema import (
    ExactArgument as ExactArgument,
)
from martian_redteam.task_authorization_schema import (
    TaskAuthorization as TaskAuthorization,
)
from martian_redteam.task_authorization_schema import (
    TextArgument as TextArgument,
)
from martian_redteam.task_authorization_schema import (
    _canonical,
)

if TYPE_CHECKING:
    from martian_redteam.runtime_policy import RuntimeTask


class TaskDenied(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Task authorization denied: {reason}")


class TaskGuard:
    """Shared by every turn of one session in one process. No await between reserve and entry."""

    def __init__(
        self, authorization: TaskAuthorization, *, session_id: str, issuer: str, authority: RuntimeTask | None = None
    ) -> None:
        # Own all nested exact values, including arrays, rather than caller-owned model objects.
        self._authorization = TaskAuthorization.model_validate_json(authorization.model_dump_json())
        if authority is not None:
            authority.validate(self._authorization)
        self.authority = authority
        self._session_id = session_id
        self._issuer = issuer
        self._task_id = uuid4()
        self._key = secrets.token_bytes(32)
        self._revoked = False
        self._used: dict[UUID, int] = {}
        self._calls: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def revoke(self) -> None:
        with self._lock:
            self._revoked = True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": 1,
                "task_id": str(self._task_id),
                "remote_task_id": self.authority.task_id if self.authority else None,
                "issuer": self._issuer,
                "principal_ref": str(self._authorization.principal_ref),
                "policy_ref": str(self._authorization.policy_ref),
                "expires_at": self._authorization.expires_at.isoformat(),
                "revoked": self._revoked,
                "expired": datetime.now(UTC) >= self._authorization.expires_at,
                "grants": [
                    {"grant_id": str(g.grant_id), "remaining_calls": g.max_calls - self._used.get(g.grant_id, 0)}
                    for g in self._authorization.grants
                ],
            }

    def check(
        self,
        *,
        session_id: str,
        system: str,
        tool: str,
        resolved_tool: str,
        arguments: dict[str, Any],
        action_id: str,
        call_id: str,
        consume: bool = False,
    ) -> dict[str, str]:
        with self._lock:
            if session_id != self._session_id:
                raise TaskDenied("session_mismatch")
            if self._revoked:
                raise TaskDenied("revoked")
            if datetime.now(UTC) >= self._authorization.expires_at:
                raise TaskDenied("expired")
            if not action_id or not call_id or len(action_id) > 256 or len(call_id) > 256:
                raise TaskDenied("missing_call_identity")
            if (action_id, call_id) in self._calls:
                raise TaskDenied("call_replayed")
            targets = [
                g
                for g in self._authorization.grants
                if (g.system, g.tool, g.resolved_tool) == (system, tool, resolved_tool)
            ]
            if not targets:
                raise TaskDenied("operation_out_of_scope")
            matching = [g for g in targets if g.permits(arguments)]
            if not matching:
                raise TaskDenied("arguments_out_of_scope")
            grant = next((g for g in matching if self._used.get(g.grant_id, 0) < g.max_calls), None)
            if grant is None:
                raise TaskDenied("grant_exhausted")
            if not consume:
                return {}
            self._used[grant.grant_id] = self._used.get(grant.grant_id, 0) + 1
            self._calls.add((action_id, call_id))
            # Local correlation only: no low-entropy raw argument hash and no reusable permit token.
            binding = _canonical(
                {
                    "task_id": str(self._task_id),
                    "session_id": session_id,
                    "principal_ref": str(self._authorization.principal_ref),
                    "policy_ref": str(self._authorization.policy_ref),
                    "grant_id": str(grant.grant_id),
                    "system": system,
                    "tool": tool,
                    "resolved_tool": resolved_tool,
                    "action_id": action_id,
                    "call_id": call_id,
                    "arguments": arguments,
                }
            )
            return {
                "task_id": str(self._task_id),
                "grant_id": str(grant.grant_id),
                "decision_id": str(uuid4()),
                "action_hmac": hmac.new(self._key, binding.encode(), hashlib.sha256).hexdigest(),
            }
