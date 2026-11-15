"""One pass of a monitor over its project's new traces (MorningHQ/martian-notes#196).

A monitor attaches scorers, judges and deterministic checks, to a project's live traffic with a
sampling rate, a trace filter and a daily judge token cap (:class:`~martian.evals.records.Monitor`).
Its work is a row of the durable queue, which a :class:`~martian.evals.worker.QueueWorker` takes
each time it is due and gives back for the next pass (:mod:`martian.evals.worker.queue`). One
pass, :func:`execute_monitor`:

- **Reads new traces by time only.** It searches the store for the project's traces whose request
  time is at or after its watermark and before ``now - settle_seconds``, oldest first, with the
  filters the store has (session, user, text, state), and applies the rest (minimum latency, an
  assessment) to each page, as the trace search does. It never reads the project from its start:
  the watermark starts at the monitor's ``active_since``. ``settle_seconds`` leaves a trace that
  long to be recorded after its request time. A pass reads at most ``read_bound`` traces; one
  that stops there says so (``caught_up`` False) and the queue gives it its next pass at once.
- **Samples by trace id.** :func:`sampled` keeps a trace when a number drawn from its id alone is
  below the rate, so a trace is in or out the same way on every pass, and in at every higher rate.
  Traces of eval runs and traces still in progress are not production traffic to score.
- **Scores each sampled trace once.** It takes the trace first (``claim_monitored_trace``, keyed
  by monitor and trace), reads it whole, runs the code scorers itself and the judges through the
  worker's judge, one request per model, and stores every score that has a value on the trace as
  an assessment that names the scorer, its version and the monitor (``martian.monitor_id``). A
  trace a pass took and did not finish is taken over by the next pass, which scores only the
  scorers the trace has no assessment of from this monitor yet.
- **Keeps the daily cap.** Judge tokens are what the judge reports (``JudgeResult.tokens``),
  counted per UTC day. Once the day's count reaches ``max_judge_tokens_per_day``, or a judge
  reports no usage so the cap cannot be kept, the monitor stops scoring until the day ends: the
  traffic until then is not scored, and its progress says so (``capped``).
- **Stops when it is paused or deleted.** The monitor is read again before each sampled trace,
  and taking a trace fails once it is paused or gone.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from martian.evals.model import Score, ScorerVersion, Trace, TraceQuery
from martian.evals.records import Monitor, MonitoredTrace, MonitorFilter, MonitorProgress, RecordStore
from martian.evals.server.projection import TRACE_STATE_BACK
from martian.evals.store import EvalStore

# The package defines these before it imports this module.
from martian.evals.worker import Judge, JudgeRequest, JudgeResult, Work
from martian.evals.worker.execute import JUDGE_ERROR_LENGTH, NO_JUDGE, _judge_metadata, _judged, model_source
from martian.evals.worker.scorers import code_score, scorer_source

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

log = logging.getLogger("martian.evals.worker")

SETTLE_SECONDS = 20.0
"""How long after its request time a trace is first read: the time it has to be recorded."""
READ_BOUND = 1000
"""The most traces one pass reads, like the trace search's 1,000 (docs/trace-store-limits.md)."""
PAGE = 100
MONITOR_KEY = "martian.monitor_id"
"""The score metadata key that names the monitor that stored the score."""
CAP = "max_judge_tokens_per_day"
PASS_FAILED = "The monitor's last pass failed; its next pass goes on from where it stopped"


def _now() -> datetime:
    return datetime.now(UTC)


def sampled(trace_id: str, rate: float) -> bool:
    """Whether a monitor at ``rate`` scores the trace: the first 8 bytes of the SHA-256 of its id,
    as a fraction of 2**64, below ``rate``. It depends on the trace id alone, so the same trace is
    in or out on every pass, and a trace in at one rate is in at every higher rate."""
    if rate >= 1:
        return True
    if rate <= 0:
        return False
    point = int.from_bytes(hashlib.sha256(trace_id.encode("utf-8")).digest()[:8], "big") / 2**64
    return point < rate


def store_query(monitor: Monitor, since: datetime, until: datetime) -> TraceQuery:
    """The store's search for the monitor's traces with request times in ``[since, until)``,
    oldest first, with the filters the store applies."""
    found = monitor.filter
    return TraceQuery(
        project_id=monitor.project_id,
        session_id=found.session_id or None,
        user_id=found.user_id or None,
        text=found.q or None,
        states=[TRACE_STATE_BACK[found.state]] if found.state else [],
        since=since,
        until=until,
        oldest_first=True,
        limit=PAGE,
    )


