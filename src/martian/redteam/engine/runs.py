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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from martian._privacy import RedactionPolicy
from martian.redteam.engine.local import LocalService
from martian.redteam.engine.report import for_report
from martian.redteam.engine.run import Engine
from martian.redteam.engine.service import ServiceSelectionError, select_service
from martian.redteam.model import RedteamRun
from martian.redteam.runfile import RunFile
from martian.redteam.runfile import Target as TargetBlock
from martian.redteam.targets import Target
from martian.redteam.verify.canary import new_canary
from martian_redteam.types import RedteamService


class TargetUnavailable(ValueError):
    """The run file's target is of a kind this release cannot call."""


class SessionHandoverError(ValueError):
    """The agent's session API did not take the tool session. The message names the setting."""


SESSION_FILE = "tool-session.json"
"""Where a run with ``tools.proxy`` writes the session that binds the agent's tools, in its output
folder, readable by its user only, for as long as the run lasts."""


def build_target(
    block: TargetBlock, *, base: str | Path | None = None, environ: Mapping[str, str] | None = None
) -> Target:
    """The target a run file's ``target`` block names. ``base`` is where an MCP target's stdio server
    starts: the run file's folder. ``environ`` stands in for the environment when ``${NAME}`` in an
    HTTP or MCP target is read; by default it is the process's."""
    if block.kind == "function":
        from martian.redteam.targets.callback import function_target

        assert block.file is not None
        return function_target(block.file, block.function or "target")
    from martian.redteam.targets.factory import target_from_run_file

    return target_from_run_file(block, base=base if base is not None else ".", environ=environ)


async def tool_boundary(
    run_file: RunFile, target: Target, stack: AsyncExitStack, *, folder: Path, run_id: str | None = None
) -> Target:
    """``target`` behind the run's recording proxy, or ``target`` itself when the run file lists no
    ``tools.proxy``. The proxy starts here and stops with ``stack``, which ends its session. The
    session is written to ``folder``/:data:`SESSION_FILE`, mode ``0600``, removed when the run ends,
    and posted to ``tools.session_api``'s ``/sessions`` when the run file names one, as the cloud
    hands a session to the agent's session API. The reply of each attempt then carries the calls the
    proxy saw, attributed by the conversation id the agent passes as ``bind_tools``' action id. With
    ``tools.decoy``, the run also serves the inert test MCP server
    (:class:`~martian.redteam.poisoning.DecoyServer`) behind the proxy, and the returned target's
    ``decoy`` is it."""
    if not run_file.tools.proxy and run_file.tools.decoy is None:
        return target
    from martian.redteam.proxy import ProxiedTarget, ToolProxy, session_payload
    from martian.redteam.runfile import ProxiedServer

    servers = list(run_file.tools.proxy)
    decoy = None
    if run_file.tools.decoy is not None:
        from martian.redteam.poisoning import DecoyServer

        decoy = DecoyServer()
        upstream = await decoy.start()
        stack.push_async_callback(decoy.stop)
        servers.append(ProxiedServer(name=run_file.tools.decoy.name, upstream=upstream))
    proxy = ToolProxy(servers)
    await proxy.start()
    stack.push_async_callback(proxy.stop)
    session = await proxy.issue_session(target_id="local", campaign_id=run_id)
    await hand_session(session_payload(session), stack, folder=folder, session_api=run_file.tools.session_api)
    proxied = ProxiedTarget(target, proxy)
    proxied.decoy = decoy
    return proxied


async def hand_session(
    payload: dict[str, Any], stack: AsyncExitStack, *, folder: Path, session_api: str | None
) -> None:
    """Give the agent its tool session: write it to ``folder``/:data:`SESSION_FILE`, mode ``0600``,
    removed when ``stack`` closes, and post it to ``session_api``'s ``/sessions`` when one is named."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SESSION_FILE
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    stack.callback(path.unlink, missing_ok=True)
    if session_api is not None:
        await hand_over(session_api, payload)


@dataclass
class Admitted:
    """A run the Martian server admitted in an environment, and the state it is finished with when
    the run's stack closes: ``failed`` until the run sets it (:meth:`ended`)."""

    run: Any
    client: Any
    state: str = "failed"
    finish_error: str | None = None
    """Why the server did not take the end of the run, when it did not: the run's records stand, and
    the server ends it at its ``max_seconds``."""

    def world(self) -> Any:
        """The run's world: the server's world operations, with the run key."""
        from martian.redteam.environment import ServerWorld

        return ServerWorld(self.client, self.run)

    def ended(self, state: str) -> None:
        from martian.redteam.environment import FINISHED

        self.state = FINISHED.get(state, "failed")


