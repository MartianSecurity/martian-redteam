"""The work itself: running a queued run against its target, and scoring a finished run.

A port of ``app/evals_execution.py`` and ``_execute_scores`` in ``app/evals_workbench.py``
(MorningHQ/MartianBackend) onto the store and the record store:

- A run reads the active cases of its published dataset (the default project's dataset version it
  was admitted with) up to its case limit, calls the target once per case with no retry, and
  grades each output by exact match. Each case that called the target gets a trace in the store,
  with its exact-match score; a case with no usable prompt or expected text is skipped, and so is a
  conversation case (``Case.turns``), which ``martian evals run`` runs turn by turn. A failed
  call is the case's error: an HTTP target's in its own words (``TargetCallError``), which name
  the setting to check, anything else with its type.
- Before a run calls its target, on its first start and on every resume, the worker pins the
  target as it has it now; a pin that is not the run's (``provenance["target"]``) fails the run,
  naming the target, so one run never mixes two systems.
- A scoring job scores every case result of a completed run (at most 1,000) with each of its
  scorers: code scorers here, judge scorers through the worker's judge, one request per model the
  job's judges are pinned to. Each judge score records the model and version that produced it.
  Its scores sit beside the run's own, and each scored value is also kept on the case's trace.
- Before each case the record is read again. A cancel request stops the work there; a record that
  is no longer running (someone else failed or finished it) ends it without another write.
- Each piece of work runs under limits (``RunLimits``): the run's own, ``provenance["limits"]``,
  which a scoring job takes from its run, else the worker's. Each start of the work has them
  afresh. Before each case, work that has run for ``max_seconds``, or whose judges have used
  ``max_judge_tokens``, stops: it is ``stopped``, with the limit in its ``error`` and a run's in
  ``provenance["stopped"]`` too, and keeps the cases it finished. A run's remaining cases get no
  result, so a resumed run calls them; the case in flight finishes within its target's own limit.
- Each finished case is checkpointed before the next one starts: a run's case result and its score
  in one transaction (``put_results`` with ``scores``), and a scoring job's scores of one case in
  one transaction. Work that runs again skips the cases already finished, so their target or
  judge is not called again, and counts them as they were recorded.
- Every move from one state to the next names the state it expects, so two workers never both take
  the same work. Work that fails part way is marked ``failed`` with the reason, never left running.
  Work whose worker stopped part way is queued again by the queue's next worker, which resumes it
  after its finished cases (:func:`take_over`), or failed when there is no queue to take it again
  (:func:`fail_unfinished`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import TYPE_CHECKING, Any

from martian.evals.limits import Budget
from martian.evals.model import (
    Case,
    CaseResult,
    CaseStatus,
    Counts,
    Run,
    RunLimits,
    RunState,
    Score,
    ScorerVersion,
    ScoringJob,
    Span,
    StoreRef,
    Trace,
)
from martian.evals.records import RecordStore
from martian.evals.runner import TargetCallError
from martian.evals.server.projection import DEFAULT_PROJECT, expected_text, job_config, prompt_text, text_of
from martian.evals.store import EvalStore, StoreError

# The package defines these before it imports this module.
from martian.evals.worker import Judge, JudgeError, JudgeRequest, JudgeResult, TargetCall, Work, Worker
from martian.evals.worker.scorers import code_score, exact_match, scorer_source
from martian.evals.worker.targets import changed_target

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

log = logging.getLogger("martian.evals.worker")

CASE_PAGE = 1000
MAX_SCORED_RESULTS = 1000
EXACT_MATCH_SCORER = "exact_match"
STOPPED = "Stopped before this target request"
UNUSABLE = "item is missing a usable prompt or expected output"
CONVERSATION = "item is a conversation, which martian evals run runs turn by turn; the worker sends one prompt per item"
NO_OUTPUT = "No successful saved target output"
NO_JUDGE = "Judge unavailable; no automatic model retry"
JUDGE_MODEL_KEY = "martian.judge.model"
JUDGE_MODEL_VERSION_KEY = "martian.judge.model_version"
JUDGE_ENDPOINT_KEY = "martian.judge.endpoint"
JUDGE_VERSION_KEY = "martian.judge.version"
JUDGE_ERROR_LENGTH = 500
INTERRUPTED = "Interrupted: the process working on this run stopped before it finished"
NOT_ACCEPTED = "The worker did not accept this work, so nothing ran"
LIMITS_KEY = "limits"
"""``Run.provenance`` key of the limits a run, and its scoring jobs, run under."""
STOPPED_KEY = "stopped"
"""``Run.provenance`` key of the limit a stopped run reached (``RunLimits.stop``)."""


def _now() -> datetime:
    return datetime.now(UTC)


def run_limits(run: Run, default: RunLimits | None = None) -> RunLimits:
    """The limits ``run`` and its scoring jobs run under: its own, ``provenance["limits"]``, else
    ``default``, the worker's."""
    own = run.provenance.get(LIMITS_KEY)
    if isinstance(own, dict):
        return RunLimits.model_validate(own)
    return default or RunLimits()


