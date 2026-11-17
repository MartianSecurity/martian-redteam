"""Run a version 2 run file: build its target and its service, run the engine, keep the run.

:func:`build_target` makes the run file's target: a function in a file
(:func:`~martian.redteam.targets.callback.function_target`), or an HTTP or MCP target
(:func:`~martian.redteam.targets.target_from_run_file`). :func:`tool_boundary` puts it behind the
recording proxy when the run file lists ``tools.proxy``, and hands the agent the proxy's session.
:func:`build_service` picks
the service (:func:`~martian.redteam.engine.service.select_service`). :func:`execute` runs the engine
and, given a store, keeps the run there after every attempt, as the reports hold it
(:func:`~martian.redteam.engine.report.for_report`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import AsyncExitStack
from pathlib import Path
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


class SessionHandoverError(ValueError):
    """The agent's session API did not take the tool session. The message names the setting."""


SESSION_FILE = "tool-session.json"
"""Where a run with ``tools.proxy`` writes the session that binds the agent's tools, in its output
folder, readable by its user only, for as long as the run lasts."""


def build_target(block: TargetBlock, *, base: str | Path | None = None) -> Target:
    """The target a run file's ``target`` block names. ``base`` is where an MCP target's stdio server
    starts: the run file's folder."""
    if block.kind == "function":
        from martian.redteam.targets.callback import function_target

        assert block.file is not None
        return function_target(block.file, block.function or "target")
    from martian.redteam.targets.factory import target_from_run_file

    return target_from_run_file(block, base=base if base is not None else ".")


async def tool_boundary(
    run_file: RunFile, target: Target, stack: AsyncExitStack, *, folder: Path, run_id: str | None = None
) -> Target:
    """``target`` behind the run's recording proxy, or ``target`` itself when the run file lists no
    ``tools.proxy``. The proxy starts here and stops with ``stack``, which ends its session. The
    session is written to ``folder``/:data:`SESSION_FILE`, mode ``0600``, removed when the run ends,
    and posted to ``tools.session_api``'s ``/sessions`` when the run file names one, as the cloud
    hands a session to the agent's session API. The reply of each attempt then carries the calls the
    proxy saw, attributed by the conversation id the agent passes as ``bind_tools``' action id."""
    if not run_file.tools.proxy:
        return target
    from martian.redteam.proxy import ProxiedTarget, ToolProxy, session_payload

    proxy = ToolProxy(run_file.tools.proxy)
    await proxy.start()
    stack.push_async_callback(proxy.stop)
    session = await proxy.issue_session(target_id="local", campaign_id=run_id)
    payload = session_payload(session)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SESSION_FILE
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    stack.callback(path.unlink, missing_ok=True)
    if run_file.tools.session_api is not None:
        await hand_over(run_file.tools.session_api, payload)
    return ProxiedTarget(target, proxy)


async def hand_over(session_api: str, payload: dict[str, Any]) -> None:
    """Post the session to the agent's session API, as the cloud does: ``POST {session_api}/sessions``
    with the session, answered with its ``session_id``."""
    import httpx

    url = session_api.rstrip("/") + "/sessions"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as error:
        raise SessionHandoverError(
            f"the agent's session API could not be reached: {type(error).__name__}; check tools.session_api"
        ) from None
    if not response.is_success:
        raise SessionHandoverError(
            f"the agent's session API answered HTTP {response.status_code}; check tools.session_api"
        )
    try:
        echoed = response.json().get("session_id")
    except (ValueError, AttributeError):
        echoed = None
    if echoed != payload["session_id"]:
        raise SessionHandoverError(
            "the agent's session API did not answer with the session's id; check tools.session_api"
        )


def unsupported(run_file: RunFile) -> list[str]:
    """What the run file asks for that this release cannot do. A run stops on any of it, rather than
    run without it. Every target kind and the tool proxy run, so nothing is listed today."""
    return []


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
    resume: RedteamRun | None = None,
) -> RedteamRun:
    """Run the engine. With ``store`` (a :class:`~martian.redteam.store.RedteamStore`), the run is
    kept after each attempt and at the end, as the reports hold it. With ``resume``, an earlier
    session of the run, its finished attempts whose fingerprint is unchanged are kept, not run."""

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
        resume=resume,
    )
    return await engine.run()


__all__ = [
    "SESSION_FILE",
    "SessionHandoverError",
    "TargetUnavailable",
    "build_service",
    "build_target",
    "execute",
    "hand_over",
    "redaction_for",
    "tool_boundary",
    "unsupported",
]
