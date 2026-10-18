"""Runtime task-policy v1: metadata only, mirrored in the customer SDK."""
import json
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from martian_redteam.call_gate_schema import CallTarget, GateModel


class OperationBudget(GateModel):
    target: CallTarget
    max_calls: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def tools_only(self) -> Self:
        if self.target.kind != "tool":
            raise ValueError("Task policies support tools only")
        return self


class PolicyOperation(OperationBudget):
    require_approval: bool = False
    approval_scope: Literal["operation", "exact_action"] = "operation"

    @model_validator(mode="after")
    def single_action(self) -> Self:
        if self.approval_scope == "exact_action" and (not self.require_approval or self.max_calls != 1):
            raise ValueError("Exact action review requires approval and a one-call budget")
        return self


class PolicySpec(GateModel):
    schema_version: Literal[1, 2, 3] = 1
    name: str = Field(min_length=3, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")
    max_task_seconds: int = Field(ge=30, le=86400)
    operations: tuple[PolicyOperation, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def distinct(self) -> Self:
        if self.schema_version == 1 and any(x.require_approval for x in self.operations):
            raise ValueError("Human approval requires policy schema 2")
        if self.schema_version < 3 and any(x.approval_scope != "operation" for x in self.operations):
            raise ValueError("Exact action review requires policy schema 3")
        if len({x.target for x in self.operations}) != len(self.operations):
            raise ValueError("Duplicate operations")
        return self


class PolicyChannelAssignment(GateModel):
    schema_version: Literal[1] = 1
    channel_id: UUID
    binding_id: UUID


class TaskIssue(GateModel):
    schema_version: Literal[1] = 1
    request_id: UUID
    policy_id: UUID
    principal_ref: UUID
    expires_at: AwareDatetime
    operations: tuple[OperationBudget, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def distinct(self) -> Self:
        if len({x.target for x in self.operations}) != len(self.operations):
            raise ValueError("Duplicate operations")
        return self


class TaskAssignment(TaskIssue):
    task_id: UUID
    channel_id: UUID
    binding_id: UUID


class TaskCall(GateModel):
    schema_version: Literal[1] = 1
    request_id: UUID
    target: CallTarget


class ActionContext(GateModel):
    """Explicitly exported complete arguments, never telemetry or model instructions."""

    transport_name: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/@+\-]+$")
    destination: str = Field(min_length=1, max_length=512, pattern=r"^[\x21-\x7e]+$")
    host_header: str | None = Field(default=None, max_length=256, pattern=r"^[A-Za-z0-9.\-]+(?::[0-9]{1,5})?$")
    tool_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    arguments_json: str = Field(max_length=3072, repr=False)

    @model_validator(mode="after")
    def canonical_object(self) -> Self:
        endpoint = urlsplit(self.destination)
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("Exact review requires a destination without URL credentials, query or fragment")
        try:
            value = json.loads(self.arguments_json)
        except RecursionError:
            raise ValueError("Action arguments exceed the nesting limit") from None
        if not isinstance(value, dict):
            raise ValueError("Action arguments must be an object")
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 16:
                raise ValueError("Action arguments exceed the nesting limit")
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if canonical != self.arguments_json or len(canonical.encode()) > 3072:
            raise ValueError("Action arguments must be canonical JSON within 3 KiB")
        if len(self.model_dump_json().encode()) > 4096:
            raise ValueError("Action context envelope exceeds 4 KiB")
        return self


class TaskActionCall(TaskCall):
    action: ActionContext = Field(repr=False)


class TaskDecision(TaskCall):
    task_id: UUID
    channel_id: UUID
    policy_id: UUID
    allowed: bool
    reason: Literal[
        "allowed", "task_revoked", "task_expired", "policy_inactive", "operation_out_of_scope", "budget_exhausted",
        "task_suspended", "principal_suspended", "approval_required", "approval_expired", "approval_rejected",
        "approval_revoked", "action_context_required", "action_mismatch",
    ]
    decided_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.allowed != (self.reason == "allowed"):
            raise ValueError("Inconsistent task decision")
        return self
