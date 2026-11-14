"""The store interface the evals core reads and writes through.

The core never talks to MLflow or a database directly: an edition gives it an ``EvalStore``. The
local edition will bind ``mlflow-skinny`` in-process and the server binds its MLflow server over
REST (MorningHQ/martian-notes#104 and #106). ``MemoryStore`` keeps everything in memory, for
tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

StoreState = Literal["not_configured", "pending", "provisioning", "ready", "failed", "needs_review"]


class StoreStatus(BaseModel):
    """Whether the store can take evaluation work for a principal. The same shape as the server's
    ``GET /v1/evals/status`` (``EvalsStatusOut`` in the evals API contract)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: StoreState
    ready: bool
    error_code: str | None = None
    updated_at: datetime | None = None
    provider: Literal["mlflow"] | None = None
    admission_paused: bool = False
    routing_epoch: int = 0


@runtime_checkable
class EvalStore(Protocol):
    async def status(self, principal: Principal) -> StoreStatus:
        """The store's readiness for this principal's organisation, or for the single local user."""
        ...


class MemoryStore:
    """An in-memory store. It records which principals asked, so tests can see what was injected."""

    def __init__(self, status: StoreStatus | None = None) -> None:
        self._status = status or StoreStatus(status="ready", ready=True)
        self.callers: list[Principal] = []

    async def status(self, principal: Principal) -> StoreStatus:
        self.callers.append(principal)
        return self._status


__all__ = ["EvalStore", "MemoryStore", "StoreState", "StoreStatus"]
