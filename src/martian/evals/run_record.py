"""A suite-file run, recorded in the local edition's data folder so that ``martian ui`` shows it.

``martian evals run`` writes each run through the evals core's store and record store
(:func:`martian.evals.local.open_stores`), as the single local user, in the default project:

- The run is a ``Run`` of source ``local`` with the suite's id, digest, target, scorers and dataset
  digest, and its limits in ``provenance.limits``. Its code is the suite's name and its number
  among that suite's runs, such as ``evals-3``. It is ``running`` while cases are graded and
  ``completed`` once every case has a result, whether or not each has a verdict. A run that
  reached one of its limits is ``stopped``, with the limit in its ``error`` and
  ``provenance.stopped``; one cancelled in ``martian ui``, or interrupted, is ``cancelled``; one
  that stops on an error is ``failed``.
- Each case graded in the run is a ``CaseResult`` with a ``Score`` per check, mapped from its
  ``results.jsonl`` record by :func:`~martian.evals.model.from_results_record`, and a trace: the one
  MLflow's evaluation harness recorded for the case, with its spans, copied into the store under a
  new id and carrying the scores.
- A case whose verdict is reused from an earlier run in the same output folder keeps that verdict.
  Its result in this run has no trace: the trace stays with the run that graded the case.
- When the suite runs each case more than once, the run's ``provenance`` holds the repeats under
  ``repeats``: the attempts per case and each check's rule. A case result holds every attempt, and
  each attempt's trace is recorded with that attempt's verdicts. The case result's trace is its
  representative attempt's (:func:`martian.evals.repeats.representative`). Its scores are the
  attempts combined, which no one trace carries, so they name no trace.
- When the run has conversation cases, the run's ``provenance`` holds how many under
  ``conversations``. A conversation case's result holds its session id and every turn it sent.
  Each turn's trace is recorded with that turn's verdicts and the conversation's session id, so
  the turns group into one session; with repeats each attempt's turns are, under that attempt's
  own session id. The
  case result's trace is that of its first turn whose verdict is the conversation's, else of its
  first turn, its latency the turns' latencies added up. Its scores are the conversation's
  verdicts, which no one trace carries, so they name no trace.
- A run with simulated users says which simulator played them: the run's ``provenance`` holds its
  model, inference parameters and version under ``simulator``, and the run's scorers include the
  goal verdict (``goal_reached``), a model-judged scorer of the conversation whose model is the
  simulator's, so each goal score names that model. A simulated case's result is marked
  ``simulated``, and each of its turns' traces is tagged ``martian.simulated``.
- A run that stops at a limit records only the cases whose attempts all ran, and a
  conversation case only when every turn of every attempt ran.
- A suite with a matrix records one run per combination (:mod:`martian.evals.matrix`). Its
  ``provenance`` holds the combination under ``matrix`` (:func:`martian.evals.matrix.provenance`),
  and its code is the suite's name with the combination's id in brackets and its number among
  that combination's runs, such as ``evals[model=large,prompt=terse]-2``. Each of the
  combination's text values that is a registered prompt version's text, by content digest, is
  named under ``prompts`` (:func:`martian.evals.prompts.matched_prompts`), with the prompt's name
  and version, so the run shows the prompt version it used.
- A run with the cache of model calls on (:mod:`martian.evals.cache`) has it in ``provenance``
  under ``cache``: its folder, time to live and whether function targets are cached from the
  start, and once the run ends how many target and judge replies came from the cache, so a
  replayed result is never taken for a fresh one.

Results are written after each batch, so a run shows in the UI while it runs. The output folder's
files are written as before, and nothing here reads them.

The process holds the run in the data folder's queue (:class:`~martian.evals.local_runs.RunHold`)
from before the run is recorded until it ends, so ``martian ui`` leaves the run alone while it
runs. Cancelling it there records the request on the run, which :meth:`RunRecorder.watch_cancel`
reads every second, so the run stops before its next case; every write of the run here keeps the
request. A run whose process was killed, so that it could not record its end, is finished as
interrupted once its hold's lease runs out, by a ``martian ui`` serving the folder, or at once when
the run is cancelled there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from martian._version import __version__
from martian.config import ConfigError
from martian.evals.model import (
    CaseResult,
    CaseStatus,
    CheckVerdict,
    Counts,
    Run,
    RunLimits,
    RunState,
    Score,
    ScorerVersion,
    Simulator,
    StoreRef,
    Suite,
    Trace,
    TurnResult,
    from_results_record,
)
from martian.evals.prompts import PROVENANCE_KEY, matched_prompts
from martian.evals.repeats import representative
from martian.evals.server.principal import SINGLE_USER
from martian.evals.server.projection import DEFAULT_PROJECT
from martian.evals.simulation import GOAL, SIMULATOR_VERSION

if TYPE_CHECKING:
    from martian.evals.local_runs import RunHold

log = logging.getLogger("martian.evals.runner")

CANCEL_POLL_SECONDS = 1.0
"""How often a run reads its record for a cancel request made in ``martian ui``."""


@dataclass(frozen=True)
class GradedCase:
    """One case graded in this run: its position among the run's cases, its ``results.jsonl``
    record, its own expectations, and the JSON of the trace MLflow recorded for it, if any. A
    case run more than once has the JSON of each attempt's trace in ``attempt_traces`` instead,
    in the order of its record's ``attempts``. A conversation case has the JSON of its turns'
    traces in ``turn_traces`` instead, one tuple per attempt (one without repeats) in the order of
    its turns, and each turn's expectations in ``turn_expectations``."""

    position: int
    record: dict[str, Any]
    expectations: dict[str, Any]
    trace: str | None = None
    attempt_traces: tuple[str | None, ...] = ()
    turn_traces: tuple[tuple[str | None, ...], ...] = ()
    turn_expectations: tuple[dict[str, Any], ...] = ()


