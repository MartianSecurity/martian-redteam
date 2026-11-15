"""The durable queue: a worker whose ``submit`` puts the work in the record store's database, and the
worker process that takes it from there (MorningHQ/martian-notes#139).

:class:`QueueWorker` is a :class:`~martian.evals.worker.Worker` like the others, so the routes do
not change. ``submit`` puts the work in the queue (:class:`~martian.evals.records.queue.SqlWorkQueue`,
in the record store's database: SQLite locally, Postgres on a server) and returns. A worker then
takes it with :meth:`QueueWorker.serve`, one piece at a time, and runs it with its *runner*, a
:class:`~martian.evals.worker.Runner`: an :class:`~martian.evals.worker.InProcessWorker`, or a
:class:`~martian.evals.worker.ProcessWorker`, which gives each piece a process of its own.

- **Where the worker runs.** With ``in_process=True`` the worker serves beside the app, in the API
  process, which starts it when it starts serving and stops it when it stops
  (``create_app`` does both): the local edition, and an installation with no worker process.
  Otherwise the API process only queues work, and a worker process of its own builds the same
  ``QueueWorker`` and awaits :meth:`~QueueWorker.serve`.
- **Claims and leases.** Taking a piece of work claims it for ``lease_seconds``. While the work
  runs, the worker renews the claim every third of the lease; once it has ended, the worker
  removes it from the queue. Two workers never hold one piece of work at once. A worker that finds
  its claim taken by another, after its lease ran out, stops the work at once.
- **A worker that stops.** Work whose worker died, or lost the database for a whole lease, is
  claimable again once the lease runs out, and the next worker takes it again: a run or scoring
  job still ``running`` is queued again and runs from the start (:func:`take_over`). A worker that
  is stopped (``stop``, or a cancelled ``serve``) stops its work and gives it back at once, without
  counting the attempt. A ``ProcessWorker``'s run process ends with the worker that started it.
  After ``max_attempts`` claims, the work is ``failed`` with :data:`STOPPED_TOO_OFTEN`, not taken
  again.
- **What it never does.** It never retries a target or a judge, and work that ends ``failed`` stays
  failed: only a worker that stopped before the work ended makes it run again.

So restarting the API process loses no queued or running run, whether its worker runs in that
process or in another. Progress events are the record store's (``records.events``), as with any
worker: a worker in another process publishes them on its own record store's bus, so an
installation that runs one gives both processes a bus that reaches between them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from martian.evals.model import Target
from martian.evals.records import RecordStore
from martian.evals.records.queue import Claim, SqlWorkQueue
from martian.evals.worker import Runner, Work
from martian.evals.worker.execute import fail_unfinished, take_over
from martian.evals.worker.process import dump_work, load_work

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

log = logging.getLogger("martian.evals.worker")

LEASE_SECONDS = 30.0
POLL_SECONDS = 1.0
MAX_ATTEMPTS = 3
STOPPED_TOO_OFTEN = "The worker stopped before this work finished, too many times; it is not taken again"


def entry_id(work: Work) -> str:
    """The work's id in the queue: ``run:<run id>`` or ``scoring:<scoring job id>``."""
    return f"scoring:{work.job_id}" if work.kind == "scoring" else f"run:{work.run_id}"


