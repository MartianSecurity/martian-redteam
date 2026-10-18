"""Opt-in application-reported control decisions; mirrored by the backend."""

import uuid
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# Fixed codes are the only reason text allowed across this boundary.
DECISION_REASONS: dict[str, tuple[str, str]] = {
    "output_clear": ("output_policy", "admitted"),
    **{
        reason: ("output_policy", "denied")
        for reason in (
            "output_restricted",
            "output_over_budget",
            "output_invalid",
        )
    },
    "scope_allowed": ("local_task", "admitted"),
    **{
        reason: ("local_task", "denied")
        for reason in (
            "session_mismatch",
            "revoked",
            "expired",
            "missing_call_identity",
            "call_replayed",
            "operation_out_of_scope",
            "arguments_out_of_scope",
            "grant_exhausted",
        )
    },
    **{
        f"central_{reason}": ("shared_task", "denied")
        for reason in (
            "task_revoked",
            "task_expired",
            "policy_inactive",
            "operation_out_of_scope",
            "budget_exhausted",
            "task_suspended",
            "principal_suspended",
            "approval_required",
            "approval_expired",
            "approval_rejected",
            "approval_revoked",
            "action_context_required",
            "action_mismatch",
        )
    },
    "task_authority_unavailable": ("shared_task", "unavailable"),
    "call_gate_blocked": ("call_gate", "denied"),
    "call_gate_unavailable": ("call_gate", "unavailable"),
}


class SecurityDecisionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: uuid.UUID
    occurred_at: AwareDatetime
    trace_id: uuid.UUID
    kind: Literal["security_decision"] = "security_decision"
    target_kind: Literal["model", "tool", "output"]
    system: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/@+\-]+$")]
    control: Literal["local_task", "shared_task", "call_gate", "output_policy"]
    decision: Literal["admitted", "denied", "unavailable"]
    reason: Annotated[str, Field(min_length=1, max_length=64)]
    task_ref: uuid.UUID | None = None
    remote_task_ref: uuid.UUID | None = None
    policy_ref: uuid.UUID | None = None

    @model_validator(mode="after")
    def consistent_decision(self) -> Self:
        if DECISION_REASONS.get(self.reason) != (self.control, self.decision):
            raise ValueError("Unknown or inconsistent decision reason")
        if self.control == "output_policy":
            if (
                self.target_kind != "output"
                or self.policy_ref is None
                or self.task_ref is not None
                or self.remote_task_ref is not None
            ):
                raise ValueError("Output decisions require a policy reference and no task references")
        elif self.target_kind == "output":
            raise ValueError("Output targets require an output policy decision")
        elif self.control != "call_gate":
            if (
                (
                    self.target_kind != "tool"
                    and not (
                        self.control == "shared_task"
                        and self.target_kind == "model"
                        and self.system == "runtime"
                        and self.name == "session_turn"
                    )
                )
                or self.task_ref is None
                or self.policy_ref is None
            ):
                raise ValueError("Task decisions require a tool and task/policy references")
            if self.control == "shared_task" and self.remote_task_ref is None:
                raise ValueError("Shared task decisions require a remote task reference")
        elif any(ref is not None for ref in (self.task_ref, self.remote_task_ref, self.policy_ref)):
            raise ValueError("Call gate reports do not carry task references")
        return self


class SecurityDecisionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    events: list[SecurityDecisionEvent] = Field(min_length=1, max_length=25)
