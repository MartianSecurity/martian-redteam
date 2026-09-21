"""Optional production tool metadata export; independent of assessment sessions."""

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr


class ToolActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID = Field(default_factory=uuid4)
    occurred_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    tool_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=100)
    outcome: Literal["success", "error"]
    duration_ms: int = Field(ge=0, le=86400000)


class VisibilityClient:
    """Explicit, no-retry export of metadata. Contains no prompt/argument/result fields.

    The backend binding pins agent, deployment, version and environment. A caller
    cannot select a different identity in an event. Reported success is not verified effect.
    """

    def __init__(self, backend_url: str, binding_id: UUID | str, token: str, *, allow_http: bool = False):
        url = urlsplit(backend_url)
        if not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("Backend must be an origin without credentials or path")
        if url.scheme != "https" and not (allow_http and url.scheme == "http"):
            raise ValueError("HTTPS is required; local HTTP requires explicit opt-in")
        self.backend_url = backend_url.rstrip("/")
        self.binding_id = UUID(str(binding_id))
        if not token.startswith("mt_") or len(token) != 46:
            raise ValueError("Expected a deployment-scoped telemetry token")
        self.token = SecretStr(token)

    async def send(self, events: list[ToolActivity]) -> int:
        """Send 1–100 events. Keep event IDs unchanged if explicitly resubmitting."""
        if not 1 <= len(events) <= 100:
            raise ValueError("Send 1–100 tool events")
        if len({e.event_id for e in events}) != len(events):
            raise ValueError("Event IDs must be unique")
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.post(
                f"{self.backend_url}/v1/discovery/telemetry-bindings/{self.binding_id}/events",
                headers={"Authorization": f"Bearer {self.token.get_secret_value()}"},
                json={"schema_version": 1, "events": [e.model_dump(mode="json") for e in events]},
            )
        if response.status_code != 200:
            raise RuntimeError(f"Visibility export rejected (HTTP {response.status_code})")
        return int(response.json()["accepted"])