def _detail(error: Exception, fallback: str) -> str:
    """What a failed record says: a store's refusal as the store gave it, which names the operation
    and never a value written, anything else generic."""
    return error.detail if isinstance(error, StoreError) else fallback


def trace_id() -> str:
    """A trace id in MLflow's form, which the contract's trace routes take."""
    return "tr-" + uuid.uuid4().hex


def is_active(case: Case) -> bool:
    return str(case.tags.get("martian.status", "ACTIVE")).upper() == "ACTIVE"


# --- runs -----------------------------------------------------------------------------------


async def execute_run(
    store: EvalStore,
    records: RecordStore,
    work: Work,
    *,
    call_for: Callable[[Run], TargetCall | None],
    pin_for: Callable[[Run], Awaitable[dict[str, Any]]],
    timeout_seconds: float,
    limits: RunLimits | None = None,
) -> None:
    """Run a queued run. ``call_for`` gives the call for the run's target, or ``None`` when the
    worker cannot call it, which fails the run. ``pin_for`` pins the run's target as the worker has
    it now, as ``Worker.check_target`` does; a pin that is not the run's fails the run before any
    call (:func:`~martian.evals.worker.targets.changed_target`). ``timeout_seconds`` bounds each
    target call, and ``limits`` are the worker's, for a run that has none of its own."""
    principal = work.principal
    run = await records.get_run(principal, work.project_id, work.run_id)
    if run is None or run.state != "queued":
        return
    if run.cancel_requested_at is not None:
        moved = await _move_run(records, principal, run, {"state": "cancelled", "finished_at": _now()}, "queued")
        if moved is not None and run.store.run_id is not None:  # started by a worker that stopped (take_over)
            await store.finish_run(principal, run.project_id, run.id, state="cancelled", counts=run.counts)
        return
    try:
        # A run taken again after its worker stopped keeps the time it first started.
        started = run.started_at or _now()
        run = await records.update_run(
            principal, run.model_copy(update={"state": "running", "started_at": started}), expected_state="queued"
        )
    except StoreError:
        return  # another worker took it, or it was cancelled meanwhile
    try:
        await _run_cases(store, records, principal, run, call_for(run), pin_for, timeout_seconds, limits)
    except Exception as error:  # noqa: BLE001 - a run never stays running after its worker fails
        log.exception("Eval run %s failed", run.id)
        await _fail_run(records, principal, run, _detail(error, "runner crashed"))


async def _move_run(
    records: RecordStore, principal: Principal, run: Run, changes: dict[str, Any], expected: RunState
) -> Run | None:
    try:
        return await records.update_run(principal, run.model_copy(update=changes), expected_state=expected)
    except StoreError as error:
        if error.kind != "conflict":
            raise
        return None


async def _advance_run(records: RecordStore, principal: Principal, run: Run, changes: dict[str, Any]) -> Run | None:
    """Apply ``changes`` to the run as it is stored now, while it is still running. The run is read
    again first: a cancel request made while a case ran must not be written over."""
    current = await records.get_run(principal, run.project_id, run.id)
    if current is None or current.state != "running":
        return None
    return await _move_run(records, principal, current, changes, "running")


async def _fail_run(records: RecordStore, principal: Principal, run: Run, detail: str) -> None:
    current = await records.get_run(principal, run.project_id, run.id)
    if current is not None and current.state == "running":
        await _move_run(
            records, principal, current, {"state": "failed", "error": detail, "finished_at": _now()}, "running"
        )


