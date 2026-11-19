"""Bind a queue worker's database transactions to its current claim."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, select

from martian.evals.records.tables import work_queue
from martian.evals.store import StoreError


@dataclass(frozen=True)
class WorkLease:
    space: str
    project_id: str
    entry_id: str
    token: str


current_work_lease: ContextVar[WorkLease | None] = ContextVar("current_work_lease", default=None)


class WorkLeaseLost(StoreError):
    def __init__(self) -> None:
        super().__init__("conflict", "The worker no longer holds the current work lease")


def lock_current_lease(connection: Connection, lease: WorkLease, now: Callable[[], datetime]) -> bool:
    """Keep the claim stable through the caller's transaction and check expiry after taking the lock."""
    query = select(work_queue.c.lease_expires_at).where(
        work_queue.c.space == lease.space,
        work_queue.c.project_id == lease.project_id,
        work_queue.c.id == lease.entry_id,
        work_queue.c.token == lease.token,
        work_queue.c.state == "claimed",
    )
    if connection.dialect.name == "postgresql":
        query = query.with_for_update(of=work_queue)
    else:
        # A SQLite SELECT alone takes no write lock. Serialize the check and any
        # writes with a competing claim before reading the current owner.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    expires_at = connection.execute(query).scalar_one_or_none()
    if not isinstance(expires_at, datetime):
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > now()