class QueueWorker:
    """A worker backed by ``queue``, which runs the work it takes with ``runner``.

    ``records`` is the record store the work's records are in, which the worker reads and moves on
    when it takes over work whose worker stopped. ``in_process`` serves the queue in this process
    while the app does. ``lease_seconds`` is how long a claim holds work without being renewed,
    ``poll_seconds`` how often an idle worker looks for work another process queued, and
    ``max_attempts`` how many workers may take one piece of work before it is failed."""

    def __init__(
        self,
        queue: SqlWorkQueue,
        runner: Runner,
        records: RecordStore,
        *,
        in_process: bool = False,
        lease_seconds: float = LEASE_SECONDS,
        poll_seconds: float = POLL_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if lease_seconds <= 0 or poll_seconds <= 0 or max_attempts < 1:
            raise ValueError("lease_seconds and poll_seconds are positive and max_attempts at least 1")
        self._queue = queue
        self._runner = runner
        self._records = records
        self._in_process = in_process
        self._lease = lease_seconds
        self._poll = poll_seconds
        self._max_attempts = max_attempts
        self._wake = asyncio.Event()
        self._serving: asyncio.Task[None] | None = None

    @property
    def judges(self) -> bool:
        return self._runner.judges

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        return await self._runner.check_target(principal, target)

    async def submit(self, work: Work) -> None:
        """Put the work in the queue and return. Submitting the same work again while it is queued
        or running leaves the queue as it is."""
        await self._queue.put(work.principal, work.project_id, entry_id(work), work.kind, dump_work(work))
        self._wake.set()

    # --- serving ---------------------------------------------------------------------------------

    async def start(self) -> None:
        """Start serving the queue in this process, when the worker is ``in_process``. The app calls
        it when it starts serving."""
        if self._in_process and self._serving is None:
            self._serving = asyncio.get_running_loop().create_task(self.serve(), name="martian-queue-worker")

    async def stop(self) -> None:
        """Stop serving the queue in this process, giving back the work in hand. The app calls it
        when it stops serving."""
        serving, self._serving = self._serving, None
        if serving is not None:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)

    async def serve(self) -> None:
        """Take work from the queue and run it, one piece at a time, until cancelled. Cancelled, it
        stops the work in hand and gives it back to the queue."""
        while True:
            claim = await self._next()
            if claim is None:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._poll)
                continue
            await self._handle(claim)

    async def drain(self) -> None:
        """Wait until the queue holds no work, of any organisation: for the local edition and for
        tests, where one worker serves the whole queue."""
        while await self._queue.pending():
            await asyncio.sleep(self._poll)

    async def _next(self) -> Claim | None:
        try:
            return await self._queue.claim(self._lease)
        except Exception:  # noqa: BLE001 - the database is away; look again at the next poll
            log.exception("The worker could not read its queue")
            return None

    async def _handle(self, claim: Claim) -> None:
        work = load_work(claim.work)
        if claim.attempt > self._max_attempts:
            log.error("%s work on run %s: its worker stopped %d times", work.kind, work.run_id, claim.attempt - 1)
            await fail_unfinished(self._records, work, STOPPED_TOO_OFTEN)
            await self._queue.done(claim)
            return
        running = asyncio.get_running_loop().create_task(self._run(work))
        try:
            ended = await self._hold(claim, running)
        except asyncio.CancelledError:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await self._queue.release(claim)
            raise
        if ended:
            await self._queue.done(claim)

    async def _run(self, work: Work) -> None:
        await take_over(self._records, work)
        await self._runner.run(work)

    async def _hold(self, claim: Claim, running: asyncio.Task[None]) -> bool:
        """Renew the claim while the work runs. True once the work has ended; False when it stopped
        with an error, which leaves the claim to run out so the next worker takes it again, or when
        another worker took the claim, which stops the work."""
        while True:
            done, _ = await asyncio.wait({running}, timeout=self._lease / 3)
            if done:
                if running.cancelled() or (error := running.exception()) is not None:
                    log.error("The worker stopped on %s: %r", claim.id, None if running.cancelled() else error)
                    return False
                return True
            try:
                renewed = await self._queue.renew(claim, self._lease)
            except Exception:  # noqa: BLE001 - the database is away; try again at the next renewal
                log.exception("The worker could not renew its claim on %s", claim.id)
                continue
            if not renewed:
                log.warning("Another worker took %s after this worker's lease ran out; stopping it here", claim.id)
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                return False


__all__ = ["LEASE_SECONDS", "MAX_ATTEMPTS", "POLL_SECONDS", "STOPPED_TOO_OFTEN", "QueueWorker", "entry_id"]
