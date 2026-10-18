"""Fresh, deployment-scoped task decisions backed by the control plane's durable budgets."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from martian_redteam.call_gate_schema import CallTarget, GateModel
from martian_redteam.runtime_policy_schema import (
    ActionContext,
    OperationBudget,
    PolicyChannelAssignment,
    TaskActionCall,
    TaskAssignment,
    TaskCall,
    TaskDecision,
    TaskIssue,
)
from martian_redteam.task_authorization import TaskAuthorization, TaskDenied


def _budgets(authorization: TaskAuthorization) -> tuple[OperationBudget, ...]:
    counts: dict[CallTarget, int] = {}
    for grant in authorization.grants:
        target = CallTarget(kind="tool", system=grant.system, name=grant.resolved_tool)
        counts[target] = counts.get(target, 0) + grant.max_calls
    return tuple(OperationBudget(target=t, max_calls=n) for t, n in counts.items())


class TaskAuthorityUnavailable(TaskDenied):
    """No usable fresh decision; the call must not enter the tool transport."""

    def __init__(self) -> None:
        super().__init__("task_authority_unavailable")


class RuntimeTaskClient:
    def __init__(
        self, backend_url: str, assignment: PolicyChannelAssignment, token_file: Path, *, allow_http: bool = False,
        action_review_targets: tuple[CallTarget, ...] = (),
    ) -> None:
        origin = urlsplit(backend_url)
        if (
            not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
            or origin.scheme not in ({"https", "http"} if allow_http else {"https"})
        ):
            raise ValueError("Task policy requires a trusted HTTPS origin; HTTP requires explicit local configuration")
        self.assignment = PolicyChannelAssignment.model_validate(assignment.model_dump())
        if len(action_review_targets) > 32 or any(t.kind != "tool" for t in action_review_targets):
            raise ValueError("Action review export supports at most 32 explicit tool targets")
        self.action_review_targets = frozenset(action_review_targets)
        self._url = f"{backend_url.rstrip('/')}/v1/runtime-security/runtime/{assignment.channel_id}"
        self._token_file = token_file
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is not None:
            raise RuntimeError("Task client already started")
        self._credential()
        self._client = httpx.AsyncClient(timeout=2, follow_redirects=False, trust_env=False)

    def _credential(self) -> str:
        if self._token_file.stat().st_size > 1024:
            raise ValueError("Task credential file exceeds limit")
        token = self._token_file.read_text().strip()
        if not token.startswith("mp_") or not 32 <= len(token) <= 128:
            raise ValueError("Invalid task credential file")
        return token

    async def _post(self, path: str, body: GateModel | None = None) -> bytes:
        if self._client is None:
            raise TaskAuthorityUnavailable()
        try:
            async with asyncio.timeout(3):
                async with self._client.stream(
                    "POST",
                    f"{self._url}/{path}",
                    headers={"Authorization": f"Bearer {self._credential()}", "Content-Type": "application/json"},
                    content=body.model_dump_json() if body else "{}",
                ) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 32768:
                            raise ValueError("Task response exceeds limit")
                        data.extend(chunk)
                    return bytes(data)
        except (httpx.HTTPError, OSError, ValueError, TimeoutError):
            # No credentials, origin, content or exception chain in public errors.
            raise TaskAuthorityUnavailable() from None

    async def issue(self, authorization: TaskAuthorization) -> RuntimeTask:
        body = TaskIssue(
            request_id=uuid4(),
            policy_id=authorization.policy_ref,
            principal_ref=authorization.principal_ref,
            expires_at=authorization.expires_at,
            operations=_budgets(authorization),
        )
        try:
            assignment = TaskAssignment.model_validate_json(await self._post("tasks", body))
            if TaskIssue.model_validate(assignment.model_dump(include=set(TaskIssue.model_fields))) != body:
                raise ValueError("Task issuance mismatch")
            return self.attach(authorization, assignment)
        except ValueError:
            raise TaskAuthorityUnavailable() from None

    def attach(self, authorization: TaskAuthorization, assignment: TaskAssignment) -> RuntimeTask:
        """Explicitly reuse an issued task across workers; this does not issue a new budget."""
        if (
            assignment.channel_id != self.assignment.channel_id
            or assignment.binding_id != self.assignment.binding_id
            or assignment.policy_id != authorization.policy_ref
            or assignment.principal_ref != authorization.principal_ref
            or assignment.expires_at != authorization.expires_at
            or assignment.operations != _budgets(authorization)
        ):
            raise ValueError("Task assignment does not match local authorization or deployment")
        return RuntimeTask(self, assignment, authorization)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class RuntimeTask:
    def __init__(self, client: RuntimeTaskClient, assignment: TaskAssignment, authorization: TaskAuthorization) -> None:
        self._client = client
        self.assignment = TaskAssignment.model_validate(assignment.model_dump())
        self._authorization = authorization.model_dump_json()

    def validate(self, authorization: TaskAuthorization) -> None:
        if authorization.model_dump_json() != self._authorization:
            raise ValueError("Remote task and local argument authorization must match")

    @property
    def task_id(self) -> str:
        return str(self.assignment.task_id)

    async def before_call(
        self, target: CallTarget, *, transport_name: str | None = None, arguments: dict[str, Any] | None = None,
        destination: str | None = None, host_header: str | None = None, tool_sha256: str | None = None,
    ) -> None:
        if target not in self._client.action_review_targets:
            await self._decision("decide", target)
            return
        if arguments is None or transport_name is None or destination is None or tool_sha256 is None:
            raise TaskDenied("central_action_context_required")
        try:
            action = ActionContext(transport_name=transport_name, destination=destination,
                host_header=host_header, tool_sha256=tool_sha256, arguments_json=json.dumps(
                arguments, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            ))
        except ValueError:
            raise TaskAuthorityUnavailable() from None
        await self._decision("decide-action", target, action=action)

    async def before_turn(self) -> None:
        """Check a new session turn without spending an operation allowance."""
        await self._decision("check", CallTarget(kind="model", system="runtime", name="session_turn"))

    async def _decision(self, operation: str, target: CallTarget, *, action: ActionContext | None = None) -> None:
        body = TaskCall(request_id=uuid4(), target=target)
        request_body = TaskActionCall(**body.model_dump(), action=action) if action is not None else body
        try:
            decision = TaskDecision.model_validate_json(
                await self._client._post(f"tasks/{self.task_id}/{operation}", request_body)
            )
            now = datetime.now(UTC)
            if (
                TaskCall.model_validate(decision.model_dump(include=set(TaskCall.model_fields))) != body
                or decision.task_id != self.assignment.task_id
                or decision.channel_id != self.assignment.channel_id
                or decision.policy_id != self.assignment.policy_id
                or decision.expires_at != self.assignment.expires_at
                or abs((now - decision.decided_at).total_seconds()) > 5
                or (decision.allowed and decision.expires_at <= now + timedelta(milliseconds=1))
            ):
                raise ValueError("Task decision mismatch or stale response")
        except ValueError:
            raise TaskAuthorityUnavailable() from None
        if not decision.allowed:
            raise TaskDenied(f"central_{decision.reason}")

    async def revoke(self) -> None:
        await self._client._post(f"tasks/{self.task_id}/revoke")
