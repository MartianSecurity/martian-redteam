"""Customer-pinned operation identities for multiplexed MCP tools. No inference."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from martian_redteam.call_gate_schema import CallTarget

Token = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:/+\-]{1,128}$")]


class OperationResolutionError(ValueError):
    """A configured dispatcher could not be resolved; it must not be called."""


class DispatcherRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    system: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")]
    tool: Token
    selector: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...] = Field(min_length=1, max_length=8)
    operations: tuple[Token, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_operations(self) -> Self:
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("Dispatcher operations must be unique")
        for operation in self.operations:
            CallTarget(kind="tool", system=self.system, name=f"{self.tool}@{operation}")
        return self

    def resolve(self, arguments: dict[str, Any]) -> str:
        value: Any = arguments
        for key in self.selector:
            if not isinstance(value, dict) or key not in value:
                raise OperationResolutionError("Configured dispatcher selector is missing")
            value = value[key]
        if not isinstance(value, str) or value not in self.operations:
            raise OperationResolutionError("Configured dispatcher operation is not approved")
        return f"{self.tool}@{value}"


class ToolOperationPolicy(BaseModel):
    """Load from trusted deployment configuration, never tool descriptions or model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    dispatchers: tuple[DispatcherRule, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_dispatchers(self) -> Self:
        keys = [(rule.system, rule.tool) for rule in self.dispatchers]
        if len(set(keys)) != len(keys):
            raise ValueError("Only one rule may resolve each dispatcher")
        return self

    def resolve(self, system: str, tool: str, arguments: dict[str, Any]) -> str:
        for rule in self.dispatchers:
            if (rule.system, rule.tool) == (system, tool):
                return rule.resolve(arguments)
        return tool

    def validate_binding(self, system: str, selected_tools: list[str], advertised_tools: set[str]) -> None:
        for rule in self.dispatchers:
            if rule.system != system:
                continue
            if rule.tool not in selected_tools:
                continue
            if any(f"{rule.tool}@{operation}" in advertised_tools for operation in rule.operations):
                raise OperationResolutionError("Resolved operation collides with an advertised tool")


def snapshot_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Own the validated bytes before awaiting a gate; caller mutations cannot change execution."""

    def check(value: Any) -> None:
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("MCP argument object keys must be strings")
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif value is not None and type(value) not in (str, int, float, bool):
            raise ValueError("MCP arguments must contain only JSON values")

    if not isinstance(arguments, dict):
        raise ValueError("MCP arguments must be an object")
    check(arguments)
    encoded = json.dumps(arguments, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) > 256000:
        raise ValueError("MCP tool arguments exceed 256 KB")
    result: dict[str, Any] = json.loads(encoded)
    return result
