"""Push a red team run to a Martian server, where the console lists it with the runs the server ran.

:func:`push_run` sends one finished run of the shared model (:class:`~martian.redteam.model.RedteamRun`)
to ``PUT /v1/push/redteam/runs/{run_id}`` with the connection's token (``MARTIAN_API_TOKEN``, an
API key with ``redteam:run``). The server labels it as executed by the client and keeps it once:
the same run pushed again changes nothing, and the answer says whether this push created it.
``martian redteam push`` calls it, and so does a run in an environment when it ends.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from martian._privacy import safe_text
from martian.config import MartianConfig
from martian.redteam.model import RedteamRun

PUSH = "/v1/push/redteam/runs"


class PushError(RuntimeError):
    """The server refused the run or could not be reached. The message never holds a credential."""


@dataclass(frozen=True)
class Pushed:
    run_id: str
    project_id: str
    created: bool


async def push_run(
    run: RedteamRun, config: MartianConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> Pushed:
    """Push ``run`` once. A run that has not finished is refused by the server."""
    async with httpx.AsyncClient(
        base_url=config.api_url,
        timeout=httpx.Timeout(config.request_timeout_seconds),
        follow_redirects=False,
        trust_env=config.trust_env,
        transport=transport,
    ) as client:
        try:
            async with asyncio.timeout(config.request_timeout_seconds):
                response = await client.put(
                    f"{PUSH}/{run.id}",
                    json={"run": run.without_local_launch().model_dump(mode="json")},
                    headers={"Authorization": f"Bearer {config.token.get_secret_value()}"},
                )
        except (httpx.HTTPError, TimeoutError) as error:
            raise PushError(f"the Martian server could not be reached ({type(error).__name__})") from None
    if not response.is_success:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        reason = (
            safe_text(detail, policy=config.redaction_policy)[:300]
            if isinstance(detail, str) and detail
            else f"HTTP {response.status_code}"
        )
        raise PushError(f"the Martian server did not take the run: {reason}")
    try:
        found = response.json()
        return Pushed(run_id=str(found["run_id"]), project_id=str(found["project_id"]), created=bool(found["created"]))
    except (ValueError, KeyError, TypeError):
        raise PushError("the Martian server's answer is not one this SDK reads") from None


__all__ = ["PUSH", "PushError", "Pushed", "push_run"]
