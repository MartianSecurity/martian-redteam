"""A planned red team campaign on a Martian server, started and followed from the SDK.

A campaign is the console's: the server's engine plans and runs it against a registered agent the
organisation approved, at a depth (``quick``, ``standard`` or ``deep``), and the backend decides
whether the agent may be released. :class:`CampaignClient` starts one with the connection's token
(``POST /v1/redteam/campaigns``) and reads it (``GET /v1/redteam/campaigns/{id}``): its state, its
release decision and, once it finished, the run in the shared red team model. Nothing runs here.

:func:`exit_code` is what ``martian redteam campaign`` exits with: ``0`` when the release decision
is ``APPROVED`` (and ``CONDITIONAL`` with ``allow_conditional``), ``1`` when it is ``BLOCKED`` or
``CONDITIONAL``, and ``2`` when there is no decision: the campaign did not finish, failed, or the
backend has not decided (``PENDING``).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from martian._privacy import safe_text
from martian.config import MartianConfig

CAMPAIGNS = "/v1/redteam/campaigns"
DECIDED = {"APPROVED", "CONDITIONAL", "BLOCKED"}


class CampaignError(RuntimeError):
    """The server refused or could not be reached. The message never holds a credential."""


@dataclass(frozen=True)
class Campaign:
    id: str
    run_code: str
    state: str
    finished: bool
    decision: str
    reasons: tuple[str, ...]
    console_path: str
    verdict: str = ""
    redteam_run: dict[str, Any] | None = field(default=None, repr=False)

    @classmethod
    def read(cls, found: dict[str, Any]) -> Campaign:
        decision = found.get("release_decision") or {}
        return cls(
            id=str(found["id"]),
            run_code=str(found["run_code"]),
            state=str(found["state"]),
            finished=bool(found["finished"]),
            decision=str(decision.get("status") or "PENDING"),
            reasons=tuple(str(item) for item in decision.get("reason_codes") or ()),
            console_path=str(found.get("console_path") or ""),
            verdict=str(found.get("verdict") or ""),
            redteam_run=found.get("redteam_run"),
        )


def exit_code(campaign: Campaign, *, allow_conditional: bool = False) -> int:
    """The exit code the release decision gives (see the module docstring)."""
    if not campaign.finished or campaign.state not in ("completed", "stopped") or campaign.decision not in DECIDED:
        return 2
    if campaign.decision == "APPROVED" or (campaign.decision == "CONDITIONAL" and allow_conditional):
        return 0
    return 1


class CampaignClient:
    """The Martian server's campaign routes, on the SDK's connection."""

    def __init__(self, config: MartianConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._http = httpx.AsyncClient(
            base_url=config.api_url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {config.token.get_secret_value()}"},
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=False,
            trust_env=config.trust_env,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Campaign:
        try:
            async with asyncio.timeout(self.config.request_timeout_seconds):
                response = await self._http.request(method, path, json=body)
        except (httpx.HTTPError, TimeoutError) as error:
            raise CampaignError(f"the Martian server could not be reached ({type(error).__name__})") from None
        if not response.is_success:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            reason = safe_text(detail)[:300] if isinstance(detail, str) and detail else f"HTTP {response.status_code}"
            raise CampaignError(reason)
        try:
            return Campaign.read(response.json())
        except (ValueError, KeyError, TypeError):
            raise CampaignError("the Martian server's answer is not one this SDK reads") from None

    async def start(
        self,
        *,
        agent: str | None,
        depth: str,
        objective: str | None = None,
        outcomes: list[str] | None = None,
        environment: str | None = None,
        request_id: str | None = None,
    ) -> Campaign:
        """Start a campaign. The request is sent once; the same ``request_id`` answers the same one."""
        body: dict[str, Any] = {"request_id": request_id or str(uuid.uuid4()), "depth": depth}
        if agent is not None:
            body["agent_id"] = agent
        if objective is not None:
            body["objective"] = objective
        if outcomes:
            body["outcomes"] = outcomes
        if environment is not None:
            body["environment"] = environment
        return await self._call("POST", CAMPAIGNS, body)

    async def read(self, campaign_id: str) -> Campaign:
        return await self._call("GET", f"{CAMPAIGNS}/{campaign_id}")

    async def follow(self, campaign: Campaign, *, every: float, within: float) -> Campaign:
        """Read the campaign every ``every`` seconds until it finished, for at most ``within``
        seconds; then the last reading. A failed reading is not a finished campaign: it raises."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while not campaign.finished and loop.time() < deadline:
            await asyncio.sleep(min(every, max(0.0, deadline - loop.time())))
            campaign = await self.read(campaign.id)
        return campaign


__all__ = ["CAMPAIGNS", "Campaign", "CampaignClient", "CampaignError", "exit_code"]
