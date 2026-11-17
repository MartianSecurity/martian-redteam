"""A red team run in a Martian environment: the SDK's side, which is a client of a Martian server.

This package holds no environment. A run file names one (``environment``), and a Martian server
runs it: it admits the run for an approved agent, pins the environment's sandbox instances and issues
the session that binds the agent's tools to its run-scoped gateway. :class:`EnvironmentClient` asks
for that and nothing else; the engine still runs here, against the user's agent.

- :meth:`EnvironmentClient.admit`: ``POST /v1/redteam/environment-runs`` with the connection's
  token (``MARTIAN_API_TOKEN``). The answer is an :class:`EnvironmentRun`: the server's run, the
  session (:class:`martian_redteam.AssessmentSession`, the shape :func:`martian_redteam.bind_tools`
  takes) and a run key, a token for this run alone.
- :class:`ServerWorld`: the run's :class:`~martian.redteam.world.World`, the server's world
  operations (``POST /v1/redteam/environment-runs/{id}/world`` with the run key): prepare, observe
  and outcome of each attempt, in a world the server resets.
- :meth:`EnvironmentClient.finish`: ``POST /v1/redteam/environment-runs/{id}/finish`` with the run
  key. The server releases the session and the instances.

A server's refusal is raised as :class:`EnvironmentRunError` with the server's own reason, which
names what to change (an agent that is not approved, an environment it does not know, a sandbox
already in use). ``docs/redteam-run-file.md`` describes a run in an environment.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from martian._privacy import safe_text
from martian.config import MartianConfig
from martian.redteam.runfile import RunFile
from martian_redteam.integration import AssessmentSession

ADMIT = "/v1/redteam/environment-runs"
DEFAULT_MAX_SECONDS = 3600
"""How long the server keeps a run when the run file's budget sets no ``max_seconds``."""


class EnvironmentRunError(RuntimeError):
    """The server refused or could not be reached. The message says why, never with a credential."""


@dataclass(frozen=True)
class EnvironmentRun:
    """A run the server admitted: its id and code, the environment, the session for the agent's
    tools and the run key."""

    id: str
    run_code: str
    environment: str
    expires_at: datetime
    session: dict[str, Any] = field(repr=False)
    run_key: str = field(repr=False)
    state_checks: tuple[str, ...] = ()
    implant_profiles: tuple[str, ...] = ()

    def assessment_session(self) -> AssessmentSession:
        """The session as :func:`martian_redteam.bind_tools` takes it."""
        return AssessmentSession.model_validate(self.session)


def admission(run_file: RunFile, *, request_id: str, label: str | None = None) -> dict[str, Any]:
    """What the server is asked for: the environment and the agent of the run file, and the state
    checks and implant profiles its probes use, so the server refuses what it cannot do before
    anything runs."""
    assert run_file.environment is not None
    checks = sorted({check.name for item in run_file.vulnerabilities for check in item.checks if check.type == "state"})
    body: dict[str, Any] = {
        "request_id": request_id,
        "environment": run_file.environment,
        "state_checks": checks,
        "implant_profiles": [],
        "max_seconds": int(run_file.budget.max_seconds or DEFAULT_MAX_SECONDS),
    }
    if run_file.agent is not None:
        body["agent_id"] = run_file.agent
    if label is not None:
        body["label"] = label
    return body


