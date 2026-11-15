"""A suite-file run, recorded in the local edition's data folder so that ``martian ui`` shows it.

``martian evals run`` writes each run through the evals core's store and record store
(:func:`martian.evals.local.open_stores`), as the single local user, in the default project:

- The run is a ``Run`` of source ``local`` with the suite's id, digest, target, scorers and dataset
  digest. Its code is the suite's name and its number among that suite's runs, such as ``evals-3``.
  It is ``running`` while cases are graded and ``completed`` once every case has a result, whether
  or not each has a verdict; a run that stops early is ``failed``, or ``cancelled`` when interrupted.
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
- A suite with a matrix records one run per combination (:mod:`martian.evals.matrix`). Its
  ``provenance`` holds the combination under ``matrix`` (:func:`martian.evals.matrix.provenance`),
  and its code is the suite's name with the combination's id in brackets and its number among
  that combination's runs, such as ``evals[model=large,prompt=terse]-2``.

Results are written after each batch, so a run shows in the UI while it runs. The output folder's
files are written as before, and nothing here reads them.

The process holds the run (:class:`~martian.evals.local_runs.RunLock`) from before the run is
recorded until it ends, so ``martian ui`` leaves the run alone while it runs. A run whose process
was killed, so that it could not record its end, is finished as interrupted when ``martian ui``
starts or when the run is cancelled there.
"""

from __future__ import annotations

import asyncio
import json
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
    RunState,
    Score,
    StoreRef,
    Suite,
    Trace,
    from_results_record,
)
from martian.evals.repeats import representative
from martian.evals.server.principal import SINGLE_USER
from martian.evals.server.projection import DEFAULT_PROJECT

if TYPE_CHECKING:
    from martian.evals.local_runs import RunLock


@dataclass(frozen=True)
class GradedCase:
    """One case graded in this run: its position among the run's cases, its ``results.jsonl``
    record, its own expectations, and the JSON of the trace MLflow recorded for it, if any. A
    case run more than once has the JSON of each attempt's trace in ``attempt_traces`` instead,
    in the order of its record's ``attempts``."""

    position: int
    record: dict[str, Any]
    expectations: dict[str, Any]
    trace: str | None = None
    attempt_traces: tuple[str | None, ...] = ()


def _wait[T](work: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(work)


class RunRecorder:
    """Records one run of ``suite`` in the data folder ``home``, as a context manager:
    :meth:`start`, :meth:`graded` for each batch, then :meth:`finish`. Open it before the run
    changes anything in its output folder, so a data folder that cannot be opened stops the run
    first."""

    def __init__(self, home: Path, suite: Suite, *, matrix: dict[str, Any] | None = None) -> None:
        """``matrix`` is the run's combination of the suite's matrix, as ``provenance.matrix``
        holds it, for a suite with a matrix."""
        from martian.evals.local import LocalEditionError, open_stores

        self._lock: RunLock | None = None
        try:
            self.store, self.records = open_stores(home)
        except LocalEditionError as error:
            raise ConfigError(str(error)) from None
        except Exception as error:  # noqa: BLE001 - a folder may fail to open in many ways; say which
            raise ConfigError(f"could not open the local data folder {home}: {type(error).__name__}") from None
        self.home = home
        self.suite = suite
        self.matrix = matrix
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
    ) -> Run:
        """Record that the run started over ``planned`` of the dataset's ``total`` cases, with the
        results it reuses from earlier runs, and with ``repeats``, the suite's repeat plan, when
        it runs each case more than once."""
        return _wait(self._start(planned=planned, total=total, limit=limit, reused=reused, repeats=repeats))

    async def _start(
        self, *, planned: int, total: int, limit: int | None, reused: list[GradedCase], repeats: dict[str, Any] | None
    ) -> Run:
        from martian.evals.local_runs import PROCESS, RunLock

        now = datetime.now(UTC)
        suite = self.suite
        provenance: dict[str, Any] = {
            "suite": {"name": suite.name, "digest": suite.digest},
            "dataset": {"name": suite.dataset_name, "digest": suite.dataset_digest},
            "sdk_version": __version__,
        }
        if repeats is not None:
            provenance["repeats"] = repeats
        if self.matrix is not None:
            provenance["matrix"] = self.matrix
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
            scorers=list(suite.scorers),
            counts=Counts(cases=planned),
            max_cases=limit or None,
            truncated=planned < total,
            provenance=provenance,
            requested_by=SINGLE_USER.user_id,
            created_at=now,
            started_at=now,
        )
        # Held before the run is recorded, so no process ever finds it running and not held.
        self._lock = RunLock(self.home, run.id, PROCESS)
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

    def __enter__(self) -> RunRecorder:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, traceback: Any) -> None:
        """A run that started and stops on an error is ``failed``, or ``cancelled`` when it was
        interrupted. The run is released and the stores are closed either way."""
        try:
            if error is not None and self.run is not None and not self._ended:
                if isinstance(error, KeyboardInterrupt):
                    _wait(self._end("cancelled", ""))
                else:
                    _wait(self._end("failed", f"the run stopped early: {type(error).__name__}"))
        finally:
            if self._lock is not None:
                self._lock.release()
            self.records.close()

    async def _end(self, state: RunState, error: str) -> Run:
        assert self.run is not None
        self._ended = True
        counts = self._counts()
        finished = self.run.model_copy(
            update={"state": state, "counts": counts, "error": error, "finished_at": datetime.now(UTC)}
        )
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
            if traces and case.attempt_traces:
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
        if results:
            await self.records.put_results(SINGLE_USER, DEFAULT_PROJECT, results)
        if scores:
            await self.records.put_scores(SINGLE_USER, DEFAULT_PROJECT, scores)
        self.run = await self.records.update_run(
            SINGLE_USER, self.run.model_copy(update={"counts": self._counts()}), expected_state="running"
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
    ) -> Trace:
        """A trace MLflow's evaluation harness recorded (its JSON), as a trace of this run under a
        new id, with ``output`` as its response and ``scores`` as its assessments."""
        from mlflow.entities import Trace as MlflowTrace

        from martian.evals.store.mlflow_local import trace_from_mlflow

        assert self.run is not None
        trace = trace_from_mlflow(MlflowTrace.from_json(found), DEFAULT_PROJECT)
        trace_id = "tr-" + uuid.uuid4().hex
        message = result.inputs.get(self.suite.input_key)
        return trace.model_copy(
            update={
                "id": trace_id,
                "name": name,
                "state": "error" if error else trace.state,
                "request": message
                if isinstance(message, str)
                else json.dumps(result.inputs, ensure_ascii=False, sort_keys=True, default=str),
                "response": output or "",
                "run_id": self.run.id,
                "tags": tags,
                "scores": [score.model_copy(update={"trace_id": trace_id}) for score in scores],
            }
        )

    def reference(self) -> dict[str, str]:
        """Where the run was recorded: its id, its code and the data folder."""
        assert self.run is not None
        return {"id": self.run.id, "code": self.run.code, "data_dir": str(self.home)}


__all__ = ["GradedCase", "RunRecorder"]
