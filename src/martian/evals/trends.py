"""Score trends of a monitor's scorers (MartianSecurity/martian-notes#199): each scorer's pass rate or
mean in consecutive windows of time, each with its 95% interval, for the Monitoring page.

A trend reads what a threshold reads: the monitor's own record of the traces it scored
(:meth:`~martian.evals.records.RecordStore.scored_monitored_traces`), by the time it took them,
newest first, at most :data:`~martian.evals.records.THRESHOLD_WINDOW_BOUND` of them. It never
reads the trace store or a project's traces. A point is measured as a threshold measures its
window (:func:`martian.evals.thresholds.measured`): Wilson for a pass rate, Student t for a mean,
with the traces independent.

The windows are aligned to whole multiples of their width since the Unix epoch, in UTC, so a point
covers the same hour or day on every read; the newest ends now. When the read reached its bound,
the oldest windows hold only part of their scores, and each says so (``truncated``).
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from martian.evals import stats
from martian.evals.model import Value
from martian.evals.records import THRESHOLD_WINDOW_BOUND, MonitoredTrace
from martian.evals.thresholds import measured

Measure = Literal["pass_rate", "mean"]

TREND_POINTS = 200
"""The most windows a trend has, per scorer."""


def trend_windows(now: datetime, span_seconds: int, bucket_seconds: int) -> list[tuple[datetime, datetime]]:
    """The windows that cover the last ``span_seconds`` before ``now``, each ``bucket_seconds``
    wide and starting on a whole multiple of it since the epoch; the newest ends at ``now``."""
    if bucket_seconds < 1 or span_seconds < 1:
        raise ValueError("A trend's span and windows are at least a second")
    count = math.ceil(span_seconds / bucket_seconds)
    if count > TREND_POINTS:
        raise ValueError(f"A trend has at most {TREND_POINTS} points")
    newest = math.floor(now.timestamp() / bucket_seconds) * bucket_seconds
    starts = [datetime.fromtimestamp(newest - bucket_seconds * n, UTC) for n in reversed(range(count))]
    width = timedelta(seconds=bucket_seconds)
    return [(start, min(start + width, now)) for start in starts]


def monitor_trends(
    rows: Sequence[MonitoredTrace],
    measures: Mapping[str, Measure | None],
    windows: Sequence[tuple[datetime, datetime]],
) -> dict[str, Any]:
    """Each scorer's points over ``windows`` from ``rows``, the monitor's scored traces newest
    first as :meth:`scored_monitored_traces` reads them. ``measures`` names each scorer's measure,
    or None for one with neither, whose points carry only their count. A read that reached
    :data:`THRESHOLD_WINDOW_BOUND` cut the oldest windows short: ``complete_since`` is the time
    the points are whole from, and each point before it is ``truncated``."""
    cut = rows[-1].created_at if len(rows) >= THRESHOLD_WINDOW_BOUND else None
    starts = [start for start, _ in windows]
    found: dict[tuple[str, int], list[Value]] = defaultdict(list)
    for row in rows:
        if row.created_at is None:
            continue
        index = bisect_right(starts, row.created_at) - 1
        if index < 0 or row.created_at >= windows[index][1]:
            continue
        for score in row.scores:
            if score.scorer in measures and score.value is not None and not score.error:
                found[score.scorer, index].append(score.value)
    scorers = []
    for name, measure in measures.items():
        points = []
        for index, (start, end) in enumerate(windows):
            values = found.get((name, index), [])
            if measure is None:
                count, estimate, interval = len(values), None, None
            else:
                count, estimate, interval = measured(measure, values)
            points.append(
                {
                    "start": start,
                    "end": end,
                    "count": count,
                    "estimate": estimate,
                    "interval": None if interval is None else [interval[0], interval[1]],
                    "truncated": cut is not None and start <= cut,
                }
            )
        scorers.append({"name": name, "measure": measure, "points": points})
    return {
        "since": windows[0][0] if windows else None,
        "until": windows[-1][1] if windows else None,
        "confidence": stats.CONFIDENCE,
        "read": len(rows),
        "truncated": cut is not None,
        "complete_since": cut,
        "scorers": scorers,
    }


__all__ = ["TREND_POINTS", "Measure", "monitor_trends", "trend_windows"]
