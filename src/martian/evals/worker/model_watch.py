"""Watching the model behind a schedule's target (MartianSecurity/martian-notes#200).

A provider can change the model behind an agent without notice, and a regression suite should run
again when it does. A schedule with ``on_model_change`` (:class:`~martian.evals.records.ModelWatch`)
has its turns, which come back every ``model_watch_seconds`` as well as at its slots, read the
model behind its target and start the schedule's suite once when it changes
(:func:`martian.evals.worker.schedule.execute_schedule`). This module is the reading and the
deciding; :func:`watch_model` is one turn's worth.

Where the model is read, the watch's ``source``:

- ``traces``: the model of the project's live traces (``Trace.model``, which the trace conventions
  map from ``gen_ai.response.model`` before ``gen_ai.request.model``), read by time only, as a
  monitor reads them: request times from the watch's watermark (at first when the schedule was
  created or last resumed) to ``now - settle_seconds``, oldest first, at most
  :data:`READ_BOUND` traces a turn. Traces of eval runs and traces still in progress are not
  traffic. A trace whose search result names no model is not read.
- ``runs``: the model the target reported on the project's runs of it
  (``Run.provenance["model"]``), from the newest :data:`RUN_PAGE` runs, each counted once as it
  finishes after the watermark. A run the schedule itself started evaluated the model it reports,
  so it moves the watch's model there without starting anything.

What counts as a change, :func:`observe` and :func:`due_change`:

- The first model seen is the watch's ``model``: the latest from before the watch started, when
  there is one, else the first after. Nothing runs for it; the schedule's slots evaluate it.
- Another model becomes the ``candidate``, from when it was seen first after the last sight of any
  other (``candidate_since``). Seeing the watch's model again drops the candidate, and seeing a
  third model makes that one the candidate from then.
- **Debounce.** A candidate is a change once ``settle_seconds`` have passed since
  ``candidate_since`` with no other model seen. A model that flaps back and forth more often than
  that never settles, so it starts nothing.
- **At most one run per interval.** A change starts the suite only when ``min_interval_seconds``
  have passed since the last change that did; until then it waits, and if the model goes back
  meanwhile it starts nothing. So however a model flaps, the watch starts the suite at most once
  per interval.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from martian.evals.model import Run, Trace, TraceQuery
from martian.evals.records import ModelChange, ModelWatch, ModelWatchProgress, RecordStore, Schedule
from martian.evals.store import EvalStore

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

log = logging.getLogger("martian.evals.worker")

READ_BOUND = 1000
"""The most traces one turn reads, as a monitor's pass does."""
PAGE = 100
RUN_PAGE = 100
"""How many of the project's newest runs one turn reads for the ``runs`` source."""
BASELINE_PAGE = 50
"""How many traces before the watch started are looked through for the model it starts from."""
SETTLE_SECONDS = 20.0
"""How long after its request time a trace is first read, as for a monitor."""
WATCH_SECONDS = 60.0
"""How often a schedule that watches its model takes a turn, besides its slots."""
READ_FAILED = "The last read of the model failed; the next turn reads again from where it stopped"
FINISHED = ("completed", "partial", "failed", "cancelled", "stopped")


@dataclass(frozen=True)
class Observation:
    """One sight of the model: ``at`` a trace's request time or a run's end, and ``own`` when the
    schedule's own run reported it."""

    at: datetime
    model: str
    own: bool = False


def observe(watch: ModelWatchProgress, observations: Iterable[Observation]) -> ModelWatchProgress:
    """``watch`` after ``observations``, in time order."""
    model, candidate, since, seen = watch.model, watch.candidate, watch.candidate_since, watch.observed
    for sight in observations:
        seen += 1
        if sight.own or model is None:
            model = sight.model  # the first model seen, or one the schedule's own run evaluated
            if candidate == model:
                candidate, since = None, None
        elif sight.model == model:
            candidate, since = None, None
        elif sight.model != candidate:
            candidate, since = sight.model, sight.at
    return watch.model_copy(update={"model": model, "candidate": candidate, "candidate_since": since, "observed": seen})


def due_change(watch: ModelWatchProgress, settings: ModelWatch, now: datetime) -> tuple[str, str] | None:
    """The change, ``(previous, current)``, that starts the suite now: a candidate seen alone for
    ``settle_seconds``, at least ``min_interval_seconds`` after the last change that did."""
    if watch.model is None or watch.candidate is None or watch.candidate_since is None:
        return None
    if now - watch.candidate_since < timedelta(seconds=settings.settle_seconds):
        return None
    if watch.last_change_at is not None and now - watch.last_change_at < timedelta(
        seconds=settings.min_interval_seconds
    ):
        return None
    return watch.model, watch.candidate


def settled(watch: ModelWatchProgress, change: ModelChange, now: datetime) -> ModelWatchProgress:
    """``watch`` once ``change`` is in the history: its model is the one the suite ran for."""
    keep = watch.candidate is not None and watch.candidate != change.current
    return watch.model_copy(
        update={
            "model": change.current,
            "candidate": watch.candidate if keep else None,
            "candidate_since": watch.candidate_since if keep else None,
            "last_change_at": now,
            "change": None,
        }
    )


def _start(schedule: Schedule, watch: ModelWatchProgress) -> datetime | None:
    """Where reading starts: the watermark, or when the schedule was created or last resumed,
    whichever is later, so nothing seen while it was paused counts."""
    starts = [moment for moment in (watch.watermark, schedule.active_since, schedule.created_at) if moment]
    return max(starts) if starts else None


