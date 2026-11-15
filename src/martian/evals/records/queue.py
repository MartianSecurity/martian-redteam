"""The worker's queue, in the record store's database: run launches and scoring jobs that no worker
has finished.

``SqlWorkQueue`` keeps one row per piece of work in ``martian_work_queue`` (migration ``0003``),
on SQLite or Postgres, beside the records it names. A worker takes the oldest row that is
``queued``, or ``claimed`` by a lease that ran out, with :meth:`SqlWorkQueue.claim`. A claim holds
the row for ``lease_seconds``; the worker renews it while the work runs (:meth:`renew`) and
removes the row when the work has ended (:meth:`done`). A claim is a new random token each time,
and every change to a claimed row names it, so a worker whose lease ran out and was taken by
another changes nothing any more.

Two workers never hold one row at once: the claim moves a row only while it is still claimable,
in one statement, and on Postgres it also locks the row it read and skips rows another claim has
locked (``FOR UPDATE SKIP LOCKED``). A row whose worker died is claimable again once its lease
runs out, and each claim counts an attempt. Lease times are this process's clock in UTC, so the
hosts of the workers that share one queue need clocks within a small part of the lease of each
other.

A process that does work itself, outside any worker, can hold it in the queue the same way
(:meth:`SqlWorkQueue.hold`), so that its end is noticed if it stops: a ``recording`` claim, which
no worker runs, and which the worker that claims it after its lease ran out finishes as
interrupted.

The queue keeps the work as JSON and knows nothing of what it means; the worker writes and reads
it (:mod:`martian.evals.worker.queue`). Each row belongs to the organisation's space, as every
record does. On Postgres row-level security filters on it again; a claim, which takes the next
row of any organisation, sets ``martian.claim`` for its own transaction, and nothing else does.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from sqlalchemy import Connection, and_, delete, func, or_, select, text, update

from martian.evals._sql import is_postgres
from martian.evals.records.events import space_of
from martian.evals.records.sql import engine_for, upgrade
from martian.evals.records.tables import work_queue

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

T = TypeVar("T")

QUEUED = "queued"
CLAIMED = "claimed"
RECORDING = "recording"
"""The kind of a run that a process records itself and holds in the queue (:meth:`SqlWorkQueue.hold`),
beside the kinds of work a worker runs, ``run`` and ``scoring``."""


@dataclass(frozen=True)
class Claim:
    """A worker's hold on one queued piece of work, until its lease runs out. ``attempt`` counts the
    claims of this work so far, this one included, less those given back with :meth:`release`."""

    space: str
    project_id: str
    id: str
    kind: str
    work: dict[str, Any]
    token: str
    attempt: int


def _now() -> datetime:
    return datetime.now(UTC)


class SqlWorkQueue:
    """The queue in the database at ``database_url``: the record store's, with its tables in
    ``schema`` on Postgres. It migrates the database when it starts, unless ``migrate=False``."""

    def __init__(self, database_url: str, *, schema: str = "martian", migrate: bool = True) -> None:
        self._postgres = is_postgres(database_url)
        self._engine = engine_for(database_url, schema=schema)
        if migrate:
            upgrade(self._engine)

    def close(self) -> None:
        self._engine.dispose()

    async def _run(self, space: str | None, work: Callable[[Connection], T]) -> T:
        """``work`` in one transaction, for ``space``, or for every space when it is None (a claim)."""

        def transaction() -> T:
            with self._engine.begin() as connection:
                if self._postgres:
                    if space is None:
                        connection.execute(text("SELECT set_config('martian.claim', 'on', true)"))
                    else:
                        connection.execute(text("SELECT set_config('martian.space', :space, true)"), {"space": space})
                return work(connection)

        return await asyncio.to_thread(transaction)

    @staticmethod
    def _key(claim: Claim) -> Any:
        return and_(
            work_queue.c.space == claim.space,
            work_queue.c.project_id == claim.project_id,
            work_queue.c.id == claim.id,
            work_queue.c.token == claim.token,
        )

    async def put(self, principal: Principal, project_id: str, entry_id: str, kind: str, work: dict[str, Any]) -> None:
        """Queue ``work`` as ``entry_id`` of the principal's organisation. Putting the same id again
        while it is in the queue leaves the queue as it is."""
        from sqlalchemy.dialects import postgresql, sqlite

        space = space_of(principal)
        dialect_insert: Any = postgresql.insert if self._postgres else sqlite.insert
        statement = dialect_insert(work_queue).values(
            space=space,
            project_id=project_id,
            id=entry_id,
            kind=kind,
            state=QUEUED,
            attempts=0,
            created_at=_now(),
            work=work,
        )
        await self._run(space, lambda connection: connection.execute(statement.on_conflict_do_nothing()))

    async def hold(
        self,
        principal: Principal,
        project_id: str,
        entry_id: str,
        kind: str,
        work: dict[str, Any],
        lease_seconds: float,
    ) -> Claim:
        """Put ``work`` in the queue already claimed by the caller for ``lease_seconds``: a process
        that does the work itself, outside any worker, and holds it in the queue so that its end is
        noticed if it stops (``martian evals run`` holding the run it records). Renew and remove it
        as a worker's claim. Raises ``IntegrityError`` when ``entry_id`` is in the queue already."""
        space, token, now = space_of(principal), uuid.uuid4().hex, _now()
        statement = work_queue.insert().values(
            space=space,
            project_id=project_id,
            id=entry_id,
            kind=kind,
            state=CLAIMED,
            attempts=1,
            token=token,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            created_at=now,
            work=work,
        )
        await self._run(space, lambda connection: connection.execute(statement))
        return Claim(space, project_id, entry_id, kind, dict(work), token, 1)

    async def claim(self, lease_seconds: float) -> Claim | None:
        """Take the oldest piece of work that is queued, or whose claim's lease ran out, and hold it
        for ``lease_seconds``; None when there is none, or when another worker took it first."""

        def claim(connection: Connection) -> Claim | None:
            now = _now()
            claimable = or_(
                work_queue.c.state == QUEUED,
                and_(work_queue.c.state == CLAIMED, work_queue.c.lease_expires_at < now),
            )
            query = (
                select(
                    work_queue.c.space,
                    work_queue.c.project_id,
                    work_queue.c.id,
                    work_queue.c.kind,
                    work_queue.c.attempts,
                    work_queue.c.work,
                )
                .where(claimable)
                .order_by(work_queue.c.created_at, work_queue.c.id)
                .limit(1)
            )
            if self._postgres:
                query = query.with_for_update(skip_locked=True)
            row = connection.execute(query).first()
            if row is None:
                return None
            token = uuid.uuid4().hex
            moved = connection.execute(
                update(work_queue)
                .where(
                    work_queue.c.space == row.space,
                    work_queue.c.project_id == row.project_id,
                    work_queue.c.id == row.id,
                    claimable,
                )
                .values(
                    state=CLAIMED,
                    token=token,
                    attempts=row.attempts + 1,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
            ).rowcount
            if moved == 0:
                return None
            return Claim(row.space, row.project_id, row.id, row.kind, dict(row.work), token, row.attempts + 1)

        return await self._run(None, claim)

    async def renew(self, claim: Claim, lease_seconds: float) -> bool:
        """Hold the claimed work for another ``lease_seconds`` from now; False when the claim no
        longer holds it, because another worker took it after its lease ran out."""
        return bool(
            await self._run(
                claim.space,
                lambda connection: (
                    connection.execute(
                        update(work_queue)
                        .where(self._key(claim), work_queue.c.state == CLAIMED)
                        .values(lease_expires_at=_now() + timedelta(seconds=lease_seconds))
                    ).rowcount
                ),
            )
        )

    async def release(self, claim: Claim) -> None:
        """Give the work back unfinished, for the next worker to take at once, without counting
        this attempt: the worker is stopping, not failing. Does nothing once the claim no longer
        holds it."""
        await self._run(
            claim.space,
            lambda connection: connection.execute(
                update(work_queue)
                .where(self._key(claim))
                .values(state=QUEUED, token=None, lease_expires_at=None, attempts=work_queue.c.attempts - 1)
            ),
        )

    async def done(self, claim: Claim) -> None:
        """Remove the work, which has ended. Does nothing once the claim no longer holds it."""
        await self._run(claim.space, lambda connection: connection.execute(delete(work_queue).where(self._key(claim))))

    async def held(self, principal: Principal, project_id: str, entry_id: str) -> bool:
        """Whether a claim whose lease has not run out holds ``entry_id``: whether a worker is on it."""
        space = space_of(principal)

        def held(connection: Connection) -> bool:
            found = connection.execute(
                select(work_queue.c.id).where(
                    work_queue.c.space == space,
                    work_queue.c.project_id == project_id,
                    work_queue.c.id == entry_id,
                    work_queue.c.state == CLAIMED,
                    work_queue.c.lease_expires_at >= _now(),
                )
            ).first()
            return found is not None

        return await self._run(space, held)

    async def contains(self, principal: Principal, project_id: str, entry_id: str) -> bool:
        """Whether ``entry_id`` is in the queue: queued, or claimed, whether or not its lease ran out."""
        space = space_of(principal)

        def contains(connection: Connection) -> bool:
            found = connection.execute(
                select(work_queue.c.id).where(
                    work_queue.c.space == space, work_queue.c.project_id == project_id, work_queue.c.id == entry_id
                )
            ).first()
            return found is not None

        return await self._run(space, contains)

    async def pending(self) -> int:
        """How many pieces of work, of every organisation, are in the queue: queued or claimed."""
        return int(
            await self._run(
                None, lambda connection: connection.execute(select(func.count()).select_from(work_queue)).scalar_one()
            )
        )


__all__ = ["CLAIMED", "QUEUED", "RECORDING", "Claim", "SqlWorkQueue"]
