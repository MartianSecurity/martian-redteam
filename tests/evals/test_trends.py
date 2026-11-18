"""Score trends of a monitor's scorers (MartianSecurity/martian-notes#199).

``GET /v1/evals/monitors/{id}/trends`` gives each scorer's pass rate or mean in windows aligned to
their width, each with the 95% interval a threshold would compute over the same scores. It reads
the monitor's record of its scored traces by time, from the oldest window's start, bounded as a
threshold's window is, and never the project's traces; a read cut short by its bound says which
windows are partial. Nothing calls a model.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import martian.evals.server.monitors as monitors_module
from martian.evals import stats
from martian.evals.model import Score, ScorerVersion
from martian.evals.records import THRESHOLD_WINDOW_BOUND, Monitor, MonitoredTrace
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import COMMUNITY, DEFAULT_PROJECT, ENTERPRISE, SINGLE_USER, create_app
from martian.evals.server.monitors import scorer_measure
from martian.evals.store import MemoryStore
from martian.evals.trends import TREND_POINTS, monitor_trends, trend_windows
from martian.evals.worker.scorers import builtin_scorer

NOW = datetime(2026, 11, 15, 10, 42, 10, tzinfo=UTC)
HOUR = timedelta(hours=1)
TONE = ScorerVersion(name="tone", kind="llm_judge", type="registered", version="1", criteria={"kind": "categorical"})


class CountingRecords(SqlRecordStore):
    """The record store, keeping what each read of a monitor's scored traces asked for and refusing
    the reads a trend must never make: a monitor's whole record of its traces."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.reads: list[dict[str, Any]] = []

    async def scored_monitored_traces(self, principal: Any, project_id: str, monitor_id: str, **window: Any) -> Any:
        self.reads.append(window)
        return await super().scored_monitored_traces(principal, project_id, monitor_id, **window)

    async def list_monitored_traces(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a trend reads scored traces by time only")


@pytest.fixture
def records(tmp_path: Path) -> Iterator[CountingRecords]:
    found = CountingRecords(f"sqlite:///{tmp_path / 'martian.db'}")
    yield found
    found.close()


def taken(monitor_id: str, number: int, at: datetime, **values: Any) -> MonitoredTrace:
    """A trace the monitor scored at ``at``, with one score per scorer; a value of ``"error"`` is a
    score without a verdict."""
    scores = [
        Score(trace_id=f"tr-{number}", scorer=name, source="llm_judge", error="Timed out")
        if value == "error"
        else Score(trace_id=f"tr-{number}", scorer=name, source="llm_judge", value=value)
        for name, value in values.items()
    ]
    return MonitoredTrace(
        monitor_id=monitor_id, trace_id=f"tr-{number}", state="scored", pass_id="p", scores=scores, created_at=at
    )


def test_windows_are_aligned_to_their_width_and_the_newest_ends_now() -> None:
    windows = trend_windows(NOW, 6 * 3600, 3600)
    assert [start.hour for start, _ in windows] == [5, 6, 7, 8, 9, 10]
    assert all(end - start == HOUR for start, end in windows[:-1])
    assert windows[-1] == (datetime(2026, 11, 15, 10, tzinfo=UTC), NOW)
    # A day of windows a day wide starts at midnight UTC, whenever it is read.
    assert trend_windows(NOW, 86400, 86400) == [(datetime(2026, 11, 15, tzinfo=UTC), NOW)]
    assert len(trend_windows(NOW, TREND_POINTS * 60, 60)) == TREND_POINTS
    with pytest.raises(ValueError, match="at most 200 points"):
        trend_windows(NOW, TREND_POINTS * 60 + 1, 60)


def test_each_window_has_the_pass_rate_or_mean_and_interval_a_threshold_would_compute() -> None:
    windows = trend_windows(NOW, 3 * 3600, 3600)  # 08:00, 09:00, 10:00 to now
    nine = datetime(2026, 11, 15, 9, tzinfo=UTC)
    rows = [
        # Newest first, as the record store reads them.
        taken("m", 9, NOW - timedelta(minutes=1), safety=True, response_length=40, tone="warm"),
        *[taken("m", n, nine + timedelta(minutes=n), safety=n < 6, response_length=10 * n) for n in range(8, 0, -1)],
        taken("m", 0, nine, safety="error", response_length="error"),
    ]
    found = monitor_trends(rows, {"safety": "pass_rate", "response_length": "mean", "tone": None}, windows)
    assert (found["read"], found["truncated"], found["complete_since"]) == (10, False, None)
    assert found["since"] == windows[0][0] and found["until"] == NOW
    safety, length, tone = found["scorers"]
    assert [p["count"] for p in safety["points"]] == [0, 8, 1]
    point = safety["points"][1]
    assert (point["start"], point["end"], point["estimate"]) == (nine, nine + HOUR, 5 / 8)
    assert point["interval"] == list(stats.wilson(5, 8) or ())
    # An empty window has no estimate; a score without a verdict counts for nothing.
    assert safety["points"][0] == {**safety["points"][0], "count": 0, "estimate": None, "interval": None}
    values = [10.0 * n for n in range(1, 9)]
    assert length["points"][1]["estimate"] == pytest.approx(45)
    assert length["points"][1]["interval"] == list(stats.mean_interval(values) or ())
    assert length["points"][2]["interval"] is None  # one value has no t interval
    assert tone["measure"] is None and [p["count"] for p in tone["points"]] == [0, 0, 1]
    assert tone["points"][2]["estimate"] is None


def test_a_read_cut_at_its_bound_marks_the_windows_it_cut_short() -> None:
    windows = trend_windows(NOW, 3 * 3600, 3600)
    oldest = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)
    rows = [taken("m", n, oldest, safety=True) for n in range(THRESHOLD_WINDOW_BOUND)]
    found = monitor_trends(rows, {"safety": "pass_rate"}, windows)
    assert (found["truncated"], found["complete_since"]) == (True, oldest)
    assert [p["truncated"] for p in found["scorers"][0]["points"]] == [True, True, False]