def matches(found: MonitorFilter) -> Callable[[Trace], bool]:
    """The rest of the filter, which the core applies to the store's pages as the trace search
    does, and what a monitor never scores: a trace still in progress, or one of an eval run."""

    def keep(trace: Trace) -> bool:
        if trace.state == "in_progress" or trace.run_id is not None:
            return False
        if found.min_latency_ms is not None and (trace.latency_ms is None or trace.latency_ms < found.min_latency_ms):
            return False
        if found.assessment:
            wanted = found.assessment_value == "true"
            return any(
                s.scorer == found.assessment and isinstance(s.value, bool) and s.value is wanted for s in trace.scores
            )
        return True

    return keep


def _cap_reached(monitor: Monitor, progress: MonitorProgress, unreported: bool, now: datetime) -> dict[str, Any] | None:
    """What the daily cap stopped, once it is reached, or None."""
    cap = monitor.max_judge_tokens_per_day
    if cap is None or not (unreported or progress.judge_tokens >= cap):
        return None
    until = datetime.combine(now.date() + timedelta(days=1), time(), UTC)
    when = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    if unreported:
        message = (
            f"Stopped until {when}: the judge reported no token usage, so the daily judge token cap "
            f"({CAP} {cap}) cannot be kept; traces until then are not scored"
        )
    else:
        message = (
            f"Stopped until {when} at its daily judge token cap ({CAP} {cap}) after "
            f"{progress.judge_tokens} tokens; traces until then are not scored"
        )
    used = None if unreported else progress.judge_tokens
    return {"limit": CAP, "value": cap, "used": used, "until": when, "message": message}


async def execute_monitor(
    store: EvalStore,
    records: RecordStore,
    work: Work,
    *,
    judge: Judge | None,
    settle_seconds: float = SETTLE_SECONDS,
    read_bound: int = READ_BOUND,
) -> None:
    """One pass of the monitor ``work`` names. A paused or deleted monitor's pass does nothing. A
    pass that fails part way says so in the monitor's progress, and the next pass goes on from its
    watermark."""
    principal, project, monitor_id = work.principal, work.project_id, work.monitor_id or ""
    monitor = await records.get_monitor(principal, project, monitor_id)
    if monitor is None or monitor.state != "active":
        return
    progress = await records.monitor_progress(principal, project, monitor_id) or MonitorProgress()
    try:
        await _pass(store, records, judge, principal, monitor, progress, settle_seconds, read_bound)
    except Exception:  # noqa: BLE001 - a failed pass is the monitor's error, and the next pass goes on
        log.exception("A pass of monitor %s failed", monitor_id)
        latest = await records.monitor_progress(principal, project, monitor_id)
        if latest is not None:
            failed = latest.model_copy(update={"error": PASS_FAILED, "last_pass_at": _now()})
            await records.set_monitor_progress(principal, project, monitor_id, failed)


async def _pass(
    store: EvalStore,
    records: RecordStore,
    judge: Judge | None,
    principal: Principal,
    monitor: Monitor,
    progress: MonitorProgress,
    settle_seconds: float,
    read_bound: int,
) -> None:
    project, pass_id, now = monitor.project_id, uuid.uuid4().hex, _now()

    async def save(**changes: Any) -> MonitorProgress:
        saved = progress.model_copy(update=changes)
        await records.set_monitor_progress(principal, project, monitor.id, saved)
        return saved

    if progress.day != now.date():  # a new day: the cap counts afresh
        today = datetime.combine(now.date(), time(), UTC)
        # The rest of a capped day stays unscored: the new day's tokens are for the new day's traffic.
        watermark = progress.watermark
        if progress.capped is not None and watermark is not None:
            watermark = max(watermark, today)
        progress = progress.model_copy(
            update={"day": now.date(), "judge_tokens": 0, "capped": None, "watermark": watermark}
        )
    until = now - timedelta(seconds=settle_seconds)
    starts = [moment for moment in (progress.watermark, monitor.active_since, monitor.created_at) if moment]
    since = max(starts) if starts else until
    done = {"last_pass_at": now, "error": "", "caught_up": True}
    if progress.capped is not None or since >= until:
        # Capped for the day, the traffic until it ends is passed over unread.
        await save(watermark=max(since, until), **done)
        return
    keep, query = matches(monitor.filter), store_query(monitor, since, until)
    examined, cursor = 0, None
    while True:
        page = await store.search_traces(principal, query.model_copy(update={"cursor": cursor}))
        for trace in page.items:
            if keep(trace) and sampled(trace.id, monitor.sampling_rate):
                current = await records.get_monitor(principal, project, monitor.id)
                if current is None or current.state != "active":
                    await save(last_pass_at=now)
                    return
                if await records.claim_monitored_trace(principal, project, monitor.id, trace.id, pass_id=pass_id):
                    scores, tokens, unreported = await _score(store, records, judge, principal, monitor, trace, pass_id)
                    progress = await save(
                        sampled=progress.sampled + 1,
                        scored=progress.scored + sum(s.value is not None and not s.error for s in scores),
                        errors=progress.errors + sum(s.value is None or bool(s.error) for s in scores),
                        judge_tokens=progress.judge_tokens + tokens,
                        watermark=trace.request_time or progress.watermark,
                    )
                    if (capped := _cap_reached(monitor, progress, unreported, now)) is not None:
                        await save(capped=capped, watermark=until, **done)
                        return
            examined += 1
            progress = progress.model_copy(
                update={"examined": progress.examined + 1, "watermark": trace.request_time or progress.watermark}
            )
        if page.next_cursor is None:
            break
        if examined >= read_bound:  # more to read: the next pass goes on at once
            await save(**{**done, "caught_up": False})
            return
        progress = await save()
        cursor = page.next_cursor
    await save(watermark=until, **done)