async def active_cases(
    store: EvalStore, principal: Principal, dataset_id: str, *, limit: int | None
) -> tuple[list[Case], bool]:
    """The dataset's active cases in order, at most ``limit``, and whether there were more."""
    found: list[Case] = []
    offset = 0
    while limit is None or len(found) <= limit:
        page = await store.read_cases(principal, DEFAULT_PROJECT, dataset_id, offset=offset, limit=CASE_PAGE)
        found.extend(case for case in page.items if is_active(case))
        offset += len(page.items)
        if page.next_cursor is None:
            break
    if limit is None or len(found) <= limit:
        return found, False
    return found[:limit], True


async def _run_cases(
    store: EvalStore,
    records: RecordStore,
    principal: Principal,
    run: Run,
    call: TargetCall | None,
    pin_for: Callable[[Run], Awaitable[dict[str, Any]]],
    timeout_seconds: float,
    default_limits: RunLimits | None,
) -> None:
    if call is None:
        await _fail_run(records, principal, run, "The worker cannot call this run's target")
        return
    # Every start, a resume included, calls only the target the run was admitted with.
    if (changed := changed_target(run, await pin_for(run))) is not None:
        await _fail_run(records, principal, run, changed)
        return
    limits = run_limits(run, default_limits)
    budget = Budget(limits)
    dataset_id = run.store.dataset_id
    dataset = await store.get_dataset(principal, DEFAULT_PROJECT, dataset_id) if dataset_id else None
    if dataset is None or (run.dataset_digest is not None and dataset.digest != run.dataset_digest):
        await _fail_run(records, principal, run, "Dataset identity changed; submit a new eval run")
        return
    cases, truncated = await active_cases(store, principal, dataset.id, limit=run.max_cases)
    # The cases a worker that stopped part way already finished: their results are kept, their
    # target is not called again, and they count as they are.
    finished = await finished_cases(records, principal, run, cases)
    counts = Counts(cases=len(cases))
    for result in finished.values():
        counts = _counted(counts, result.status)
    native = await store.start_run(principal, run.model_copy(update={"counts": counts, "truncated": truncated}))
    stored = StoreRef(
        experiment_id=native.store.experiment_id, run_id=native.store.run_id, dataset_id=run.store.dataset_id
    )
    changes: dict[str, Any] = {"counts": counts, "truncated": truncated, "store": stored}
    if LIMITS_KEY not in run.provenance and limits != RunLimits():  # the worker's: kept with the run
        changes["provenance"] = {**run.provenance, LIMITS_KEY: limits.model_dump(mode="json")}
    moved = await _move_run(records, principal, run, changes, "running")
    if moved is None:
        return
    run = moved
    for position, case in enumerate(cases):
        if case.id in finished:
            continue
        current = await records.get_run(principal, run.project_id, run.id)
        if current is None or current.state != "running":
            return
        if current.cancel_requested_at is not None:
            stopped = [
                _skipped(run, offset, pending, STOPPED)
                for offset, pending in enumerate(cases[position:], position)
                if pending.id not in finished
            ]
            await records.put_results(principal, run.project_id, stopped)
            counts = counts.model_copy(update={"skipped": counts.skipped + len(stopped)})
            break
        if (reached := budget.reached()) is not None:
            # The cases not run keep no result, so a resumed run calls them.
            await store.finish_run(principal, run.project_id, run.id, state="stopped", counts=counts)
            stop = {
                "state": "stopped",
                "counts": counts,
                "error": reached["message"],
                "provenance": {**current.provenance, STOPPED_KEY: reached},
                "finished_at": _now(),
            }
            await _move_run(records, principal, current, stop, "running")
            return
        result, score = await _run_case(store, principal, run, position, case, call, timeout_seconds)
        # The case's checkpoint: its result and its score in one transaction, before the next case.
        await records.put_results(principal, run.project_id, [result], scores=[score] if score else [])
        counts = _counted(counts, result.status)
        if await _advance_run(records, principal, run, {"counts": counts}) is None:
            return
    current = await records.get_run(principal, run.project_id, run.id)
    if current is None or current.state != "running":
        return
    state: RunState = "cancelled" if current.cancel_requested_at is not None else "completed"
    await store.finish_run(principal, run.project_id, run.id, state=state, counts=counts)
    await _move_run(records, principal, current, {"state": state, "counts": counts, "finished_at": _now()}, "running")