def test_scorers_are_measured_as_their_thresholds_are() -> None:
    assert scorer_measure(builtin_scorer("safety")) == "pass_rate"
    assert scorer_measure(builtin_scorer("valid_json")) == "pass_rate"
    assert scorer_measure(builtin_scorer("response_length")) == "mean"
    numeric = TONE.model_copy(update={"criteria": {"kind": "numeric"}})
    assert scorer_measure(TONE.model_copy(update={"criteria": {}})) == "pass_rate"
    assert (scorer_measure(numeric), scorer_measure(TONE)) == ("mean", None)


def make_app(records: CountingRecords, edition: Any = ENTERPRISE) -> TestClient:
    app = create_app(
        store=MemoryStore(), records=records, principal=lambda: SINGLE_USER, entitlements=lambda _p: edition
    )
    return TestClient(app)


def test_the_route_reads_the_monitors_scores_by_time_from_its_oldest_window(
    records: CountingRecords, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitors_module, "_now", lambda: NOW)
    monitor_id = str(uuid.uuid4())
    scorers = [builtin_scorer("safety"), builtin_scorer("response_length")]
    with make_app(records) as client:
        assert client.portal is not None
        monitor = Monitor(id=monitor_id, project_id=DEFAULT_PROJECT, name="Live", scorers=scorers, sampling_rate=1)
        client.portal.call(records.create_monitor, SINGLE_USER, DEFAULT_PROJECT, monitor)
        # Scored a day and a half ago, outside the default day, then twice an hour for the last 3.
        times = [NOW - timedelta(hours=36)] + [NOW - timedelta(minutes=30 * n) for n in range(6, 0, -1)]
        for number, at in enumerate(times):
            row = taken(monitor_id, number, at, safety=number % 2 == 0, response_length=number)
            client.portal.call(records.finish_monitored_trace, SINGLE_USER, DEFAULT_PROJECT, row)

        answer = client.get(f"/v1/evals/monitors/{monitor_id}/trends")
        assert answer.status_code == 200, answer.text
        found = answer.json()
        assert (found["monitor_id"], found["bucket_seconds"], found["read"], found["confidence"]) == (
            monitor_id,
            3600,
            6,
            0.95,
        )
        # The one read: by time, from the oldest window's start to now, at most the bound.
        [read] = records.reads
        assert read == {"since": datetime(2026, 11, 14, 11, tzinfo=UTC), "until": NOW, "limit": THRESHOLD_WINDOW_BOUND}
        safety, length = found["scorers"]
        assert (safety["name"], safety["measure"], length["measure"]) == ("safety", "pass_rate", "mean")
        assert len(safety["points"]) == 24
        assert sum(p["count"] for p in safety["points"]) == 6  # the trace of 36 hours ago is not read
        assert [p["count"] for p in safety["points"][-4:]] == [1, 2, 2, 1]

        week = client.get(
            f"/v1/evals/monitors/{monitor_id}/trends", params={"span_seconds": 7 * 86400, "bucket_seconds": 86400}
        )
        assert week.status_code == 200, week.text
        assert sum(p["count"] for p in week.json()["scorers"][0]["points"]) == 7
        assert records.reads[-1]["since"] == datetime(2026, 11, 9, tzinfo=UTC)

        too_many = client.get(
            f"/v1/evals/monitors/{monitor_id}/trends", params={"span_seconds": 86400, "bucket_seconds": 60}
        )
        assert too_many.status_code == 422 and "at most 200 points" in too_many.json()["detail"]
        assert client.get(f"/v1/evals/monitors/{uuid.uuid4()}/trends").status_code == 404


def test_the_route_needs_monitoring(records: CountingRecords) -> None:
    with make_app(records, COMMUNITY) as client:
        answer = client.get(f"/v1/evals/monitors/{uuid.uuid4()}/trends")
        assert answer.status_code == 403
        assert answer.json()["detail"] == "The Community edition does not include monitoring"
