"""Thresholds on scores, judged on their 95% intervals, with a state machine that raises one alert
per breach and one per recovery (MorningHQ/martian-notes#198).

A threshold (:class:`~martian.evals.records.Threshold`) is healthy while a measure of a source's
scores, a pass rate or a mean, is at least or at most a value. Each evaluation reads the scores of
the last ``window_seconds`` from its source and computes the measure's 95% interval with
:mod:`martian.evals.stats`: Wilson for a pass rate, Student t for a mean, both ungrouped, since a
trace carries no group.

**It alerts on the interval, not on the point estimate.** A window of sampled traffic is a sample,
so its rate moves from one evaluation to the next with no change in quality: at the point
estimate, a monitor whose real pass rate sits at its threshold would breach and recover on about
half its evaluations. So a verdict is ``breach`` only when the whole interval is on the wrong side
of the value (the upper bound below an ``at_least`` value, the lower bound above an ``at_most``
one), which by chance happens in at most 2.5% of windows of a source that is at the value, and
``healthy`` only when the whole interval is on the right side. While the interval holds the value
the verdict is ``undecided``, and below ``min_count`` scores ``insufficient``; neither changes the
state.

**The state machine** (:func:`next_state`) is ``unknown`` until the first decided verdict, then
``ok`` or ``breached``. The band between the two verdicts, the interval's width either side of
the value, is its hysteresis: after a breach the rate must climb until its whole interval clears
the value before it recovers, and noise inside the band changes nothing. A change to ``breached``
raises a breach alert per channel; a change from ``breached`` to ``ok`` a recovery alert; the first
change from ``unknown`` to ``ok`` none. An evaluation writes its change of state and the alerts it
raises in one transaction, conditioned on the number of changes it read
(:meth:`~martian.evals.records.RecordStore.set_threshold_status`), so a change is written once
however often, and by however many workers, the same window is evaluated, and a worker that
restarts reads the state it left and raises nothing again. Delivering the alerts is
:mod:`martian.evals.alerts`.

**Sources.** What a threshold judges comes from a :class:`Source`, which finds each threshold's
window of values. :class:`MonitorSource` reads a monitor's scores: the traces the monitor scored in
the window, by when it took them, newest first, at most
:data:`~martian.evals.records.THRESHOLD_WINDOW_BOUND`, with the value of the threshold's scorer on
each (a score without a verdict counts for nothing). Scheduled regression runs
(MorningHQ/martian-notes#197) add their gate results as a second source, ``kind="gate"``, whose
window is the scheduled runs' gate outcomes as booleans, judged by the same state machine.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from martian.evals import stats
from martian.evals.model import Value
from martian.evals.records import (
    THRESHOLD_WINDOW_BOUND,
    Alert,
    Observation,
    RecordStore,
    Threshold,
    ThresholdStatus,
)

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

log = logging.getLogger("martian.evals.thresholds")

State = Literal["unknown", "ok", "breached"]
Verdict = Literal["insufficient", "undecided", "breach", "healthy"]
Event = Literal["breached", "recovered"]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Window:
    """The values a source found for one threshold in ``[start, end)``: each score's value, which
    the threshold's measure reads as it can (a pass rate its booleans, a mean its other numbers).
    ``truncated`` when the window held more than the source reads in one evaluation."""

    start: datetime
    end: datetime
    values: Sequence[Value] = field(default_factory=tuple)
    truncated: bool = False


@runtime_checkable
class Source(Protocol):
    """Where the values a threshold judges come from: ``kind`` is the ``source.kind`` of the
    thresholds it reads, and :meth:`windows` gives each of them, by id, its window ending ``now``."""

    @property
    def kind(self) -> str: ...

    async def windows(
        self, principal: Principal, project_id: str, thresholds: Sequence[Threshold], *, now: datetime
    ) -> Mapping[str, Window]: ...


class MonitorSource:
    """A monitor's scores (``kind="monitor"``): the traces the monitor scored in each threshold's
    window, read once per monitor for the widest window of its thresholds."""

    kind = "monitor"

    def __init__(self, records: RecordStore) -> None:
        self._records = records

    async def windows(
        self, principal: Principal, project_id: str, thresholds: Sequence[Threshold], *, now: datetime
    ) -> Mapping[str, Window]:
        by_monitor: dict[str, list[Threshold]] = {}
        for threshold in thresholds:
            by_monitor.setdefault(threshold.source.monitor_id, []).append(threshold)
        found: dict[str, Window] = {}
        for monitor_id, group in by_monitor.items():
            widest = max(t.window_seconds for t in group)
            rows = await self._records.scored_monitored_traces(
                principal,
                project_id,
                monitor_id,
                since=now - timedelta(seconds=widest),
                until=now,
                limit=THRESHOLD_WINDOW_BOUND,
            )
            oldest = rows[-1].created_at if len(rows) >= THRESHOLD_WINDOW_BOUND else None
            for threshold in group:
                start = now - timedelta(seconds=threshold.window_seconds)
                values = [
                    score.value
                    for row in rows
                    if row.created_at is not None and row.created_at >= start
                    for score in row.scores
                    if score.scorer == threshold.source.scorer and score.value is not None and not score.error
                ]
                # The bound cut the read inside this window: older scores in it were not read.
                truncated = oldest is not None and oldest >= start
                found[threshold.id] = Window(start=start, end=now, values=values, truncated=truncated)
        return found


def interval_verdict(direction: str, value: float, interval: tuple[float, float]) -> Verdict:
    """``breach`` when the whole interval is on the wrong side of ``value``, ``healthy`` when it is
    wholly on the right side, ``undecided`` while it holds it."""
    low, high = interval
    if direction == "at_least":
        if high < value:
            return "breach"
        if low >= value:
            return "healthy"
    else:
        if low > value:
            return "breach"
        if high <= value:
            return "healthy"
    return "undecided"


def observe(threshold: Threshold, window: Window) -> Observation:
    """The threshold's measure over ``window``, its 95% interval, and the verdict."""
    found: tuple[float, float] | None
    if threshold.measure == "pass_rate":
        outcomes = [v for v in window.values if isinstance(v, bool)]
        count = len(outcomes)
        estimate = sum(outcomes) / count if count else None
        found = stats.wilson(sum(outcomes), count)
    else:
        numbers = [
            float(v)
            for v in window.values
            if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v))
        ]
        count = len(numbers)
        estimate = fmean(numbers) if numbers else None
        found = stats.mean_interval(numbers)
    verdict: Verdict = "insufficient"
    if count >= threshold.min_count and found is not None:
        verdict = interval_verdict(threshold.direction, threshold.value, found)
    return Observation(
        at=window.end,
        window_start=window.start,
        count=count,
        estimate=estimate,
        interval=None if found is None else [found[0], found[1]],
        verdict=verdict,
        truncated=window.truncated,
    )


