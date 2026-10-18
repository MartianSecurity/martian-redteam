"""Strict task scope wire contract, mirrored in the backend; no execution authority."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from .call_gate_schema import CallTarget

ArgumentPath = tuple[Annotated[str, Field(min_length=1, max_length=128)], ...]
DecimalText = Annotated[str, Field(pattern=r"^-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,6})?$")]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: ArgumentPath = Field(min_length=1, max_length=8)
    required: bool = True


class ExactArgument(_Rule):
    kind: Literal["exact"] = "exact"
    value: JsonValue

    @model_validator(mode="after")
    def leaf_value(self) -> Self:
        if isinstance(self.value, dict) and self.value:
            raise ValueError("Declare object fields individually; exact objects must be empty")
        if len(_canonical(self.value).encode()) > 8192:
            raise ValueError("Exact argument exceeds 8 KiB")
        return self

    def permits(self, value: Any) -> bool:
        return _canonical(value) == _canonical(self.value)


class DecimalArgument(_Rule):
    """Exact decimal arithmetic. No float, exponent, whitespace or boolean coercion."""

    kind: Literal["decimal"] = "decimal"
    minimum: DecimalText
    maximum: DecimalText
    places: int = Field(ge=0, le=6)

    @model_validator(mode="after")
    def ordered_bounds(self) -> Self:
        if Decimal(self.minimum) > Decimal(self.maximum):
            raise ValueError("Decimal minimum exceeds maximum")
        return self

    def permits(self, value: Any) -> bool:
        if type(value) not in (str, int):
            return False
        rendered = str(value)
        if not re.fullmatch(r"-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,6})?", rendered):
            return False
        if "." in rendered and len(rendered.split(".")[1]) > self.places:
            return False
        return Decimal(self.minimum) <= Decimal(rendered) <= Decimal(self.maximum)


class TextArgument(_Rule):
    """Explicitly allow variable text in one leaf, for example a CRM note."""

    kind: Literal["text"] = "text"
    max_length: int = Field(ge=1, le=8192)

    def permits(self, value: Any) -> bool:
        return isinstance(value, str) and len(value) <= self.max_length


ArgumentRule = Annotated[ExactArgument | DecimalArgument | TextArgument, Field(discriminator="kind")]


class ActionGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    grant_id: UUID
    system: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")]
    tool: str = Field(min_length=1, max_length=256)
    resolved_tool: str = Field(min_length=1, max_length=256)
    max_calls: int = Field(ge=1, le=100)
    arguments: tuple[ArgumentRule, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        for name in (self.tool, self.resolved_tool):
            CallTarget(kind="tool", system=self.system, name=name)
        paths = [rule.path for rule in self.arguments]
        for index, path in enumerate(paths):
            if any(path[: len(other)] == other or other[: len(path)] == path for other in paths[index + 1 :]):
                raise ValueError("Argument paths must be unique and must not overlap")
        return self

    def permits(self, arguments: dict[str, Any]) -> bool:
        leaves: dict[tuple[str, ...], Any] = {}

        def visit(value: Any, path: tuple[str, ...]) -> None:
            if isinstance(value, dict) and value:
                for key, child in value.items():
                    visit(child, (*path, key))
            else:
                leaves[path] = value

        for key, value in arguments.items():
            visit(value, (key,))
        rules = {rule.path: rule for rule in self.arguments}
        if leaves.keys() - rules.keys():
            return False
        return all(rule.permits(leaves[path]) if path in leaves else not rule.required for path, rule in rules.items())


class TaskAuthorization(BaseModel):
    """Trusted host input. A principal reference is an issuer assertion, not an IdP check."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    principal_ref: UUID
    policy_ref: UUID
    expires_at: AwareDatetime
    grants: tuple[ActionGrant, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_grants(self) -> Self:
        if len({grant.grant_id for grant in self.grants}) != len(self.grants):
            raise ValueError("Grant IDs must be unique")
        if len(self.model_dump_json().encode()) > 65536:
            raise ValueError("Task authorization exceeds 64 KiB")
        return self
