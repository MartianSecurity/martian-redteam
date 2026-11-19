"""The worker's queue, in the record store's database: run launches and scoring jobs that no worker
has finished.

``SqlWorkQueue`` keeps one row per piece of work in ``martian_work_queue`` (migration ``0003``),
on SQLite or Postgres, beside the records it names. A worker takes a row that is ``queued``, or
``claimed`` by a lease that ran out, with :meth:`SqlWorkQueue.claim`: the oldest row of the
organisation with the fewest rows running, so one organisation's many queued pieces never hold
back another's (MartianSecurity/martian-notes#145). A row is running while a claim whose lease has not
run out holds it. A claim holds the row for ``lease_seconds``; the worker renews it while the work
runs (:meth:`renew`) and removes the row when the work has ended (:meth:`done`). A claim is a new
random token each time, and every change to a claimed row names it, so a worker whose lease ran
out and was taken by another changes nothing any more.

Two workers never hold one row at once: the claim moves a row only while it is still claimable,
in one statement, and on Postgres it also locks the row it read and skips rows another claim has
locked (``FOR UPDATE SKIP LOCKED``). Two claims at the same moment each count the rows running
before either moved one, so both may go to the same organisation; the next claim counts both. A
row whose worker died is claimable again once its lease runs out, and each claim counts an
attempt. Lease times are this process's clock in UTC, so the
hosts of the workers that share one queue need clocks within a small part of the lease of each
other.

A process that does work itself, outside any worker, can hold it in the queue the same way
(:meth:`SqlWorkQueue.hold`), so that its end is noticed if it stops: a ``recording`` claim, which
no worker runs, and which the worker that claims it after its lease ran out finishes as
interrupted.

Work that comes back, such as a monitor's next pass, is given back with :meth:`reschedule`
instead of removed: queued again, taken by no claim before its ``available_at``, and then behind
the work queued before that time.

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

from sqlalchemy import Connection, Delete, Update, and_, delete, func, or_, select, text, update
from sqlalchemy.exc import DataError, IntegrityError

from martian.evals._sql import database_refusal, is_postgres, operation_name
from martian.evals.records.events import space_of
from martian.evals.records.lease import WorkLease, lock_current_lease
from martian.evals.records.sql import engine_for, upgrade
from martian.evals.records.tables import work_queue
from martian.evals.store import StoreError

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

T = TypeVar("T")

QUEUED = "queued"
CLAIMED = "claimed"
RECORDING = "recording"
"""The kind of a run that a process records itself and holds in the queue (:meth:`SqlWorkQueue.hold`),
beside the kinds of work a worker runs, ``run``, ``scoring`` and ``monitor``."""


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

    def ownership(self) -> WorkLease:
        return WorkLease(self.space, self.project_id, self.id, self.token)


class AlreadyHeld(StoreError):
    """:meth:`SqlWorkQueue.hold` found its entry in the queue already. A ``conflict`` that names
    the operation and the table, never the entry's id or the principal's organisation."""

    def __init__(self, detail: str) -> None:
        super().__init__("conflict", detail)


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
        """``work`` in one transaction, for ``space``, or for every space when it is None (a claim).
        A write the database refuses becomes a ``StoreError`` naming the method ``work`` belongs to
        and the table, never a value (``database_refusal``)."""
        operation = operation_name(work)

        def transaction() -> T:
            try:
                with self._engine.begin() as connection:
                    if self._postgres:
                        if space is None:
                            connection.execute(text("SELECT set_config('martian.claim', 'on', true)"))
                        else:
                            connection.execute(
                                text("SELECT set_config('martian.space', :space, true)"), {"space": space}
                            )
                    return work(connection)
            except (DataError, IntegrityError) as error:
                refused = database_refusal(operation, error)
            # Raised outside the except clause, so that the database's error, which holds the
            # values written, does not stay on it as its context.
            raise refused

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

        def put(connection: Connection) -> None:
            connection.execute(statement.on_conflict_do_nothing())

        await self._run(space, put)

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
        as a worker's claim. Raises :class:`AlreadyHeld`, a ``StoreError``, when ``entry_id`` is in
        the queue already; a refusal of any other value is a ``StoreError`` too."""
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

        def hold(connection: Connection) -> None:
            connection.execute(statement)

        try:
            await self._run(space, hold)
        except StoreError as error:
            if error.kind != "conflict":
                raise
            already = AlreadyHeld(error.detail)
        else:
            return Claim(space, project_id, entry_id, kind, dict(work), token, 1)
        raise already

    async def claim(self, lease_seconds: float) -> Claim | None:
        """Take a piece of work that is queued, or whose claim's lease ran out, and hold it for
        ``lease_seconds``: the oldest of the organisation with the fewest pieces running, so that
        organisations share the workers. None when there is none, or when another worker took it
        first."""

        def claim(connection: Connection) -> Claim | None:
            now = _now()
            claimable = or_(
                and_(
                    work_queue.c.state == QUEUED,
                    or_(work_queue.c.available_at.is_(None), work_queue.c.available_at <= now),
                ),
                and_(work_queue.c.state == CLAIMED, work_queue.c.lease_expires_at <= now),
            )
            other = work_queue.alias("running")
            running = (
                select(func.count())
                .select_from(other)
                .where(
                    other.c.space == work_queue.c.space,
                    other.c.state == CLAIMED,
                    other.c.lease_expires_at > now,
                )
                .correlate(work_queue)
                .scalar_subquery()
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
                .order_by(running, work_queue.c.created_at, work_queue.c.id)
                .limit(1)
            )
            if self._postgres:
                query = query.with_for_update(skip_locked=True, of=work_queue)
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
        """Extend a current claim. Expired claims cannot be revived."""

        def renew(connection: Connection) -> bool:
            if not lock_current_lease(connection, claim.ownership(), _now):
                return False
            connection.execute(
                update(work_queue)
                .where(self._key(claim))
                .values(lease_expires_at=_now() + timedelta(seconds=lease_seconds))
            )
            return True

        return await self._run(claim.space, renew)

    async def _finish(self, claim: Claim, statement: Delete | Update) -> bool:
        def finish(connection: Connection) -> bool:
            if not lock_current_lease(connection, claim.ownership(), _now):
                return False
            connection.execute(statement.where(self._key(claim)))
            return True

        return await self._run(claim.space, finish)

    async def release(self, claim: Claim) -> None:
        """Give the work back unfinished, for the next worker to take at once, without counting
        this attempt: the worker is stopping, not failing. Does nothing once the claim no longer
        holds it."""
        await self._finish(
            claim,
            update(work_queue).values(
                state=QUEUED, token=None, lease_expires_at=None, attempts=work_queue.c.attempts - 1
            ),
        )

    async def done(self, claim: Claim) -> None:
        """Remove the work, which has ended. Does nothing once the claim no longer holds it."""
        await self._finish(claim, delete(work_queue))

    async def reschedule(self, claim: Claim, available_at: datetime) -> bool:
        """Give back work that has ended for now and comes back, such as a monitor's next pass: it
        is queued again, with no attempt counted, and no claim takes it before ``available_at``;
        it then waits behind the work queued before that time. False when the claim no longer
        holds it."""
        return await self._finish(
            claim,
            update(work_queue).values(
                state=QUEUED,
                token=None,
                lease_expires_at=None,
                attempts=0,
                available_at=available_at,
                created_at=available_at,
            ),
        )

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
                    work_queue.c.lease_expires_at > _now(),
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
        """How many pieces of work, of every organisation, are in the queue, queued or claimed,
        leaving out work rescheduled for later that is not due yet."""

        def pending(connection: Connection) -> int:
            due = or_(work_queue.c.available_at.is_(None), work_queue.c.available_at <= _now())
            return int(connection.execute(select(func.count()).select_from(work_queue).where(due)).scalar_one())

        return await self._run(None, pending)


__all__ = ["CLAIMED", "QUEUED", "RECORDING", "AlreadyHeld", "Claim", "SqlWorkQueue"]