async def finished_cases(
    records: RecordStore, principal: Principal, run: Run, cases: list[Case]
) -> dict[str, CaseResult]:
    """The results already recorded for ``run``'s ``cases``, by case id: the cases a worker that
    stopped part way finished, each recorded with its scores as it completed. A new run has none.
    The run reads the dataset version it was admitted with, so its cases are the ones those
    results are of."""
    ids = {case.id for case in cases}
    return {result.case_id: result for result in await all_results(records, principal, run) if result.case_id in ids}


def _counted(counts: Counts, status: str) -> Counts:
    field = {"passed": "passed", "failed": "failed", "skipped": "skipped"}.get(status, "errors")
    return counts.model_copy(update={field: getattr(counts, field) + 1})


def _skipped(run: Run, position: int, case: Case, reason: str) -> CaseResult:
    return CaseResult(
        run_id=run.id,
        case_id=case.id,
        position=position,
        status="skipped",
        inputs=case.inputs,
        expectations=case.expectations,
        error=reason,
        finished_at=_now(),
    )


async def _run_case(
    store: EvalStore,
    principal: Principal,
    run: Run,
    position: int,
    case: Case,
    call: TargetCall,
    timeout_seconds: float,
) -> tuple[CaseResult, Score | None]:
    if case.turns:
        return _skipped(run, position, case, CONVERSATION), None
    prompt, expected = prompt_text(case.inputs), expected_text(case.expectations)
    if not prompt or not expected:
        return _skipped(run, position, case, UNUSABLE), None
    started, clock = _now(), perf_counter()
    output: Any = None
    error = ""
    try:
        output = await asyncio.wait_for(call(case), timeout=timeout_seconds)
    except TimeoutError:
        error = "item timed out"
    except TargetCallError as failure:  # the target's own words, which name the setting to check
        error = str(failure)[:1800]
    except Exception as failure:  # noqa: BLE001 - any target failure is the case's error
        error = f"{type(failure).__name__}: {failure}"[:1800]
    latency = round((perf_counter() - clock) * 1000, 3)
    text = text_of(output)
    status: CaseStatus = "error" if error else "passed" if exact_match(text, expected) else "failed"
    tid = trace_id()
    scores = []
    if not error:
        matched = status == "passed"
        scores.append(
            Score(
                run_id=run.id,
                case_id=case.id,
                trace_id=tid,
                scorer=EXACT_MATCH_SCORER,
                scorer_version="v1",
                source="code",
                source_id="martian.exact_match:v1",
                value=matched,
                passed=matched,
                rationale=code_score(EXACT_MATCH_SCORER, text, expected)[1],
                created_at=_now(),
            )
        )
    start_ms = started.timestamp() * 1000
    trace = await store.record_trace(
        principal,
        Trace(
            id=tid,
            project_id=run.project_id,
            name=f"{run.code} {case.id}",
            state="error" if error else "ok",
            request_time=started,
            latency_ms=latency,
            request=prompt,
            response=text,
            run_id=run.id,
            scores=scores,
            spans=[
                Span(
                    id=uuid.uuid4().hex[:16],
                    name="target",
                    kind="AGENT",
                    status="error" if error else "ok",
                    start_ms=start_ms,
                    end_ms=start_ms + latency,
                    inputs=case.inputs,
                    outputs=output,
                    attributes={"martian.case_id": case.id},
                )
            ],
        ),
    )
    logged = trace.scores[0] if trace.scores else None
    result = CaseResult(
        run_id=run.id,
        case_id=case.id,
        position=position,
        status=status,
        inputs=case.inputs,
        expectations=case.expectations,
        output=output,
        error=error,
        latency_ms=latency,
        finished_at=_now(),
        store=StoreRef(
            experiment_id=run.store.experiment_id,
            run_id=run.store.run_id,
            trace_id=tid,
            assessment_id=logged.store.assessment_id if logged else None,
        ),
    )
    score = scores[0].model_copy(update={"store": logged.store}) if logged else None
    return result, score


# --- scoring jobs ---------------------------------------------------------------------------