async def _score(
    store: EvalStore,
    records: RecordStore,
    judge: Judge | None,
    principal: Principal,
    monitor: Monitor,
    found: Trace,
    pass_id: str,
) -> tuple[list[Score], int, bool]:
    """Score one taken trace with the monitor's scorers that it has no assessment of from this
    monitor yet, store each score with a value on the trace, and record the trace as scored. The
    judges' tokens, and whether a judge reported none."""
    project = monitor.project_id
    trace = await store.get_trace(principal, project, found.id) or found
    done = {s.scorer for s in trace.scores if s.metadata.get(MONITOR_KEY) == monitor.id}
    todo = [scorer for scorer in monitor.scorers if scorer.name not in done]
    by_model: dict[tuple[str | None, str | None], list[ScorerVersion]] = {}
    for scorer in todo:
        if scorer.kind == "llm_judge":
            by_model.setdefault((scorer.model, scorer.endpoint), []).append(scorer)
    answers: dict[tuple[str | None, str | None], JudgeResult | str | None] = {}
    if judge is not None:
        for (model, endpoint), scorers in by_model.items():
            answers[(model, endpoint)] = await _judged(
                judge,
                JudgeRequest(
                    principal=principal,
                    prompt=trace.request,
                    expected="",
                    output=trace.response,
                    scorers=tuple(scorers),
                    model=model,
                    endpoint=endpoint,
                    job_id=monitor.id,
                ),
                f"trace {trace.id} of monitor {monitor.id}",
            )
    tokens, unreported = 0, False
    for answer in answers.values():
        if isinstance(answer, JudgeResult):  # a judge that failed reported nothing to count
            if answer.tokens is None:
                unreported = True
            else:
                tokens += answer.tokens
    scores = []
    for scorer in todo:
        value: Any = None
        rationale = error = ""
        source_id = scorer_source(scorer)
        metadata: dict[str, Any] = {MONITOR_KEY: monitor.id}
        answer = answers.get((scorer.model, scorer.endpoint))
        if scorer.kind != "llm_judge":
            value, rationale = code_score(scorer.name, trace.response, "")
        else:
            metadata |= _judge_metadata(scorer, answer if isinstance(answer, JudgeResult) else None)
            if isinstance(answer, str):
                error = answer
            elif answer is not None and scorer.name in answer.verdicts:
                verdict = answer.verdicts[scorer.name]
                value, rationale, source_id = verdict.value, verdict.rationale, model_source(answer)
            elif answer is not None and scorer.name in answer.errors:
                error = answer.errors[scorer.name][:JUDGE_ERROR_LENGTH] or NO_JUDGE
            else:
                error = NO_JUDGE
        score = Score(
            trace_id=trace.id,
            scorer=scorer.name,
            scorer_version=scorer.version,
            source="code" if scorer.kind != "llm_judge" else "llm_judge",
            source_id=source_id,
            value=value,
            passed=value if isinstance(value, bool) else None,
            rationale=rationale,
            error=error,
            metadata=metadata,
            created_at=_now(),
        )
        if value is not None:
            logged = await store.log_score(principal, project, trace.id, score)
            score = score.model_copy(update={"store": logged.store})
        scores.append(score)
    record = MonitoredTrace(
        monitor_id=monitor.id,
        trace_id=trace.id,
        state="scored",
        pass_id=pass_id,
        tokens=None if unreported else tokens,
        scores=scores,
        finished_at=_now(),
    )
    await records.finish_monitored_trace(principal, project, record)
    return scores, tokens, unreported


__all__ = [
    "MONITOR_KEY",
    "PASS_FAILED",
    "READ_BOUND",
    "SETTLE_SECONDS",
    "execute_monitor",
    "matches",
    "sampled",
    "store_query",
]