def next_state(state: State, verdict: Verdict) -> State:
    """A decided verdict sets the state; ``undecided`` and ``insufficient`` keep it."""
    if verdict == "breach":
        return "breached"
    if verdict == "healthy":
        return "ok"
    return state


def raised_event(before: State, after: State) -> Event | None:
    """The alert a change of state raises: a breach on any change to ``breached``, a recovery on a
    change from ``breached`` to ``ok``, none on the first change from ``unknown`` to ``ok``."""
    if after == before:
        return None
    if after == "breached":
        return "breached"
    return "recovered" if before == "breached" else None


def alert_id(threshold_id: str, sequence: int, channel: str) -> str:
    """The one alert of the threshold's change of state ``sequence`` to ``channel``."""
    # Imported here: the server package imports the worker, whose monitor pass imports this module.
    from martian.evals.server.projection import derived_id

    return str(derived_id("threshold-alert", threshold_id, str(sequence), channel))


def alert_payload(
    threshold: Threshold, event: Event, sequence: int, observation: Observation, *, alert: str, at: datetime
) -> dict[str, Any]:
    """What an alert sends: the webhook's JSON body, and what its email says."""
    from martian.evals.server.projection import project_out

    return {
        "id": alert,
        "type": f"threshold.{event}",
        "created_at": at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "sequence": sequence,
        "threshold": {
            "id": threshold.id,
            "name": threshold.name,
            "project_id": project_out(threshold.project_id),
            "source": threshold.source.model_dump(mode="json"),
            "measure": threshold.measure,
            "direction": threshold.direction,
            "value": threshold.value,
            "window_seconds": threshold.window_seconds,
            "min_count": threshold.min_count,
        },
        "observation": {**observation.model_dump(mode="json"), "confidence": stats.CONFIDENCE},
    }


def channels(threshold: Threshold) -> list[Literal["webhook", "email"]]:
    found: list[Literal["webhook", "email"]] = []
    if threshold.webhook is not None:
        found.append("webhook")
    if threshold.email is not None:
        found.append("email")
    return found


async def evaluate_thresholds(
    records: RecordStore,
    principal: Principal,
    project_id: str,
    thresholds: Sequence[Threshold],
    source: Source,
    *,
    now: datetime | None = None,
) -> list[Alert]:
    """Evaluate each of ``thresholds`` that ``source`` reads over its window ending ``now``, move
    its state machine, and record each change of state with the alerts it raises, in one
    transaction. The alerts this evaluation raised, for :mod:`martian.evals.alerts` to deliver."""
    now = now or _now()
    mine = [t for t in thresholds if t.source.kind == source.kind]
    if not mine:
        return []
    windows = await source.windows(principal, project_id, mine, now=now)
    raised: list[Alert] = []
    for threshold in mine:
        status = await records.threshold_status(principal, project_id, threshold.id)
        if status is None:  # deleted meanwhile
            continue
        observation = observe(threshold, windows[threshold.id])
        state = next_state(status.state, observation.verdict)
        alerts: list[Alert] = []
        if state == status.state:
            updated = status.model_copy(update={"last": observation})
        else:
            sequence = status.sequence + 1
            updated = ThresholdStatus(state=state, sequence=sequence, since=now, last=observation)
            if (event := raised_event(status.state, state)) is not None:
                for channel in channels(threshold):
                    found = alert_id(threshold.id, sequence, channel)
                    payload = alert_payload(threshold, event, sequence, observation, alert=found, at=now)
                    alerts.append(
                        Alert(
                            id=found,
                            threshold_id=threshold.id,
                            channel=channel,
                            event=event,
                            sequence=sequence,
                            payload=payload,
                            next_attempt_at=now,
                            created_at=now,
                        )
                    )
        if await records.set_threshold_status(
            principal, project_id, threshold.id, updated, expected_sequence=status.sequence, alerts=alerts
        ):
            if state != status.state:
                log.info("Threshold %s is %s (%s), %d alerts", threshold.id, state, observation.verdict, len(alerts))
            raised.extend(alerts)
    return raised


__all__ = [
    "MonitorSource",
    "Source",
    "Window",
    "alert_id",
    "alert_payload",
    "channels",
    "evaluate_thresholds",
    "interval_verdict",
    "next_state",
    "observe",
    "raised_event",
]