async def execute_scoring(
    store: EvalStore, records: RecordStore, work: Work, *, judge: Judge | None, limits: RunLimits | None = None
) -> None:
    """Score a finished run with a queued scoring job. Without a judge, judge scores are errors.
    The job runs under its run's limits, else ``limits``, the worker's."""
    principal, project = work.principal, work.project_id
    job = await _job(records, principal, project, work.run_id, work.job_id or "")
    if job is None or job.state != "queued":
        return
    if job.cancel_requested_at is not None:
        await _move_job(records, principal, project, job, {"state": "cancelled", "finished_at": _now()}, "queued")
        return
    try:
        job = await records.update_scoring_job(
            principal, project, job.model_copy(update={"state": "running"}), expected_state="queued"
        )
    except StoreError:
        return
    try:
        await _score_results(store, records, principal, project, job, judge, limits)
    except Exception:  # noqa: BLE001 - a job never stays running after its worker fails
        log.exception("Scoring job %s failed", job.id)
        current = await _job(records, principal, project, job.run_id, job.id)
        if current is not None and current.state == "running":
            await _move_job(
                records,
                principal,
                project,
                current,
                {
                    "state": "failed",
                    "error": "Scoring interrupted; saved assessments retained; submit a new job to score again",
                    "finished_at": _now(),
                },
                "running",
            )


async def _job(records: RecordStore, principal: Principal, project: str, run_id: str, job_id: str) -> ScoringJob | None:
    jobs = await records.list_scoring_jobs(principal, project, run_id)
    return next((job for job in jobs if job.id == job_id), None)


async def _move_job(
    records: RecordStore,
    principal: Principal,
    project: str,
    job: ScoringJob,
    changes: dict[str, Any],
    expected: RunState,
) -> ScoringJob | None:
    try:
        return await records.update_scoring_job(
            principal, project, job.model_copy(update=changes), expected_state=expected
        )
    except StoreError as error:
        if error.kind != "conflict":
            raise
        return None


async def _advance_job(
    records: RecordStore, principal: Principal, project: str, job: ScoringJob, changes: dict[str, Any]
) -> ScoringJob | None:
    """:func:`_advance_run` for a scoring job."""
    current = await _job(records, principal, project, job.run_id, job.id)
    if current is None or current.state != "running":
        return None
    return await _move_job(records, principal, project, current, changes, "running")


def model_source(answer: JudgeResult) -> str:
    """A judge score's ``source_id``: the model that produced it, with its version when reported."""
    return f"{answer.model}@{answer.model_version}" if answer.model_version else answer.model


def _judge_metadata(scorer: ScorerVersion, answer: JudgeResult | None) -> dict[str, Any]:
    """What a judge score records of its model: the model that answered (or the one the judge is
    pinned to, when none did), the version the provider reported, the judge's own version, and
    the endpoint it was asked at."""
    found: dict[str, Any] = {}
    if model := answer.model if answer is not None else scorer.model:
        found[JUDGE_MODEL_KEY] = model
    if answer is not None and answer.model_version:
        found[JUDGE_MODEL_VERSION_KEY] = answer.model_version
    if answer is not None and answer.judge_version:
        found[JUDGE_VERSION_KEY] = answer.judge_version
    if scorer.endpoint:
        found[JUDGE_ENDPOINT_KEY] = scorer.endpoint
    return found


async def _judged(judge: Judge, request: JudgeRequest, what: str) -> JudgeResult | str | None:
    """The judge's answer to one request, an error for each of its scores when the judge refused
    or failed with a message it can show (``JudgeError``) or answered from another model than the
    request names, or None when the judge failed otherwise."""
    try:
        answer = await judge(request)
    except JudgeError as error:
        log.warning("Judge gave no verdict for %s: %s", what, error)
        return str(error)[:JUDGE_ERROR_LENGTH] or NO_JUDGE
    except Exception:  # noqa: BLE001 - a judge failure is each judge score's error
        log.exception("Judge failed for %s", what)
        return None
    if request.model is not None and answer.model != request.model:
        return f"The judge answered with {answer.model}, not {request.model}, the model this judge is pinned to"
    return answer


def job_outcome(succeeded: int, errors: int) -> RunState:
    if succeeded and not errors:
        return "completed"
    return "partial" if succeeded else "failed"


async def all_results(records: RecordStore, principal: Principal, run: Run) -> list[CaseResult]:
    results: list[CaseResult] = []
    while True:
        page = await records.list_results(principal, run.project_id, run.id, offset=len(results), limit=1000)
        results.extend(page.items)
        if page.next_cursor is None:
            return results


