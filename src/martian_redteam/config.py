"""Environment and YAML configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator
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
    request_timeout_seconds: float = Field(default=120.0, ge=1.0, le=240.0)

    @field_validator("api_url")
    @classmethod
    def api_url_must_be_http(cls, value: str) -> str:
        trimmed = value.strip().rstrip("/")
        if not trimmed.startswith(("http://", "https://")):
            raise ValueError("MARTIAN_API_URL must start with http:// or https://")
        return trimmed


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
    id: str | None = None
    criteria: str | None = None
    gated: bool | None = None


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent: int = 2
    attacks_per_vulnerability_type: int = 1
    output_folder: str = "results"
    # Upper bound for one target callback call; a hung target becomes a target error.
    target_timeout_seconds: float = Field(default=120.0, ge=1.0)

    @field_validator("max_concurrent")
    @classmethod
    def concurrent_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_concurrent must be >= 1")
        return value

    @field_validator("attacks_per_vulnerability_type")
    @classmethod
    def attacks_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("attacks_per_vulnerability_type must be >= 1")
        return value


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_on_infrastructure_error: bool = True
    ignore_target_errors: bool = False
    pass_rate_threshold: float = 1.0

    @field_validator("pass_rate_threshold")
    @classmethod
    def threshold_must_be_unit_interval(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("pass_rate_threshold must be between 0 and 1")
        return value


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl | None = None


class RunConfig(BaseModel):
    """YAML file loaded by `martian-redteam run`."""

    model_config = ConfigDict(extra="forbid")

    target: TargetConfig
    system_config: SystemConfig = Field(default_factory=SystemConfig)
    vulnerabilities: list[NamedSpec]
    attacks: list[NamedSpec]
    gate: GateConfig = Field(default_factory=GateConfig)
    api: ApiConfig | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> Self:
        config_path = Path(path).resolve()
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config YAML must be a mapping")
        config = cls.model_validate(raw)
        callback_file = Path(config.target.callback.file)
        if not callback_file.is_absolute():
            callback_file = (config_path.parent / callback_file).resolve()
        callback = config.target.callback.model_copy(update={"file": str(callback_file)})
        target = config.target.model_copy(update={"callback": callback})
        return config.model_copy(update={"target": target})

    def settings_overlay(self, settings: Settings) -> Settings:
        if self.api is None or self.api.url is None:
            return settings
        return settings.model_copy(update={"api_url": str(self.api.url).rstrip("/")})