def _reason(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail.strip():
        return safe_text(detail.strip())[:300]
    return f"HTTP {response.status_code}"


class EnvironmentClient:
    """The Martian server's environment run routes, on the SDK's connection."""

    def __init__(self, config: MartianConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._http = httpx.AsyncClient(
            base_url=config.api_url,
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=False,
            trust_env=config.trust_env,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: dict[str, Any], *, bearer: str, what: str) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.config.request_timeout_seconds):
                response = await self._http.post(path, json=body, headers={"Authorization": f"Bearer {bearer}"})
        except (httpx.HTTPError, TimeoutError) as error:
            raise EnvironmentRunError(
                f"{what}: the Martian server could not be reached ({type(error).__name__})"
            ) from None
        if not response.is_success:
            raise EnvironmentRunError(f"{what}: {_reason(response)}")
        try:
            found = response.json()
        except ValueError:
            raise EnvironmentRunError(f"{what}: the Martian server's answer is not JSON") from None
        if not isinstance(found, dict):
            raise EnvironmentRunError(f"{what}: the Martian server's answer is not an object")
        return found

    async def admit(
        self, run_file: RunFile, *, request_id: str | None = None, label: str | None = None
    ) -> EnvironmentRun:
        """Admit the run file's run in its environment. One request: an answer that is lost is not
        repeated under another ``request_id``."""
        body = admission(run_file, request_id=request_id or str(uuid.uuid4()), label=label)
        found = await self._post(
            ADMIT, body, bearer=self.config.token.get_secret_value(), what="The environment run was not admitted"
        )
        try:
            run = EnvironmentRun(
                id=str(found["id"]),
                run_code=str(found["run_code"]),
                environment=str(found["environment"]),
                expires_at=datetime.fromisoformat(str(found["expires_at"])),
                session=dict(found["session"]),
                run_key=str(found["run_key"]),
                state_checks=tuple(found.get("state_checks") or ()),
                implant_profiles=tuple(found.get("implant_profiles") or ()),
            )
            run.assessment_session()
        except (KeyError, TypeError, ValueError):
            raise EnvironmentRunError("The Martian server's environment run is not one this SDK reads") from None
        return run

    async def finish(self, run: EnvironmentRun, state: str) -> None:
        """End the run on the server: its session and instances are released."""
        await self._post(
            f"{ADMIT}/{run.id}/finish",
            {"state": state},
            bearer=run.run_key,
            what="The environment run was not finished",
        )


class ServerWorld:
    """The world of a run the server admitted: each operation is one request with the run key. A
    failed one raises :class:`~martian.redteam.world.WorldUnavailable` with the server's reason."""

    def __init__(self, client: EnvironmentClient, run: EnvironmentRun) -> None:
        self.client = client
        self.run = run

    @property
    def name(self) -> str:
        return self.run.environment

    async def _call(self, operation: str, attempt: str, action_id: str, **extra: Any) -> dict[str, Any]:
        from martian.redteam.world import WorldUnavailable

        body = {"operation": operation, "attempt": attempt, "action_id": action_id, **extra}
        try:
            return await self.client._post(
                f"{ADMIT}/{self.run.id}/world", body, bearer=self.run.run_key, what=f"The world could not {operation}"
            )
        except EnvironmentRunError as error:
            raise WorldUnavailable(str(error)) from None

    async def prepare(self, attempt: str, action_id: str, *, implant: Any = None) -> dict[str, Any]:
        found = await self._call("prepare", attempt, action_id)
        world = found.get("world")
        return dict(world) if isinstance(world, dict) else {}

    async def observe(self, attempt: str, action_id: str) -> Any:
        return self._read(await self._call("observe", attempt, action_id), attempt)

    async def outcome(self, attempt: str, action_id: str) -> Any:
        return self._read(await self._call("outcome", attempt, action_id), attempt)

    def _read(self, found: dict[str, Any], attempt: str) -> Any:
        """The server's answer as a :class:`~martian.redteam.world.WorldOutcome`: its state checks,
        and each call the gateway recorded as an executed tool call with its evidence."""
        import hashlib
        import json as jsonlib

        from martian.redteam.model import Evidence, ToolCall
        from martian.redteam.world import StateResult, WorldOutcome, WorldUnavailable

        try:
            checks = {
                str(name): StateResult(
                    violated=item.get("violated"),
                    description=str(item.get("description") or ""),
                    conditions=list(item.get("conditions") or []),
                )
                for name, item in dict(found.get("checks") or {}).items()
            }
            calls, evidence = [], []
            for item in found.get("tool_calls") or []:
                call_id = f"gateway-{item['call_id']}"
                calls.append(
                    ToolCall(
                        name=str(item["tool"]),
                        arguments=dict(item.get("arguments") or {}),
                        call_id=call_id,
                        executed=item.get("executed"),
                    )
                )
                content = jsonlib.dumps(item, sort_keys=True, default=str)
                evidence.append(
                    Evidence(
                        id=f"{attempt}/{call_id}",
                        kind="tool_call",
                        tier="tool",
                        source=f"gateway:{item.get('system', '')}",
                        sha256=hashlib.sha256(content.encode()).hexdigest(),
                        content=content,
                        metadata={
                            "tool": item["tool"],
                            "call_id": call_id,
                            "executed": item.get("executed"),
                            "outcome": item.get("outcome"),
                            "world_epoch": item.get("world_epoch"),
                        },
                    )
                )
        except (KeyError, TypeError, ValueError):
            raise WorldUnavailable("The Martian server's world answer is not one this SDK reads") from None
        return WorldOutcome(checks=checks, tool_calls=calls, evidence=evidence, receipt=found)


FINISHED = {"completed": "completed", "stopped": "stopped", "failed": "failed", "cancelled": "cancelled"}
"""A red team run's state as the server's finish takes it; a run that ended otherwise is ``failed``."""


__all__ = [
    "ADMIT",
    "DEFAULT_MAX_SECONDS",
    "FINISHED",
    "EnvironmentClient",
    "EnvironmentRun",
    "EnvironmentRunError",
    "ServerWorld",
    "admission",
]
