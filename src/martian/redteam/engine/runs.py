"""Run a version 2 run file: build its target and its service, run the engine, keep the run.

:func:`build_target` makes the run file's target. This release runs the ``file`` and ``function``
target; an ``url`` or ``mcp`` target stops with :class:`TargetUnavailable`. :func:`build_service` picks
the service (:func:`~martian.redteam.engine.service.select_service`). :func:`execute` runs the engine
and, given a store, keeps the run there after every attempt, as the reports hold it
(:func:`~martian.redteam.engine.report.for_report`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

from martian._privacy import RedactionPolicy
from martian.redteam.engine.local import LocalService
from martian.redteam.engine.report import for_report
from martian.redteam.engine.run import Engine
from martian.redteam.engine.service import select_service
from martian.redteam.model import RedteamRun
from martian.redteam.runfile import RunFile
from martian.redteam.runfile import Target as TargetBlock
from martian.redteam.targets import Target
from martian_redteam.types import RedteamService


class TargetUnavailable(ValueError):
    """The run file's target is of a kind this release cannot call."""


def build_target(block: TargetBlock) -> Target:
    """The target a run file's ``target`` block names."""
    if block.kind != "function":
        raise TargetUnavailable(
            f"This release runs a function target (file and function); an {block.kind} target is not in it yet."
        )
    from martian.redteam.targets.callback import function_target

    assert block.file is not None
    return function_target(block.file, block.function or "target")


def unsupported(run_file: RunFile) -> list[str]:
    """What the run file asks for that this release cannot do. A run stops on any of it, rather than
    run without it."""
    found = []
    if run_file.tools.proxy:
        found.append("tools.proxy: this release has no tool proxy")
    return found


def redaction_for(run_file: RunFile, environ: Mapping[str, str] | None = None) -> RedactionPolicy:
    """The values a report must never show: the models' header values, as sent."""
    environ = os.environ if environ is None else environ
    values: list[str] = []
    for model in (run_file.models.attacker, run_file.models.judge):
        if model is None:
            continue
        for value in model.headers.values():
            values.append(value)
            if value.startswith("${") and value.endswith("}"):
                values.append(environ.get(value[2:-1], ""))
    return RedactionPolicy.from_secrets(*[value for value in values if len(value) >= 4])


def build_service(
    run_file: RunFile, stack: AsyncExitStack, environ: Mapping[str, str] | None = None
) -> tuple[RedteamService, dict[str, Any]]:
    """The service for this run file, and what the run records about it. A hosted client is closed
    with ``stack``."""
    kind = select_service(run_file, environ)
    policy = redaction_for(run_file, environ)
    if kind == "local":
        assert run_file.models.attacker is not None
        local = LocalService(attacker=run_file.models.attacker, judge=run_file.models.judge, redaction_policy=policy)
        return local, local.describe()
    from martian.config import MartianConfig
    from martian.redteam.client import Client

    client = Client(config=MartianConfig.from_env())
    stack.push_async_callback(client.aclose)
    return client, {"service": "hosted"}


async def execute(
    run_file: RunFile,
    *,
    service: RedteamService,
    target: Target,
    provenance: dict[str, Any] | None = None,
    store: Any = None,
    run_id: str | None = None,
    project_id: str = "default",
    principal: Any = None,
    include_content: bool = False,
) -> RedteamRun:
    """Run the engine. With ``store`` (a :class:`~martian.redteam.store.RedteamStore`), the run is
    kept after each attempt and at the end, as the reports hold it."""

    async def keep(run: RedteamRun) -> None:
        copy = for_report(run, include_content=include_content, policy=service.redaction_policy)
        if principal is None:
            await store.save(copy)
        else:
            await store.save(copy, principal=principal)

    engine = Engine(
        run_file,
        service=service,
        target=target,
        run_id=run_id,
        project_id=project_id,
        checkpoint=keep if store is not None else None,
        provenance=provenance,
    )
    return await engine.run()


__all__ = ["TargetUnavailable", "build_service", "build_target", "execute", "redaction_for", "unsupported"]
