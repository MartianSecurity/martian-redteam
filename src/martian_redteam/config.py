"""Environment and YAML configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """API location and credentials. Loaded from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="MARTIAN_",
        env_file=".env",
        extra="ignore",
    )

    api_url: str = "https://api.agents.martiansecurity.ai"
    api_key: SecretStr = Field(default=SecretStr(""))
    request_timeout_seconds: float = 120.0


class CallbackRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    function: str = "model_callback"


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str
    callback: CallbackRef


class NamedSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    types: list[str] | None = None
    weight: float | None = None


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent: int = 2
    attacks_per_vulnerability_type: int = 1
    output_folder: str = "results"

    @field_validator("max_concurrent")
    @classmethod
    def concurrent_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_concurrent must be >= 1")
        return value


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_on_infrastructure_error: bool = True
    pass_rate_threshold: float = 1.0


class RunConfig(BaseModel):
    """YAML file loaded by `martian-redteam run`."""

    model_config = ConfigDict(extra="forbid")

    target: TargetConfig
    system_config: SystemConfig = Field(default_factory=SystemConfig)
    vulnerabilities: list[NamedSpec]
    attacks: list[NamedSpec]
    gate: GateConfig = Field(default_factory=GateConfig)
    api: dict[str, Any] | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> Self:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config YAML must be a mapping")
        return cls.model_validate(raw)


CatalogKind = Literal["vulnerability", "attack"]