async def environment_boundary(
    run_file: RunFile, stack: AsyncExitStack, *, folder: Path, label: str, client: Any = None
) -> Admitted:
    """Admit the run file's run in its environment on the Martian server, and give the agent the
    session the server issued: its tools then reach the environment's sandboxes through the
    server's run-scoped gateway, as :func:`tool_boundary` gives it the proxy's. The run is finished
    on the server when ``stack`` closes, with the state :meth:`Admitted.ended` set."""
    from martian.config import MartianConfig
    from martian.redteam.environment import EnvironmentClient

    if client is None:
        client = EnvironmentClient(MartianConfig.from_env())
        stack.push_async_callback(client.aclose)
    admitted = Admitted(await client.admit(run_file, label=label), client)

    async def finish() -> None:
        from martian.redteam.environment import EnvironmentRunError

        try:
            await client.finish(admitted.run, admitted.state)
        except EnvironmentRunError as error:
            admitted.finish_error = str(error)

    stack.push_async_callback(finish)
    await hand_session(admitted.run.session, stack, folder=folder, session_api=run_file.tools.session_api)
    return admitted


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


def run_canary(earlier: RedteamRun | None = None) -> str:
    """The canary a run plants: the earlier session's when it is resumed, else a new one. The target
    is built from :func:`~martian.redteam.verify.canary.planted_run_file` with it, and the engine is
    given it."""
    kept = earlier.provenance.get("canary") if earlier is not None else None
    return kept if isinstance(kept, str) and kept else new_canary()


def unsupported(run_file: RunFile, *, environments: bool = False) -> list[str]:
    """What the run file asks for that this caller cannot do. A run stops on any of it, rather than
    run without it. Every target kind and the tool proxy run. A run in an environment starts only
    from ``martian redteam run`` (``environments``), connected to a Martian server."""
    if run_file.environment is not None and not environments:
        return ["environment: a run in a Martian environment starts from martian redteam run, not here"]
    return []


def redaction_for(run_file: RunFile, environ: Mapping[str, str] | None = None) -> RedactionPolicy:
    """The values a report must never show: the models' header values, as sent."""
    environ = os.environ if environ is None else environ
    values: list[str] = []
    for model in (*run_file.models.pool, run_file.models.judge):
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
        if (
            run_file.budget.cost_usd is not None
            and (run_file.models.attacker is not None or run_file.models.judge is not None)
            and not os.environ.get("MLFLOW_MODEL_CATALOG_URI", "").strip()
        ):
            raise ServiceSelectionError("A local cost_usd budget requires MLFLOW_MODEL_CATALOG_URI for model pricing.")
        if len(run_file.models.pool) > 1:
            from martian.redteam.engine.local import PooledService

            pooled = PooledService(
                [
                    LocalService(
                        attacker=attacker,
                        judge=run_file.models.judge,
                        redaction_policy=policy,
                        spend_capped=run_file.budget.limits_model_spend,
                    )
                    for attacker in run_file.models.pool
                ]
            )
            return pooled, pooled.describe()
        local = LocalService(
            attacker=run_file.models.attacker,
            judge=run_file.models.judge,
            redaction_policy=policy,
            spend_capped=run_file.budget.limits_model_spend,
        )
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
    canary: str | None = None,
    world: Any = None,
) -> RedteamRun:
    """Run the engine. With ``store`` (a :class:`~martian.redteam.store.RedteamStore`), the run is
    kept after each attempt and at the end, as the reports hold it. With ``resume``, an earlier
    session of the run, its finished attempts whose fingerprint is unchanged are kept, not run. With
    ``world`` (a run in a Martian environment), each attempt runs in a world reset for it."""

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
        canary=canary,
        world=world,
    )
    return await engine.run()


__all__ = [
    "SESSION_FILE",
    "Admitted",
    "SessionHandoverError",
    "TargetUnavailable",
    "build_service",
    "build_target",
    "environment_boundary",
    "execute",
    "hand_over",
    "hand_session",
    "redaction_for",
    "run_canary",
    "tool_boundary",
    "unsupported",
]