async def _latest_model(store: EvalStore, principal: Principal, project_id: str, until: datetime) -> str | None:
    """The model of the newest live trace among the :data:`BASELINE_PAGE` newest before ``until``.
    A page of a trace search may hold fewer traces than its limit, or none, and still have a
    ``next_cursor``, so the search reads on until it has looked through that many or ends."""
    query = TraceQuery(project_id=project_id, until=until, states=["ok", "error"], limit=BASELINE_PAGE)
    looked: list[Trace] = []
    while True:
        page = await store.search_traces(principal, query)
        looked += page.items
        if len(looked) >= BASELINE_PAGE or page.next_cursor is None:
            break
        query = query.model_copy(update={"cursor": page.next_cursor})
    return next((t.model for t in looked[:BASELINE_PAGE] if t.run_id is None and t.model), None)


async def read_traces(
    store: EvalStore,
    principal: Principal,
    schedule: Schedule,
    watch: ModelWatchProgress,
    now: datetime,
    *,
    settle_seconds: float = SETTLE_SECONDS,
    read_bound: int = READ_BOUND,
) -> ModelWatchProgress:
    """``watch`` after reading the project's new traces by time."""
    until = now - timedelta(seconds=settle_seconds)
    since = _start(schedule, watch) or until
    if watch.model is None and watch.watermark is None:
        latest = await _latest_model(store, principal, schedule.project_id, since)
        if latest is not None:
            watch = watch.model_copy(update={"model": latest})
    if since >= until:
        return watch.model_copy(update={"watermark": since, "caught_up": True})
    query = TraceQuery(
        project_id=schedule.project_id, states=["ok", "error"], since=since, until=until, oldest_first=True, limit=PAGE
    )
    read, cursor, watermark = 0, None, since
    while True:
        page = await store.search_traces(principal, query.model_copy(update={"cursor": cursor}))
        sights = []
        for trace in page.items:
            read += 1
            watermark = trace.request_time or watermark
            if trace.run_id is None and trace.model and trace.request_time is not None:
                sights.append(Observation(trace.request_time, trace.model))
        watch = observe(watch, sights)
        if page.next_cursor is None:
            return watch.model_copy(update={"watermark": until, "caught_up": True})
        if read >= read_bound:  # more to read: the next turn goes on at once
            return watch.model_copy(update={"watermark": watermark, "caught_up": False})
        cursor = page.next_cursor


def _scheduled_by(run: Run) -> str | None:
    # The schedule module defines the key; it imports this one.
    from martian.evals.worker.schedule import SCHEDULE_KEY

    found = run.provenance.get(SCHEDULE_KEY)
    return str(found.get("id")) if isinstance(found, dict) else None


def _model(run: Run) -> str | None:
    # The routes' modules import the worker package, which imports this module through the
    # schedule module, so the work's module is imported when a turn needs it.
    from martian.evals.worker.execute import run_model

    return run_model(run)


async def read_runs(
    records: RecordStore, principal: Principal, schedule: Schedule, watch: ModelWatchProgress
) -> ModelWatchProgress:
    """``watch`` after reading the model the target reported on the project's newest runs."""
    agent_id = schedule.target.agent_id
    page = await records.list_runs(principal, schedule.project_id, limit=RUN_PAGE)
    finished = sorted(
        (
            (run.finished_at, model, _scheduled_by(run) == schedule.id)
            for run in page.items
            if run.target is not None
            and run.target.agent_id == agent_id
            and run.state in FINISHED
            and run.finished_at is not None
            and (model := _model(run)) is not None
        ),
        key=lambda found: found[0],
    )
    since = _start(schedule, watch)
    if watch.model is None and watch.watermark is None and since is not None:
        before = [model for at, model, _own in finished if at <= since]
        if before:
            watch = watch.model_copy(update={"model": before[-1]})
    sights = [Observation(at, model, own) for at, model, own in finished if since is None or at > since]
    watch = observe(watch, sights)
    latest = max((sight.at for sight in sights), default=since)
    return watch.model_copy(update={"watermark": latest, "caught_up": True})


async def watch_model(
    store: EvalStore,
    records: RecordStore,
    principal: Principal,
    schedule: Schedule,
    watch: ModelWatchProgress | None,
    now: datetime,
    *,
    slot: datetime | None,
    settle_seconds: float = SETTLE_SECONDS,
) -> ModelWatchProgress:
    """One turn of the schedule's model watch: read what is new, and decide whether a change
    starts the suite. A change it decides is the watch's ``change``, at ``slot`` when the turn
    also runs that regular slot, whose run then stands for the change, or at ``now`` in a slot of
    its own. A change already decided and not yet in the history is left for the turn to record,
    with nothing read. A read that fails is the watch's ``error``; the next turn reads again."""
    settings = schedule.on_model_change
    watch = watch or ModelWatchProgress()
    if settings is None or watch.change is not None:
        return watch
    try:
        if settings.source == "traces":
            watch = await read_traces(store, principal, schedule, watch, now, settle_seconds=settle_seconds)
        else:
            watch = await read_runs(records, principal, schedule, watch)
    except Exception:  # noqa: BLE001 - the schedule's slots go on; the next turn reads again
        log.exception("Schedule %s could not read the model behind its target", schedule.id)
        return watch.model_copy(update={"error": READ_FAILED, "checked_at": now})
    watch = watch.model_copy(update={"error": "", "checked_at": now})
    if (found := due_change(watch, settings, now)) is not None:
        previous, current = found
        watch = watch.model_copy(update={"change": ModelChange(previous=previous, current=current, at=slot or now)})
    return watch


__all__ = [
    "BASELINE_PAGE",
    "READ_BOUND",
    "READ_FAILED",
    "RUN_PAGE",
    "SETTLE_SECONDS",
    "WATCH_SECONDS",
    "Observation",
    "due_change",
    "observe",
    "read_runs",
    "read_traces",
    "settled",
    "watch_model",
]
