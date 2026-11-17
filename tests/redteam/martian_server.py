"""A fake Martian server's environment run routes, for the SDK's side of a run in an environment.

It answers like the hosted backend's ``/v1/redteam/environment-runs`` routes: admission returns a
session whose MCP connection is ``gateway`` (a loopback MCP server standing in for the run-scoped
gateway) and a run key, and ``finish`` takes the run key. Nothing here is an environment: it keeps
what the SDK sent, and answers what a test sets."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

TOKEN = "mce_environment_test_key"
RUN_KEY = "run-key-for-this-run-only"


class FakeMartianServer:
    def __init__(self, gateway: str, *, tools: list[str] | None = None) -> None:
        self.gateway = gateway
        self.tools = tools or ["lookup_order", "issue_refund"]
        self.admissions: list[dict[str, Any]] = []
        self.finished: list[str] = []
        self.refusal: tuple[int, str] | None = None
        self.run_id = str(uuid.uuid4())

    def __call__(self, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
        path = headers[":path"]
        if path == "/v1/redteam/environment-runs":
            if headers.get("authorization") != f"Bearer {TOKEN}":
                return 401, {"detail": "API key is not active"}
            self.admissions.append(body)
            if self.refusal is not None:
                return self.refusal[0], {"detail": self.refusal[1]}
            return 201, self.run(body)
        if path == f"/v1/redteam/environment-runs/{self.run_id}/finish":
            if headers.get("authorization") != f"Bearer {RUN_KEY}":
                return 401, {"detail": "Invalid environment run key"}
            self.finished.append(body["state"])
            return 200, {**self.run(self.admissions[-1]), "state": body["state"], "session": None, "run_key": None}
        return 404, {"detail": "Not Found"}

    def run(self, body: dict[str, Any]) -> dict[str, Any]:
        expires = (datetime.now(UTC) + timedelta(seconds=body.get("max_seconds", 3600))).isoformat()
        return {
            "id": self.run_id,
            "run_code": "MR-1001",
            "agent_id": body.get("agent_id") or str(uuid.uuid4()),
            "environment_id": str(uuid.uuid4()),
            "environment": body["environment"],
            "state": "running",
            "expires_at": expires,
            "state_checks": body.get("state_checks", []),
            "implant_profiles": body.get("implant_profiles", []),
            "session": {
                "session_id": f"env-{self.run_id}",
                "campaign_id": self.run_id,
                "target_id": "agent",
                "expires_at": expires,
                "mcp_connections": [
                    {
                        "name": "shop",
                        "url": self.gateway,
                        "headers": {"Authorization": "Bearer gateway-capability"},
                        "allowed_tools": self.tools,
                    }
                ],
            },
            "run_key": RUN_KEY,
        }
