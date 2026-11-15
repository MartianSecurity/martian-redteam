"""The durable queue: a worker whose ``submit`` puts the work in the record store's database, and the
worker process that takes it from there (MorningHQ/martian-notes#139).

:class:`QueueWorker` is a :class:`~martian.evals.worker.Worker` like the others, so the routes do
not change. ``submit`` puts the work in the queue (:class:`~martian.evals.records.queue.SqlWorkQueue`,
in the record store's database: SQLite locally, Postgres on a server) and returns. A worker then
takes it with :meth:`QueueWorker.serve`, up to ``concurrency`` pieces at once, and runs each with
its *runner*, a :class:`~martian.evals.worker.Runner`: an :class:`~martian.evals.worker.InProcessWorker`,
or a :class:`~martian.evals.worker.ProcessWorker`, which gives each piece a process of its own.

- **Several at once, shared fairly.** A worker with free places takes the next piece at once, and
  each claim takes the oldest piece of the organisation with the fewest pieces running, across
  every worker of the queue (MorningHQ/martian-notes#145). So one organisation that queues many
  runs holds back another's only until a place frees, never until its own queue is empty. How
  many of an organisation's runs and scoring jobs may be in progress at all is the edition's
  limit, checked when the work is admitted (:mod:`martian.evals.server.admission`).

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
  job still ``running`` is queued again and resumes after the cases already finished, whose
  target or judge is not called again (:func:`take_over`). A worker that
  is stopped (``stop``, or a cancelled ``serve``) stops its work and gives it back at once, without
  counting the attempt. A ``ProcessWorker``'s run process ends with the worker that started it.
  After ``max_attempts`` claims, the work is ``failed`` with :data:`STOPPED_TOO_OFTEN`, not taken
  again.
- **What it never does.** It never retries a target or a judge, and work that ends ``failed`` stays
  failed: only a worker that stopped before the work ended makes it run again.
- **Runs recorded outside the queue.** A process that records a run itself, such as
  ``martian evals run``, holds it in the queue as a ``recording`` claim, which it renews
  (:meth:`~martian.evals.records.queue.SqlWorkQueue.hold`). The worker never runs one: once its
  lease runs out, the process recording it is gone, and the worker finishes the run as interrupted
  (:data:`~martian.evals.worker.execute.INTERRUPTED`).
- **Cancelling.** It tells the cancel route whether anything is working on a running run
  (:class:`~martian.evals.worker.InterruptedRuns`): a run no live claim holds is cancelled at once
  as interrupted, since no worker will reach its next case.
- **Monitors.** A monitor's work stays in the queue for as long as the monitor exists
  (:class:`~martian.evals.worker.RunsMonitors`). Each claim of it is one pass over the monitor's
  new traces (:mod:`martian.evals.worker.monitor`), after which the worker gives the row back, due
  ``monitor_seconds`` later, or at once when the pass stopped at its read bound with more to read
  (:meth:`~martian.evals.records.queue.SqlWorkQueue.reschedule`). A paused monitor's passes do
  nothing; a deleted monitor's row is removed at its next pass. A monitor whose worker stopped
  during its pass ``max_attempts`` times is paused, with :data:`MONITOR_STOPPED_TOO_OFTEN` as its
  progress's error.

So restarting the API process loses no queued or running run, whether its worker runs in that
process or in another. Progress events are the record store's (``records.events``), as with any
worker: a worker in another process publishes them on its own record store's bus, so an
installation that runs one gives both processes a bus that reaches between them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from martian.evals.model import Run, Target
from martian.evals.records import RecordStore
from martian.evals.records.queue import RECORDING, Claim, SqlWorkQueue
from martian.evals.store import StoreError
from martian.evals.worker import Runner, Work
from martian.evals.worker.execute import fail_unfinished, interrupt, take_over
from martian.evals.worker.process import dump_work, load_work

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

log = logging.getLogger("martian.evals.worker")

LEASE_SECONDS = 30.0
POLL_SECONDS = 1.0
MAX_ATTEMPTS = 3
CONCURRENCY = 1
"""How many pieces of work a worker runs at once unless it is given another number."""
MONITOR_SECONDS = 10.0
"""How long after a monitor's pass ends its next pass is due, unless the worker is given another."""
STOPPED_TOO_OFTEN = "The worker stopped before this work finished, too many times; it is not taken again"
MONITOR_STOPPED_TOO_OFTEN = "The worker stopped during this monitor's pass too many times; the monitor is paused"


def entry_id(work: Work) -> str:
    """The work's id in the queue: ``run:<run id>``, ``scoring:<scoring job id>`` or
    ``monitor:<monitor id>``."""
    if work.kind == "monitor":
        return f"monitor:{work.monitor_id}"
    return f"scoring:{work.job_id}" if work.kind == "scoring" else f"run:{work.run_id}"