def _wait[T](work: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(work)


class RunRecorder:
    """Records one run of ``suite`` in the data folder ``home``, as a context manager:
    :meth:`start`, :meth:`graded` for each batch, then :meth:`finish`. Open it before the run
    changes anything in its output folder, so a data folder that cannot be opened stops the run
    first."""

    def __init__(
        self,
        home: Path,
        suite: Suite,
        *,
        limits: RunLimits | None = None,
        matrix: dict[str, Any] | None = None,
        cache: dict[str, Any] | None = None,
    ) -> None:
        """``limits`` are the run's (``provenance.limits`` when it has any), and ``matrix`` the run's
        combination of the suite's matrix, as ``provenance.matrix`` holds it, for a suite with a
        matrix. ``cache`` is the cache of model calls the run uses, as ``provenance.cache`` holds it
        from the start; :meth:`used_cache` gives its counts, which ending the run adds."""
        from martian.evals.local import LocalEditionError, open_stores

        self._hold: RunHold | None = None
        self._limits = limits or RunLimits()
        self._watching = threading.Event()  # set to stop the cancel watch
        self._watch: threading.Thread | None = None
        try:
            self.store, self.records = open_stores(home)
        except LocalEditionError as error:
            raise ConfigError(str(error)) from None
        except Exception as error:  # noqa: BLE001 - a folder may fail to open in many ways; say which
            raise ConfigError(f"could not open the local data folder {home}: {type(error).__name__}") from None
        self.home = home
        self.suite = suite
        self.matrix = matrix
        self.cache = cache
        self._cache_used: dict[str, Any] | None = None
        self.run: Run | None = None
        self._ended = False
        self._scorers = {scorer.name: scorer for scorer in suite.scorers}
        self._statuses: dict[str, CaseStatus] = {}

    # --- the run -----------------------------------------------------------------------------

    def start(
        self,
        *,
        planned: int,
        total: int,
        limit: int | None,
        reused: list[GradedCase],
        repeats: dict[str, Any] | None = None,
        conversations: int = 0,
        simulator: Simulator | None = None,
    ) -> Run:
        """Record that the run started over ``planned`` of the dataset's ``total`` cases, with the
        results it reuses from earlier runs, with ``repeats``, the suite's repeat plan, when it
        runs each case more than once, with ``conversations``, how many of the planned cases are
        conversations, and with ``simulator``, the model that plays its simulated users, when it
        has any."""
        if simulator is not None:
            # The goal verdict is the simulator's judgment of each simulated conversation.
            self._scorers[GOAL] = ScorerVersion(
                name=GOAL,
                kind="llm_judge",
                type="simulated_user",
                version=SIMULATOR_VERSION,
                model=simulator.model,
                parameters=dict(simulator.parameters),
                scope="conversation",
            )
        return _wait(
            self._start(
                planned=planned,
                total=total,
                limit=limit,
                reused=reused,
                repeats=repeats,
                conversations=conversations,
                simulator=simulator,
            )
        )

    async def _start(
        self,
        *,
        planned: int,
        total: int,
        limit: int | None,
        reused: list[GradedCase],
        repeats: dict[str, Any] | None,
        conversations: int,
        simulator: Simulator | None,
    ) -> Run:
        from martian.evals.local_runs import RunHold

        now = datetime.now(UTC)
        suite = self.suite
        provenance: dict[str, Any] = {
            "suite": {"name": suite.name, "digest": suite.digest},
            "dataset": {"name": suite.dataset_name, "digest": suite.dataset_digest},
            "sdk_version": __version__,
        }
        if repeats is not None:
            provenance["repeats"] = repeats
        if conversations:
            provenance["conversations"] = conversations
        if self._limits != RunLimits():
            provenance["limits"] = self._limits.model_dump(mode="json")
        if self.matrix is not None:
            provenance["matrix"] = self.matrix
            # The combination's text values that are registered prompt versions, by content digest.
            used = await matched_prompts(self.store, SINGLE_USER, suite.matrix, self.matrix["values"])
            if used:
                provenance[PROVENANCE_KEY] = used
        if self.cache is not None:
            provenance["cache"] = self.cache
        if simulator is not None:
            provenance["simulator"] = {**simulator.model_dump(mode="json"), "version": SIMULATOR_VERSION}
        run = Run(
            id=str(uuid.uuid4()),
            project_id=DEFAULT_PROJECT,
            code=await self._code(),
            source="local",
            state="running",
            suite_id=suite.id,
            suite_digest=suite.digest,
            dataset_name=suite.dataset_name or "",
            dataset_digest=suite.dataset_digest,
            target=suite.target,
            scorers=list(self._scorers.values()),
            counts=Counts(cases=planned),
            max_cases=limit or None,
            truncated=planned < total,
            provenance=provenance,
            requested_by=SINGLE_USER.user_id,
            created_at=now,
            started_at=now,
        )
        # Held before the run is recorded, so no process ever finds it running and not held.
        self._hold = await RunHold.take(self.home, run.id)
        run = await self.store.start_run(SINGLE_USER, run)
        self.run = await self.records.create_run(SINGLE_USER, run)
        await self._write(reused, traces=False)
        return self.run

    async def _code(self) -> str:
        """The suite's name and its number among the suite's runs in the project; for a
        combination of its matrix, the name with the combination's id in brackets and its number
        among that combination's runs."""
        combination = None if self.matrix is None else self.matrix["id"]
        name = self.suite.name if combination is None else f"{self.suite.name}[{combination}]"
        count, cursor = 0, None
        while True:
            page = await self.records.list_runs(SINGLE_USER, DEFAULT_PROJECT, limit=500, cursor=cursor)
            count += sum(
                run.suite_id == self.suite.id and (run.provenance.get("matrix") or {}).get("id") == combination
                for run in page.items
            )
            if (cursor := page.next_cursor) is None:
                return f"{name}-{count + 1}"

    def graded(self, cases: list[GradedCase]) -> None:
        """Record a batch of graded cases, with their traces, and the run's counts so far."""
        _wait(self._write(cases, traces=True))

    def finish(self) -> Run:
        """Record that every case has a result."""
        return _wait(self._end("completed", ""))

    def used_cache(self, report: dict[str, Any]) -> None:
        """Keep the run's use of the cache of model calls, with its counts, for ``provenance.cache``
        when the run ends, whether it finished, stopped at a limit or was cancelled."""
        self._cache_used = report

    def stop(self, stopped: dict[str, Any]) -> Run:
        """Record that the run reached one of its limits before its last case (``RunLimits.stop``):
        ``stopped``, with the limit in ``error`` and ``provenance.stopped``."""
        return _wait(self._end("stopped", str(stopped["message"]), stopped=stopped))

    def cancel(self) -> Run:
        """Record that the run stopped before its last case because it was cancelled."""
        return _wait(self._end("cancelled", ""))

    def watch_cancel(self) -> threading.Event:
        """An event set once someone asks, in ``martian ui``, to stop the run: a thread reads the
        run's record every :data:`CANCEL_POLL_SECONDS` until the run ends."""
        asked = threading.Event()
        assert self.run is not None
        run = self.run

        def watch() -> None:
            while not self._watching.wait(CANCEL_POLL_SECONDS):
                try:
                    found = asyncio.run(self.records.get_run(SINGLE_USER, run.project_id, run.id))
                except Exception:  # noqa: BLE001 - the database is busy; read again at the next poll
                    log.exception("Could not read run %s for a cancel request", run.code)
                    continue
                if found is not None and found.cancel_requested_at is not None:
                    asked.set()
                    return

        self._watch = threading.Thread(target=watch, name="martian-run-cancel-watch", daemon=True)
        self._watch.start()
        return asked

    def __enter__(self) -> RunRecorder:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, traceback: Any) -> None:
        """A run that started and stops on an error is ``failed``, or ``cancelled`` when it was
        interrupted. The run is released and the stores are closed either way."""
        try:
            self._end_watch()
            if error is not None and self.run is not None and not self._ended:
                if isinstance(error, KeyboardInterrupt):
                    _wait(self._end("cancelled", ""))
                else:
                    _wait(self._end("failed", f"the run stopped early: {type(error).__name__}"))
        finally:
            if self._hold is not None:
                self._hold.release()
            self.records.close()

    def _end_watch(self) -> None:
        self._watching.set()
        if self._watch is not None:
            self._watch.join()

    async def _current(self) -> Run:
        """The run as its record holds it now, so a write keeps a cancel request made meanwhile."""
        assert self.run is not None
        found = await self.records.get_run(SINGLE_USER, self.run.project_id, self.run.id)
        return found if found is not None else self.run

    async def _end(self, state: RunState, error: str, *, stopped: dict[str, Any] | None = None) -> Run:
        assert self.run is not None
        self._end_watch()
        self._ended = True
        counts = self._counts()
        current = await self._current()
        changes: dict[str, Any] = {"state": state, "counts": counts, "error": error, "finished_at": datetime.now(UTC)}
        provenance = dict(current.provenance)
        if stopped is not None:
            provenance["stopped"] = stopped
        if self._cache_used is not None:
            provenance["cache"] = self._cache_used
        if provenance != current.provenance:
            changes["provenance"] = provenance
        finished = current.model_copy(update=changes)
        self.run = await self.records.update_run(SINGLE_USER, finished, expected_state="running")
        await self.store.finish_run(SINGLE_USER, DEFAULT_PROJECT, finished.id, state=state, counts=counts)
        return self.run

    def _counts(self) -> Counts:
        assert self.run is not None
        statuses = list(self._statuses.values())
        return Counts(
            cases=self.run.counts.cases,
            passed=statuses.count("passed"),
            failed=statuses.count("failed"),
            errors=statuses.count("error"),
        )

    # --- case results, scores and traces -----------------------------------------------------

    async def _write(self, cases: list[GradedCase], *, traces: bool) -> None:
        assert self.run is not None
        results: list[CaseResult] = []
        scores: list[Score] = []
        traced: list[str] = []
        for case in cases:
            result, case_scores = self._result(case)
            if case.turn_expectations:
                result = self._with_turn_expectations(case, result)
            if traces and case.turn_traces:
                result, turn_ids = await self._record_conversation(case, result)
                traced += turn_ids
            elif traces and case.attempt_traces:
                result, attempt_ids = await self._record_attempts(case, result)
                traced += attempt_ids
            trace = self._trace(case, result, case_scores) if traces and case.trace is not None else None
            if trace is not None:
                saved = await self.store.record_trace(SINGLE_USER, trace)
                stored = {score.scorer: score.store for score in saved.scores}
                case_scores = [
                    score.model_copy(update={"trace_id": saved.id, "store": stored.get(score.scorer, StoreRef())})
                    for score in case_scores
                ]
                ended = (
                    trace.request_time + timedelta(milliseconds=trace.latency_ms or 0) if trace.request_time else None
                )
                result = result.model_copy(
                    update={
                        "latency_ms": trace.latency_ms,
                        "finished_at": ended or result.finished_at,
                        "store": StoreRef(trace_id=saved.id),
                    }
                )
                traced.append(saved.id)
            results.append(result)
            scores += case_scores
            self._statuses[result.case_id] = result.status
        if traced:
            await self.store.link_traces(SINGLE_USER, DEFAULT_PROJECT, self.run.id, traced)
        if results:  # each result with its scores, in one transaction
            await self.records.put_results(SINGLE_USER, DEFAULT_PROJECT, results, scores=scores)
        current = await self._current()
        self.run = await self.records.update_run(
            SINGLE_USER, current.model_copy(update={"counts": self._counts()}), expected_state="running"
        )

    def _result(self, case: GradedCase) -> tuple[CaseResult, list[Score]]:
        assert self.run is not None
        result, scores = from_results_record(
            case.record, run_id=self.run.id, position=case.position, scorers=self._scorers
        )
        now = datetime.now(UTC)
        result = result.model_copy(update={"expectations": dict(case.expectations), "finished_at": now})
        judged = [
            score.model_copy(update={"source_id": scorer.model or "", "created_at": now})
            if (scorer := self._scorers.get(score.scorer)) is not None and scorer.kind == "llm_judge"
            else score.model_copy(update={"created_at": now})
            for score in scores
        ]
        return result, judged

    async def _record_attempts(self, case: GradedCase, result: CaseResult) -> tuple[CaseResult, list[str]]:
        """Record each attempt's trace with that attempt's verdicts, and give the case result the
        trace and latency of its representative attempt. The case's own scores, the attempts
        combined, are not on any trace."""
        assert self.run is not None
        attempts = list(result.attempts)
        recorded: list[str] = []
        for position, (attempt, found) in enumerate(zip(attempts, case.attempt_traces, strict=True)):
            if found is None:
                continue
            scores = [self._attempt_score(result, label, verdict) for label, verdict in attempt.checks.items()]
            trace = self._as_run_trace(
                found,
                result,
                name=f"{self.run.code} {result.case_id} attempt {attempt.number}",
                output=attempt.output,
                error=attempt.error,
                scores=scores,
                tags={"martian.case_id": result.case_id, "martian.attempt": str(attempt.number)},
            )
            saved = await self.store.record_trace(SINGLE_USER, trace)
            attempts[position] = attempt.model_copy(update={"trace_id": saved.id, "latency_ms": trace.latency_ms})
            recorded.append(saved.id)
        verdict = {"passed": True, "failed": False}
        chosen = attempts[representative([verdict.get(a.status) for a in attempts], verdict.get(result.status))]
        update: dict[str, Any] = {"attempts": attempts, "latency_ms": chosen.latency_ms}
        if chosen.trace_id is not None:
            update["store"] = StoreRef(trace_id=chosen.trace_id)
        return result.model_copy(update=update), recorded

    def _with_turn_expectations(self, case: GradedCase, result: CaseResult) -> CaseResult:
        """The result with each turn's own expectations, which ``results.jsonl`` does not hold."""

        def expected(turns: list[TurnResult]) -> list[TurnResult]:
            return [
                turn.model_copy(update={"expectations": dict(case.turn_expectations[turn.number - 1])})
                for turn in turns
            ]

        attempts = [attempt.model_copy(update={"turns": expected(attempt.turns)}) for attempt in result.attempts]
        return result.model_copy(update={"turns": expected(result.turns), "attempts": attempts})

    async def _record_conversation(self, case: GradedCase, result: CaseResult) -> tuple[CaseResult, list[str]]:
        """Record each turn's trace under its conversation's session id, and give the case result,
        and each attempt with repeats, the trace of the turn that stands for it and the turns'
        latency. The conversation's own scores are not on any trace."""
        if not result.attempts:
            turns, recorded = await self._record_turns(case.turn_traces[0], result, result.turns, result.session_id)
            trace_id, latency = self._standing(turns, result.status)
            update: dict[str, Any] = {"turns": turns, "latency_ms": latency}
            if trace_id is not None:
                update["store"] = StoreRef(trace_id=trace_id)
            return result.model_copy(update=update), recorded
        attempts = list(result.attempts)
        recorded = []
        for position, (attempt, found) in enumerate(zip(attempts, case.turn_traces, strict=True)):
            turns, ids = await self._record_turns(found, result, attempt.turns, attempt.session_id, attempt.number)
            recorded += ids
            trace_id, latency = self._standing(turns, attempt.status)
            attempts[position] = attempt.model_copy(
                update={"turns": turns, "trace_id": trace_id, "latency_ms": latency}
            )
        verdict = {"passed": True, "failed": False}
        chosen = attempts[representative([verdict.get(a.status) for a in attempts], verdict.get(result.status))]
        update = {"attempts": attempts, "turns": chosen.turns, "latency_ms": chosen.latency_ms}
        if chosen.trace_id is not None:
            update["store"] = StoreRef(trace_id=chosen.trace_id)
        return result.model_copy(update=update), recorded

    async def _record_turns(
        self,
        found: tuple[str | None, ...],
        result: CaseResult,
        turns: list[TurnResult],
        session_id: str | None,
        attempt: int | None = None,
    ) -> tuple[list[TurnResult], list[str]]:
        """Record one conversation's turns' traces, each with that turn's verdicts."""
        assert self.run is not None
        written = list(turns)
        recorded: list[str] = []
        prefix = f"{self.run.code} {result.case_id}" + (f" attempt {attempt}" if attempt is not None else "")
        for position, (turn, trace_json) in enumerate(zip(turns, found, strict=True)):
            if trace_json is None:
                continue
            tags = {"martian.case_id": result.case_id, "martian.turn": str(turn.number)}
            if attempt is not None:
                tags["martian.attempt"] = str(attempt)
            if result.simulated:
                tags["martian.simulated"] = "true"
            trace = self._as_run_trace(
                trace_json,
                result,
                name=f"{prefix} turn {turn.number}",
                output=turn.output,
                error=turn.error,
                scores=[self._attempt_score(result, label, verdict) for label, verdict in turn.checks.items()],
                tags=tags,
                inputs=turn.inputs,
                session_id=session_id,
            )
            saved = await self.store.record_trace(SINGLE_USER, trace)
            written[position] = turn.model_copy(update={"trace_id": saved.id, "latency_ms": trace.latency_ms})
            recorded.append(saved.id)
        return written, recorded

    @staticmethod
    def _standing(turns: list[TurnResult], status: CaseStatus) -> tuple[str | None, float | None]:
        """The trace of the turn that stands for a conversation, the first whose verdict is the
        conversation's, else its first; and the conversation's latency, its turns' added up."""
        if not turns:
            return None, None
        verdict = {"passed": True, "failed": False}
        chosen = turns[representative([verdict.get(turn.status) for turn in turns], verdict.get(status))]
        latencies = [turn.latency_ms for turn in turns if turn.latency_ms is not None]
        return chosen.trace_id, round(sum(latencies), 3) if latencies else None

    def _attempt_score(self, result: CaseResult, label: str, verdict: CheckVerdict) -> Score:
        """One check's verdict on one attempt, as a score its trace carries."""
        scorer = self._scorers.get(label)
        judge = scorer is not None and scorer.kind == "llm_judge"
        return Score(
            run_id=result.run_id,
            case_id=result.case_id,
            scorer=label,
            scorer_version=scorer.version if scorer else "",
            source="llm_judge" if judge else "code",
            source_id=(scorer.model or "") if scorer is not None and judge else "",
            value=verdict.passed,
            passed=verdict.passed,
            rationale=verdict.rationale,
            error=verdict.error,
            created_at=result.finished_at,
        )

    def _trace(self, case: GradedCase, result: CaseResult, scores: list[Score]) -> Trace:
        """The case's trace from MLflow's evaluation harness, as a trace of this run."""
        assert self.run is not None and case.trace is not None
        record = case.record
        return self._as_run_trace(
            case.trace,
            result,
            name=f"{self.run.code} {result.case_id}",
            output=record.get("output"),
            error=record.get("error") or "",
            scores=scores,
            tags={"martian.case_id": result.case_id},
        )

    def _as_run_trace(
        self,
        found: str,
        result: CaseResult,
        *,
        name: str,
        output: Any,
        error: str,
        scores: list[Score],
        tags: dict[str, str],
        inputs: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Trace:
        """A trace MLflow's evaluation harness recorded (its JSON), as a trace of this run under a
        new id, with ``output`` as its response and ``scores`` as its assessments. Its request is
        the input under ``input_key`` of ``inputs`` (a conversation turn's), else of the case's.
        A conversation turn's trace has its conversation's ``session_id``."""
        from mlflow.entities import Trace as MlflowTrace

        from martian.evals.store.mlflow_local import trace_from_mlflow

        assert self.run is not None
        trace = trace_from_mlflow(MlflowTrace.from_json(found), DEFAULT_PROJECT)
        trace_id = "tr-" + uuid.uuid4().hex
        given = result.inputs if inputs is None else inputs
        message = given.get(self.suite.input_key)
        update: dict[str, Any] = {
            "id": trace_id,
            "name": name,
            "state": "error" if error else trace.state,
            "request": message
            if isinstance(message, str)
            else json.dumps(given, ensure_ascii=False, sort_keys=True, default=str),
            "response": output or "",
            "run_id": self.run.id,
            "tags": tags,
            "scores": [score.model_copy(update={"trace_id": trace_id}) for score in scores],
        }
        if session_id is not None:
            update["session_id"] = session_id
        return trace.model_copy(update=update)

    def reference(self) -> dict[str, str]:
        """Where the run was recorded: its id, its code and the data folder."""
        assert self.run is not None
        return {"id": self.run.id, "code": self.run.code, "data_dir": str(self.home)}


__all__ = ["GradedCase", "RunRecorder"]
