"""Customer SDK call gate v1; mirrored in martian-redteam without backend imports."""

import uuid
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class GateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CallTarget(GateModel):
    kind: Literal["model", "tool"]
    system: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/@+\-]+$")]


class GateAssignment(GateModel):
    schema_version: Literal[1] = 1
    gate_id: uuid.UUID
    binding_id: uuid.UUID
    targets: tuple[CallTarget, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def distinct_targets(self) -> Self:
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("Duplicate gate targets")
        return self


class GateHeartbeat(GateAssignment):
    process_id: uuid.UUID


class DecisionRequest(GateModel):
    schema_version: Literal[1] = 1
    binding_id: uuid.UUID
    process_id: uuid.UUID
    request_id: uuid.UUID
    target: CallTarget


class CallDecision(DecisionRequest):
    gate_id: uuid.UUID
    allowed: bool
    decided_at: AwareDatetime
    control_id: uuid.UUID | None
    expires_at: AwareDatetime | None

    @model_validator(mode="after")
    def consistent_decision(self) -> Self:
        if self.allowed != (self.control_id is None and self.expires_at is None):
            raise ValueError("Invalid decision")
        if not self.allowed and (self.control_id is None or self.expires_at is None):
            raise ValueError("Denied decisions require a control and expiry")
        return self


class BlockReceipt(GateModel):
    schema_version: Literal[1] = 1
    process_id: uuid.UUID
    request_id: uuid.UUID
    outcome: Literal["blocked_before_call"] = "blocked_before_call"