class QueueWorker:
    """A worker backed by ``queue``, which runs the work it takes with ``runner``.

    ``records`` is the record store the work's records are in, which the worker reads and moves on
    when it takes over work whose worker stopped. ``in_process`` serves the queue in this process
    while the app does. ``lease_seconds`` is how long a claim holds work without being renewed,
    ``poll_seconds`` how often an idle worker looks for work another process queued,
    ``max_attempts`` how many workers may take one piece of work before it is failed,
    ``concurrency`` how many pieces this worker runs at once, of any organisations, and
    ``monitor_seconds`` how long after a monitor's pass ends its next pass is due."""

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
        concurrency: int = CONCURRENCY,
        monitor_seconds: float = MONITOR_SECONDS,
    ) -> None:
        if lease_seconds <= 0 or poll_seconds <= 0 or monitor_seconds <= 0 or max_attempts < 1 or concurrency < 1:
            raise ValueError(
                "lease_seconds, poll_seconds and monitor_seconds are positive, "
                "and max_attempts and concurrency at least 1"
            )
        self._queue = queue
        self._runner = runner
        self._records = records
        self._in_process = in_process
        self._lease = lease_seconds
        self._poll = poll_seconds
        self._max_attempts = max_attempts
        self._concurrency = concurrency
        self._monitor_seconds = monitor_seconds
        self._wake = asyncio.Event()
        self._serving: asyncio.Task[None] | None = None

    @property
    def judges(self) -> bool:
        return self._runner.judges

    @property
    def concurrency(self) -> int:
        """How many pieces of work this worker runs at once."""
        return self._concurrency

    @property
    def monitor_seconds(self) -> float:
        """How long after a monitor's pass ends its next pass is due (``RunsMonitors``)."""
        return self._monitor_seconds

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        return await self._runner.check_target(principal, target)

    async def submit(self, work: Work) -> None:
        """Put the work in the queue and return. Submitting the same work again while it is queued
        or running leaves the queue as it is."""
        await self._queue.put(work.principal, work.project_id, entry_id(work), work.kind, dump_work(work))
        self._wake.set()

    async def cancel_interrupted(self, principal: Principal, run: Run) -> Run | None:
        """The cancel route's question (``InterruptedRuns``): cancel the running ``run`` at once, as
        interrupted, when no live claim holds it, so nothing would reach its next case; None while
        one does, which then stops it before its next case."""
        work = Work(kind="run", principal=principal, project_id=run.project_id, run_id=run.id)
        if await self._queue.held(principal, run.project_id, entry_id(work)):
            return None
        return await interrupt(self._records, principal, run, "cancelled")

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
        """Take work from the queue and run it, up to ``concurrency`` pieces at once, until
        cancelled. Cancelled, it stops the work in hand and gives each piece back to the queue."""
        turns: set[asyncio.Task[None]] = set()
        loop = asyncio.get_running_loop()
        try:
            while True:
                if len(turns) >= self._concurrency:
                    await asyncio.wait(turns, return_when=asyncio.FIRST_COMPLETED)
                    continue
                claim = await self._next()
                if claim is None:
                    self._wake.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), self._poll)
                    continue
                turn = loop.create_task(self._turn(claim), name=f"martian-queue-work-{claim.id}")
                turns.add(turn)
                turn.add_done_callback(turns.discard)
        finally:
            for turn in turns:
                turn.cancel()
            await asyncio.gather(*turns, return_exceptions=True)

    async def _turn(self, claim: Claim) -> None:
        try:
            await self._handle(claim)
        except Exception:  # noqa: BLE001 - the database is away; the claim runs out and is taken again
            log.exception("The worker could not end its turn on %s", claim.id)

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
        if claim.kind == RECORDING:  # the process recording this run stopped before it ended
            await self._interrupted(work)
            await self._queue.done(claim)
            return
        if claim.attempt > self._max_attempts and work.kind == "monitor":
            log.error("Monitor %s: its worker stopped %d times during a pass", work.monitor_id, claim.attempt - 1)
            await self._pause(work)
            await self._next_pass(claim, work)
            return
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
        if ended and work.kind == "monitor":
            await self._next_pass(claim, work)
        elif ended:
            await self._queue.done(claim)

    async def _next_pass(self, claim: Claim, work: Work) -> None:
        """Give a monitor's row back for its next pass, due ``monitor_seconds`` from now, or at once
        when its last pass stopped at its read bound; remove it once the monitor is gone."""
        monitor_id = work.monitor_id or ""
        monitor = await self._records.get_monitor(work.principal, work.project_id, monitor_id)
        if monitor is None:
            await self._queue.done(claim)
            return
        progress = await self._records.monitor_progress(work.principal, work.project_id, monitor_id)
        behind = monitor.state == "active" and progress is not None and not progress.caught_up
        due = datetime.now(UTC) + timedelta(seconds=0 if behind else self._monitor_seconds)
        await self._queue.reschedule(claim, due)

    async def _pause(self, work: Work) -> None:
        """Pause a monitor whose worker stopped during its pass too many times, and say so."""
        monitor_id = work.monitor_id or ""
        monitor = await self._records.get_monitor(work.principal, work.project_id, monitor_id)
        if monitor is None:
            return
        if monitor.state == "active":
            with contextlib.suppress(StoreError):  # changed meanwhile: its next pass sees it as it is
                paused = monitor.model_copy(update={"state": "paused"})
                await self._records.update_monitor(
                    work.principal, work.project_id, paused, expected_revision=monitor.revision
                )
        await fail_unfinished(self._records, work, MONITOR_STOPPED_TOO_OFTEN)

    async def _interrupted(self, work: Work) -> None:
        run = await self._records.get_run(work.principal, work.project_id, work.run_id)
        if run is None or run.state not in ("queued", "running"):
            return
        try:
            await interrupt(self._records, work.principal, run, "cancelled" if run.cancel_requested_at else "failed")
        except StoreError as error:
            if error.kind != "conflict":
                raise
            return  # it moved on meanwhile
        log.warning("Run %s was interrupted: the process recording it stopped", run.code or run.id)

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


__all__ = [
    "CONCURRENCY",
    "LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "MONITOR_SECONDS",
    "MONITOR_STOPPED_TOO_OFTEN",
    "POLL_SECONDS",
    "STOPPED_TOO_OFTEN",
    "QueueWorker",
    "entry_id",
]
