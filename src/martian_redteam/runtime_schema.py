"""Metadata-only application reports; independently packaged in the customer SDK."""

import uuid
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

RuntimeName = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/@+\-]+$")]


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    occurred_at: AwareDatetime
    trace_id: uuid.UUID
    parent_event_id: uuid.UUID | None = None
    kind: Literal["model", "tool"]
    system: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")]
    name: RuntimeName
    outcome: Literal["success", "error", "unknown"]
    duration_ms: int = Field(ge=0, le=86400000)


class RuntimeBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    events: list[RuntimeEvent] = Field(min_length=1, max_length=100)


ResourceName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")]
ResourceAction = Literal["read", "evaluate", "create", "update", "delete", "execute"]


class ResourceAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: ResourceName
    resource_type: ResourceName
    resource_ref: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    action: ResourceAction


class ResourceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["not_reported", "reported", "partial"] = "not_reported"
    items: list[ResourceAccess] = Field(default_factory=list, max_length=10)
    omitted: int = Field(default=0, ge=0, le=1000000)

    @model_validator(mode="after")
    def consistent_coverage(self) -> Self:
        if self.status == "not_reported" and (self.items or self.omitted):
            raise ValueError("Unreported resources cannot contain references or omissions")
        if self.status == "reported" and self.omitted:
            raise ValueError("Complete resource mapping cannot contain omissions")
        if self.status == "partial" and not self.omitted:
            raise ValueError("Partial resource mapping requires an omission count")
        keys = [(r.service, r.resource_type, r.resource_ref, r.action) for r in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate resource references")
        return self


class ResourceRuntimeEvent(RuntimeEvent):
    resource_report: ResourceReport = Field(default_factory=ResourceReport)

    @model_validator(mode="after")
    def tool_resources_only(self) -> Self:
        if self.kind != "tool" and self.resource_report.status != "not_reported":
            raise ValueError("Only tool calls can report resource references")
        if self.outcome != "success" and self.resource_report.status != "not_reported":
            raise ValueError("Resource response mapping requires a successful tool response")
        return self


class ResourceRuntimeBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3] = 3
    events: list[ResourceRuntimeEvent] = Field(min_length=1, max_length=25)


KubernetesLabel = Annotated[str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")]
KubernetesName = Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")]


class KubernetesRequest(BaseModel):
    """Caller-declared identity and one named, namespaced primary-resource GET.

    This is metadata, not credential attestation or a request for backend execution.
    Raw names leave the customer application only with explicit v4 opt-in.
    """

    model_config = ConfigDict(extra="forbid")

    cluster_id: KubernetesLabel
    service_account_namespace: KubernetesLabel
    service_account_name: KubernetesName
    namespace: KubernetesLabel
    api_group: Annotated[str, Field(max_length=253, pattern=r"^$|^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")]
    resource: KubernetesLabel
    name: KubernetesName
    verb: Literal["get"] = "get"


class NativeRuntimeEvent(ResourceRuntimeEvent):
    kubernetes_request: KubernetesRequest | None = None

    @model_validator(mode="after")
    def native_tool_only(self) -> Self:
        if self.kubernetes_request is not None and self.kind != "tool":
            raise ValueError("Only tool calls can declare a Kubernetes request")
        return self


class NativeRuntimeBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[4] = 4
    events: list[NativeRuntimeEvent] = Field(min_length=1, max_length=25)
