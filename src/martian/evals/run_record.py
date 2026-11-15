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

Results are written after each batch, so a run shows in the UI while it runs. The output folder's
files are written as before, and nothing here reads them.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from martian._version import __version__
from martian.config import ConfigError
from martian.evals.model import (
    CaseResult,
    CaseStatus,
    Counts,
    Run,
    RunState,
    Score,
    StoreRef,
    Suite,
    Trace,
    from_results_record,
)
from martian.evals.server.principal import SINGLE_USER
from martian.evals.server.projection import DEFAULT_PROJECT


@dataclass(frozen=True)
class GradedCase:
    """One case graded in this run: its position among the run's cases, its ``results.jsonl``
    record, its own expectations, and the JSON of the trace MLflow recorded for it, if any."""

    position: int
    record: dict[str, Any]
    expectations: dict[str, Any]
    trace: str | None = None


def _wait[T](work: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(work)


class RunRecorder:
    """Records one run of ``suite`` in the data folder ``home``, as a context manager:
    :meth:`start`, :meth:`graded` for each batch, then :meth:`finish`. Open it before the run
    changes anything in its output folder, so a data folder that cannot be opened stops the run
    first."""

    def __init__(self, home: Path, suite: Suite) -> None:
        from martian.evals.local import LocalEditionError, open_stores

        try:
            self.store, self.records = open_stores(home)
        except LocalEditionError as error:
            raise ConfigError(str(error)) from None
        except Exception as error:  # noqa: BLE001 - a folder may fail to open in many ways; say which
            raise ConfigError(f"could not open the local data folder {home}: {type(error).__name__}") from None
        self.home = home
        self.suite = suite
        self.run: Run | None = None
        self._ended = False
        self._scorers = {scorer.name: scorer for scorer in suite.scorers}
        self._statuses: dict[str, CaseStatus] = {}

    # --- the run -----------------------------------------------------------------------------

    def start(self, *, planned: int, total: int, limit: int | None, reused: list[GradedCase]) -> Run:
        """Record that the run started over ``planned`` of the dataset's ``total`` cases, with the
        results it reuses from earlier runs."""
        return _wait(self._start(planned=planned, total=total, limit=limit, reused=reused))

    async def _start(self, *, planned: int, total: int, limit: int | None, reused: list[GradedCase]) -> Run:
        now = datetime.now(UTC)
        suite = self.suite
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
            provenance={
                "suite": {"name": suite.name, "digest": suite.digest},
                "dataset": {"name": suite.dataset_name, "digest": suite.dataset_digest},
                "sdk_version": __version__,
            },
            requested_by=SINGLE_USER.user_id,
            created_at=now,
            started_at=now,
        )
        run = await self.store.start_run(SINGLE_USER, run)
        self.run = await self.records.create_run(SINGLE_USER, run)
        await self._write(reused, traces=False)
        return self.run

    async def _code(self) -> str:
        """The suite's name and its number among the suite's runs in the project."""
        count, cursor = 0, None
        while True:
            page = await self.records.list_runs(SINGLE_USER, DEFAULT_PROJECT, limit=500, cursor=cursor)
            count += sum(run.suite_id == self.suite.id for run in page.items)
            if (cursor := page.next_cursor) is None:
                return f"{self.suite.name}-{count + 1}"

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
        interrupted. The stores are closed either way."""
        try:
            if error is not None and self.run is not None and not self._ended:
                if isinstance(error, KeyboardInterrupt):
                    _wait(self._end("cancelled", ""))
                else:
                    _wait(self._end("failed", f"the run stopped early: {type(error).__name__}"))
        finally:
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

    def _trace(self, case: GradedCase, result: CaseResult, scores: list[Score]) -> Trace:
        """The case's trace from MLflow's evaluation harness, as a trace of this run."""
        from mlflow.entities import Trace as MlflowTrace

        from martian.evals.store.mlflow_local import trace_from_mlflow

        assert self.run is not None and case.trace is not None
        found = trace_from_mlflow(MlflowTrace.from_json(case.trace), DEFAULT_PROJECT)
        trace_id = "tr-" + uuid.uuid4().hex
        record = case.record
        message = result.inputs.get(self.suite.input_key)
        return found.model_copy(
            update={
                "id": trace_id,
                "name": f"{self.run.code} {result.case_id}",
                "state": "error" if record.get("error") else found.state,
                "request": message
                if isinstance(message, str)
                else json.dumps(result.inputs, ensure_ascii=False, sort_keys=True, default=str),
                "response": record.get("output") or "",
                "run_id": self.run.id,
                "tags": {"martian.case_id": result.case_id},
                "scores": [score.model_copy(update={"trace_id": trace_id}) for score in scores],
            }
        )

    def reference(self) -> dict[str, str]:
        """Where the run was recorded: its id, its code and the data folder."""
        assert self.run is not None
        return {"id": self.run.id, "code": self.run.code, "data_dir": str(self.home)}


__all__ = ["GradedCase", "RunRecorder"]