async def _score_results(
    store: EvalStore,
    records: RecordStore,
    principal: Principal,
    project: str,
    job: ScoringJob,
    judge: Judge | None,
    default_limits: RunLimits | None,
) -> None:
    run = await records.get_run(principal, project, job.run_id)
    results = await all_results(records, principal, run) if run else []
    if run is None or run.state != "completed" or len(results) > MAX_SCORED_RESULTS:
        raise ValueError("Saved run must be complete and bounded")
    budget = Budget(run_limits(run, default_limits))
    # The cases a worker that stopped part way already scored: each case's scores of the job are
    # recorded in one transaction, so a case has all of them or none. They are not scored again.
    earlier = [
        score for score in await records.list_scores(principal, project, run.id) if score.scoring_job_id == job.id
    ]
    scored = {score.case_id for score in earlier}
    succeeded = sum(s.value is not None and not s.error for s in earlier)
    errors = sum(s.value is None or bool(s.error) for s in earlier)
    progress = {"total": len(results) * len(job.scorers), "succeeded": succeeded, "errors": errors}
    moved = await _move_job(records, principal, project, job, progress, "running")
    if moved is None:
        return
    job = moved
    # One judge request per model the job's judges are pinned to, in the order they first appear.
    by_model: dict[tuple[str | None, str | None], list[ScorerVersion]] = {}
    for scorer in job.scorers:
        if scorer.kind == "llm_judge":
            by_model.setdefault((scorer.model, scorer.endpoint), []).append(scorer)
    definition = job_config(job)["definition_digest"]
    for result in results:
        if result.case_id in scored:
            continue
        current = await _job(records, principal, project, job.run_id, job.id)
        if current is None or current.state != "running":
            return
        if current.cancel_requested_at is not None:
            await _move_job(
                records,
                principal,
                project,
                current,
                {
                    "state": "cancelled",
                    "finished_at": _now(),
                    "error": "Stopped before the next saved-output judgment; existing assessments retained",
                },
                "running",
            )
            return
        if (reached := budget.reached()) is not None:
            # The cases not scored keep no scores of the job, so a resumed job scores them.
            stop = {"state": "stopped", "finished_at": _now(), "error": reached["message"]}
            await _move_job(records, principal, project, current, stop, "running")
            return
        finished = result.status in ("passed", "failed")
        output, expected = text_of(result.output), expected_text(result.expectations)
        answers: dict[tuple[str | None, str | None], JudgeResult | str | None] = {}
        if finished and judge is not None:
            for (model, endpoint), scorers in by_model.items():
                answers[(model, endpoint)] = await _judged(
                    judge,
                    JudgeRequest(
                        principal=principal,
                        prompt=prompt_text(result.inputs),
                        expected=expected,
                        output=output,
                        scorers=tuple(scorers),
                        model=model,
                        endpoint=endpoint,
                        job_id=job.id,
                    ),
                    f"case {result.case_id} of run {run.id}",
                )
        for answer in answers.values():
            if isinstance(answer, JudgeResult):  # a judge that failed reported nothing to count
                budget.spend(answer.tokens)
        scores = []
        for scorer in job.scorers:
            value: Any = None
            rationale = error = ""
            source_id = scorer_source(scorer)
            metadata: dict[str, Any] = {"martian.definition_digest": definition, "martian.job_id": job.id}
            if job.suite_id is not None:
                metadata["martian.suite_id"] = job.suite_id
                metadata["martian.suite_version"] = job.suite_version
            answer = answers.get((scorer.model, scorer.endpoint))
            if scorer.kind == "llm_judge":
                metadata |= _judge_metadata(scorer, answer if isinstance(answer, JudgeResult) else None)
            if not finished:
                error = NO_OUTPUT
            elif scorer.kind != "llm_judge":
                value, rationale = code_score(scorer.name, output, expected)
            elif isinstance(answer, str):
                error = answer
            elif answer is not None and scorer.name in answer.verdicts:
                verdict = answer.verdicts[scorer.name]
                value, rationale, source_id = verdict.value, verdict.rationale, model_source(answer)
            elif answer is not None and scorer.name in answer.errors:
                error = answer.errors[scorer.name][:JUDGE_ERROR_LENGTH] or NO_JUDGE
            else:
                error = NO_JUDGE
            score = Score(
                run_id=run.id,
                case_id=result.case_id,
                trace_id=result.store.trace_id,
                scorer=scorer.name,
                scorer_version=scorer.version,
                source="code" if scorer.kind != "llm_judge" else "llm_judge",
                source_id=source_id,
                value=value,
                passed=value if isinstance(value, bool) else None,
                rationale=rationale,
                error=error,
                scoring_job_id=job.id,
                metadata=metadata,
                created_at=_now(),
            )
            if value is not None and result.store.trace_id:
                logged = await store.log_score(principal, project, result.store.trace_id, score)
                score = score.model_copy(update={"store": logged.store})
            scores.append(score)
        await records.put_scores(principal, project, scores)  # the case's checkpoint, in one transaction
        succeeded += sum(s.value is not None and not s.error for s in scores)
        errors += sum(s.value is None or bool(s.error) for s in scores)
        if await _advance_job(records, principal, project, job, {"succeeded": succeeded, "errors": errors}) is None:
            return
    current = await _job(records, principal, project, job.run_id, job.id)
    if current is None or current.state != "running":
        return
    await _move_job(
        records,
        principal,
        project,
        current,
        {
            "state": "cancelled" if current.cancel_requested_at is not None else job_outcome(succeeded, errors),
            "finished_at": _now(),
            "error": "Some assessments failed; saved results retained" if errors else "",
        },
        "running",
    )


