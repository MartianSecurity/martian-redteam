"""Shared YAML assessment execution; independent of CLI rendering and CI persistence."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from martian_redteam.catalog.attacks import Attack, get_attack
from martian_redteam.catalog.vulnerabilities import Vulnerability, get_vulnerability
from martian_redteam.client import Client
from martian_redteam.config import NamedSpec, RunConfig, Settings
from martian_redteam.orchestrator import a_red_team
from martian_redteam.types import RedteamService, RiskAssessment, TestCase


def _load_callback(file_path: str, function_name: str) -> Any:
    path = Path(file_path).resolve()
    spec = importlib.util.spec_from_file_location("martian_redteam_target", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"cannot load callback file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    callback = getattr(module, function_name, None)
    if callback is None:
        raise AttributeError(f"{path} has no function {function_name}")
    return callback


def _vulnerability_from_spec(spec: NamedSpec) -> Vulnerability:
    return get_vulnerability(
        spec.name,
        spec.types,
        id=spec.id,
        criteria=spec.criteria,
        gated=spec.gated,
    )


def _resolve_catalog(config: RunConfig) -> tuple[list[Vulnerability], list[Attack]]:
    vulnerabilities = [_vulnerability_from_spec(item) for item in config.vulnerabilities]
    attacks = [get_attack(item.name, weight=item.weight) for item in config.attacks]
    return vulnerabilities, attacks


async def _run_async(
    config: RunConfig,
    settings: Settings | None = None,
    on_probe: Callable[[TestCase], None] | None = None,
    *,
    client: RedteamService | None = None,
) -> RiskAssessment:
    if client is not None:
        if settings is not None:
            raise ValueError("Choose one redteam connection; explicit clients cannot be mixed with legacy settings")
        config.require_platform_origin()
    else:
        settings = config.settings_overlay(settings if settings is not None else Settings())
    callback = _load_callback(config.target.callback.file, config.target.callback.function)
    vulnerabilities, attacks = _resolve_catalog(config)

    async def execute(service: RedteamService) -> RiskAssessment:
        return await a_red_team(
            target=callback,
            vulnerabilities=vulnerabilities,
            attacks=attacks,
            client=service,
            purpose=config.target.purpose,
            max_concurrent=config.system_config.max_concurrent,
            attacks_per_vulnerability_type=config.system_config.attacks_per_vulnerability_type,
            target_timeout_seconds=config.system_config.target_timeout_seconds,
            on_probe=on_probe,
        )

    if client is not None:
        return await execute(client)
    async with Client(settings=settings) as legacy:
        return await execute(legacy)