# --- work whose worker stopped --------------------------------------------------------------


async def take_over(records: RecordStore, work: Work) -> None:
    """Queue ``work`` again when the worker that was running it stopped before it finished, such as
    one whose process died: a run or scoring job still ``running`` becomes ``queued``, so the
    worker taking it now runs it again. It resumes after the cases already finished: a run's cases
    that have a result are not called again, and a scoring job's cases that have its scores are not
    scored again. The case that was in flight has neither, so it runs again.

    Only the queue's worker calls it, while its claim holds the work, so no other worker is on it.
    A finished or queued record is left alone."""
    principal, project = work.principal, work.project_id
    if work.kind == "run":
        run = await records.get_run(principal, project, work.run_id)
        if run is not None and run.state == "running":
            await _move_run(records, principal, run, {"state": "queued"}, "running")
        return
    job = await _job(records, principal, project, work.run_id, work.job_id or "")
    if job is not None and job.state == "running":
        await _move_job(records, principal, project, job, {"state": "queued"}, "running")


async def interrupt(records: RecordStore, principal: Principal, run: Run, state: RunState) -> Run:
    """Finish ``run``, queued or running, which nothing is working on any more, as ``state``
    (``failed``, or ``cancelled`` when someone asked to stop it) with :data:`INTERRUPTED` as its
    error. Raises ``StoreError`` (``conflict``) when the run changed meanwhile."""
    now = _now()
    changes: dict[str, Any] = {"state": state, "error": INTERRUPTED, "finished_at": now}
    if state == "cancelled":
        changes["cancel_requested_at"] = run.cancel_requested_at or now
    return await records.update_run(principal, run.model_copy(update=changes), expected_state=run.state)


async def submit_or_fail(worker: Worker, records: RecordStore, work: Work) -> None:
    """``worker.submit(work)``, for a route that has just recorded the work ``queued``. If the worker
    refuses it or fails, the record is marked ``failed`` with :data:`NOT_ACCEPTED` and the error is
    raised again, so a failed submit never leaves a run or scoring job queued, which would block
    the next launch or scoring request."""
    try:
        await worker.submit(work)
    except Exception:
        await fail_unfinished(records, work, NOT_ACCEPTED)
        raise


async def fail_unfinished(records: RecordStore, work: Work, detail: str) -> None:
    """Mark ``work`` failed with ``detail`` when whatever ran it stopped before finishing it, such
    as a run's process that exited early: a run or scoring job still queued or running becomes
    ``failed``. A finished record is left alone."""
    principal, project = work.principal, work.project_id
    changes: dict[str, Any] = {"state": "failed", "error": detail, "finished_at": _now()}
    if work.kind == "run":
        run = await records.get_run(principal, project, work.run_id)
        if run is not None and run.state in ("queued", "running"):
            await _move_run(records, principal, run, changes, run.state)
        return
    job = await _job(records, principal, project, work.run_id, work.job_id or "")
    if job is not None and job.state in ("queued", "running"):
        await _move_job(records, principal, project, job, changes, job.state)
