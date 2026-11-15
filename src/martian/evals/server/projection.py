"""The shared model's objects in the response shapes of the frozen evals contract.

The server's routes read rows of its own tables and MLflow's JSON; the core's read the shared model
through the store and the record store. Every conversion between the two lives here, so the routes
stay a port of the server's and the mapping is in one place:

- The default project is the project ``DEFAULT_PROJECT``. The contract names it ``null``: a
  request without ``project_id`` reads it, and a run in it reports ``project_id: null``.
- A case result has no id of its own in the model. Its API ``id`` is a UUID derived from its run
  and case ids, and a score's ``id`` one derived from its run, case, scorer and scoring job, so
  each is the same in every response.
- A person is named by a UUID (``user_out``): their id when it is one, else one derived from it,
  so the local user ``local`` has an id the routes that take a user accept.
- A case's text (``prompt``, ``expected``, ``output``) is what the server shows: the first text
  under ``message``, ``prompt``, ``question`` or ``content`` of the inputs, and under
  ``expected_response``, ``answer`` or ``content`` of the expectations. Inputs or expectations
  with none of those keys are shown as their JSON. A conversation case's ``prompt`` is its first
  turn's, and its ``output`` the conversation as messages.
- Traces are shown in MLflow's terms, as the contract's MLflow-shaped responses are: states such as
  ``OK``, assessment sources such as ``LLM_JUDGE``, and a ``TraceInfo`` built from the trace.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from martian.evals.model import (
    CaseResult,
    Policy,
    Review,
    ReviewQueue,
    Run,
    Score,
    ScoringJob,
    Trace,
    TraceState,
    TurnResult,
    Value,
    digest,
)
from martian.evals.repeats import flaky, reliability
from martian.evals.server.schemas import EvalItemOut, EvalRunDetailOut, EvalRunOut
from martian.evals.stats import wilson

DEFAULT_PROJECT = "default"
"""The project a request without ``project_id`` reads: the contract's default project."""

NO_AGENT = uuid.UUID(int=0)
"""``agent_id`` of a run whose target is not a registered agent. The contract requires a UUID."""

_IDS = uuid.UUID("5d0f4c1e-7a0b-4e0f-9c39-2f3c9a6e1b47")
PROMPT_KEYS = ("message", "prompt", "question", "content")
EXPECTED_KEYS = ("answer", "content")
TRACE_STATE = {"ok": "OK", "error": "ERROR", "in_progress": "IN_PROGRESS", "unknown": "STATE_UNSPECIFIED"}
TRACE_STATE_BACK: dict[str, TraceState] = {"OK": "ok", "ERROR": "error", "IN_PROGRESS": "in_progress"}
SPAN_STATUS = {"ok": "STATUS_CODE_OK", "error": "STATUS_CODE_ERROR"}
SCORE_SOURCE = {"code": "CODE", "llm_judge": "LLM_JUDGE", "human": "HUMAN"}
RUN_STATUS = {
    "queued": "SCHEDULED",
    "running": "RUNNING",
    "completed": "FINISHED",
    "partial": "FINISHED",
    "failed": "FAILED",
    "cancelled": "KILLED",
}
USAGE_NOTE = "Native instrumented usage is shown when supplied; missing token/cost values remain unavailable."


def project_key(project_id: uuid.UUID | None) -> str:
    """The store's project id for a request's ``project_id``."""
    return DEFAULT_PROJECT if project_id is None else str(project_id)


def project_out(project_id: str) -> str | None:
    return None if project_id == DEFAULT_PROJECT else project_id


def result_id(run_id: str, case_id: str) -> uuid.UUID:
    """The API id of a case result."""
    return uuid.uuid5(_IDS, f"case-result\x00{run_id}\x00{case_id}")


def derived_id(kind: str, *parts: str) -> uuid.UUID:
    """A UUID for an object whose id the contract takes as a UUID, derived from what identifies
    it, such as the ``request_id`` that created it, so a repeated request finds the same object."""
    return uuid.uuid5(_IDS, "\x00".join((kind, *parts)))


def user_uuid(user_id: str) -> uuid.UUID:
    """A user id where the contract takes a UUID: the id itself when it is one, else one derived
    from it (the local edition's single user is ``local``)."""
    try:
        return uuid.UUID(user_id)
    except ValueError:
        return derived_id("user", user_id)


def user_out(user_id: str) -> str:
    """A person as the API names them: :func:`user_uuid` of their id, as text. Every user id a
    response carries is in this form, and a user id a request names is compared in it, so the id
    the API gives for the principal is one the routes that take a user (a UUID) accept. A UUID id
    is itself; the local user ``local`` is a UUID derived from it."""
    return str(user_uuid(user_id))


def suite_version_id(suite_id: str, version: int) -> uuid.UUID:
    """The API id of one version of a suite."""
    return derived_id("suite-version", suite_id, str(version))


def score_id(score: Score) -> uuid.UUID:
    """The API id of a stored score."""
    return uuid.uuid5(
        _IDS, f"score\x00{score.run_id}\x00{score.case_id}\x00{score.scorer}\x00{score.scoring_job_id or ''}"
    )


def iso(when: datetime | None) -> str | None:
    if when is None:
        return None
    return when.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def milliseconds(when: datetime | None) -> int | None:
    return None if when is None else int(when.timestamp() * 1000)


# --- case text ------------------------------------------------------------------------------


def text_of(value: Any) -> str:
    """A value as the server shows it: text as it is, anything else as JSON, nothing as empty."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def first_text(value: Any, keys: tuple[str, ...]) -> str | None:
    """``value`` when it is non-blank text, else the first non-blank text under ``keys``."""
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        for key in keys:
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found
    return None


def prompt_text(inputs: dict[str, Any]) -> str:
    return first_text(inputs, PROMPT_KEYS) or (text_of(inputs) if inputs else "")


def expected_text(expectations: dict[str, Any]) -> str:
    raw: Any = expectations.get("expected_response", expectations)
    return first_text(raw, EXPECTED_KEYS) or (text_of(raw) if raw else "")


# --- runs and case results ------------------------------------------------------------------


def item_out(result: CaseResult) -> EvalItemOut:
    return EvalItemOut(
        id=result_id(result.run_id, result.case_id),
        dataset_item_id=result.case_id,
        position=result.position,
        status=result.status,
        prompt=prompt_text(result.turns[0].inputs if result.turns else result.inputs),
        expected=expected_text(result.expectations),
        output=text_of(result.output),
        error=result.error,
        evals_trace_id=result.store.trace_id or "",
        evals_assessment_id=result.store.assessment_id or "",
        latency_ms=result.latency_ms,
        finished_at=result.finished_at,
    )


_ATTEMPT_VERDICT: dict[str, bool] = {"passed": True, "failed": False}


def attempt_verdicts(result: CaseResult) -> list[bool | None]:
    """A case result's attempts' verdicts in order: True passed, False failed, None no verdict."""
    return [_ATTEMPT_VERDICT.get(attempt.status) for attempt in result.attempts]


def reliability_out(results: Sequence[CaseResult]) -> dict[str, Any]:
    """The run's reliability for the core's own attempts route: the cases that passed every attempt
    (``passed``), failed one (``failed``) or are left open (``errors``), their share
    (``pass_rate``) with its 95% Wilson interval, and ``flaky``, the case ids whose verdict changed
    between attempts. Only results with attempts count; the cases count as independent, as in a
    scorecard."""
    cases = [(result.case_id, attempt_verdicts(result)) for result in results if result.attempts]
    found = reliability(cases)
    graded = found["passed"] + found["failed"]
    interval = wilson(found["passed"], graded)
    return {
        "passed": found["passed"],
        "failed": found["failed"],
        "errors": found["errors"],
        "pass_rate": found["passed"] / graded if graded else None,
        "confidence_95": list(interval) if interval else None,
        "flaky": found["flaky"],
    }


def attempts_out(result: CaseResult) -> dict[str, Any]:
    """A case result's attempts, for the core's own attempts route: each attempt's verdict, each
    check's verdict, the reply as text, the target error, latency and trace, and whether the
    case is flaky, its verdict changing between attempts. Empty, and not flaky, for a case that
    ran once. An attempt of a conversation case also has its ``session_id`` and its ``turns``."""
    attempts = []
    for attempt in result.attempts:
        found: dict[str, Any] = {
            "number": attempt.number,
            "status": attempt.status,
            "output": text_of(attempt.output),
            "error": attempt.error,
            "latency_ms": attempt.latency_ms,
            "trace_id": attempt.trace_id,
            "checks": {label: verdict.model_dump(mode="json") for label, verdict in attempt.checks.items()},
        }
        if attempt.turns:
            found |= {"session_id": attempt.session_id, "turns": [turn_out(turn) for turn in attempt.turns]}
        attempts.append(found)
    return {
        "id": result_id(result.run_id, result.case_id),
        "dataset_item_id": result.case_id,
        "position": result.position,
        "status": result.status,
        "flaky": flaky(attempt_verdicts(result)),
        "attempts": attempts,
    }


def turn_out(turn: TurnResult) -> dict[str, Any]:
    """One turn of a conversation, for the core's own routes: its verdict, its prompt, expected text
    and reply as the run detail shows a case's, the target error, latency, trace and each check's
    verdict on it."""
    return {
        "number": turn.number,
        "status": turn.status,
        "prompt": prompt_text(turn.inputs),
        "expected": expected_text(turn.expectations),
        "output": text_of(turn.output),
        "error": turn.error,
        "latency_ms": turn.latency_ms,
        "trace_id": turn.trace_id,
        "checks": {label: verdict.model_dump(mode="json") for label, verdict in turn.checks.items()},
    }


def conversation_out(result: CaseResult, scores: Sequence[Score]) -> dict[str, Any]:
    """A conversation case's result, for the core's own turns route: its verdict and each check's
    verdict on the whole conversation (``scores``, the run's own scores of the case), its session
    id, which its turns' traces share, its latency, and its turns. With repeats these are the
    attempt's that stands for the case; the attempts route has every attempt's."""
    own = [score for score in scores if score.scoring_job_id is None]
    return {
        "id": result_id(result.run_id, result.case_id),
        "dataset_item_id": result.case_id,
        "position": result.position,
        "status": result.status,
        "session_id": result.session_id,
        "latency_ms": result.latency_ms,
        "checks": {
            score.scorer: {"passed": score.passed, "rationale": score.rationale, "error": score.error} for score in own
        },
        "turns": [turn_out(turn) for turn in result.turns],
    }


def _run_fields(run: Run) -> dict[str, Any]:
    target = run.target
    # A worker that knows its targets by name pins the name with the target, as the local
    # edition's does; a registered agent's name is the server's.
    pinned = run.provenance.get("target")
    name = pinned.get("name") if isinstance(pinned, dict) else None
    return {
        "id": uuid.UUID(run.id),
        "run_code": run.code,
        "state": run.state,
        "dataset_name": run.dataset_name,
        "dataset_id": run.store.dataset_id or run.dataset_digest or "",
        "project_id": project_out(run.project_id),
        "max_items": run.max_cases if run.max_cases is not None else run.counts.cases,
        "planned_count": run.counts.cases,
        "provenance": dict(run.provenance),
        "truncated": run.truncated,
        "passed": run.counts.passed,
        "failed": run.counts.failed,
        "skipped": run.counts.skipped,
        "errored": run.counts.errors,
        "error": run.error,
        "agent_id": uuid.UUID(target.agent_id) if target and target.kind == "agent" and target.agent_id else NO_AGENT,
        "agent_name": name if isinstance(name, str) else None,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "cancel_requested_at": run.cancel_requested_at,
        "source": run.source,
        "ci": dict(run.provenance) if run.source == "ci" else None,
    }


def run_out(run: Run) -> EvalRunOut:
    return EvalRunOut(**_run_fields(run))


def run_detail(run: Run, results: list[CaseResult]) -> EvalRunDetailOut:
    return EvalRunDetailOut(**_run_fields(run), items=[item_out(result) for result in results])


def dataset_identity(run: Run) -> str | None:
    """What a paired comparison needs equal: the dataset's content digest, or its store id."""
    return run.dataset_digest or run.store.dataset_id


# --- scores, reviews and scoring jobs, as the server's rows -------------------------------------


@dataclass(frozen=True)
class Assessment:
    id: uuid.UUID
    item_id: uuid.UUID
    job_id: str | None
    name: str
    value: Value
    rationale: str
    source: str
    source_id: str
    provenance: dict[str, Any]
    error: str
    native_id: str | None
    telemetry_state: str
    created_at: datetime | None

    @classmethod
    def of(cls, score: Score) -> Assessment:
        return cls(
            id=score_id(score),
            item_id=result_id(score.run_id or "", score.case_id or ""),
            job_id=score.scoring_job_id,
            name=score.scorer,
            value=score.value,
            rationale=score.rationale,
            source=SCORE_SOURCE[score.source],
            source_id=score.source_id,
            provenance=dict(score.metadata),
            error=score.error,
            native_id=score.store.assessment_id,
            telemetry_state="persisted" if score.store.assessment_id else "unavailable",
            created_at=score.created_at,
        )

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewRow:
    id: str
    item_id: uuid.UUID
    queue_id: str | None
    criterion: str
    previous_id: str | None
    actor_id: str | None
    assignee_id: str | None
    assessment_id: str | None
    state: str
    value: Value
    comment: str
    created_at: datetime | None

    @classmethod
    def of(cls, review: Review) -> ReviewRow:
        return cls(
            id=review.id,
            item_id=result_id(review.run_id, review.case_id),
            queue_id=review.queue_id,
            criterion=review.criterion,
            previous_id=review.previous_id,
            actor_id=user_out(review.reviewer) if review.reviewer else None,
            assignee_id=user_out(review.assignee) if review.assignee else None,
            assessment_id=review.score_id,
            state=review.state,
            value=review.value,
            comment=review.comment,
            created_at=review.created_at,
        )

    def public(self) -> dict[str, Any]:
        return asdict(self)


def policy_out(policy: Policy) -> dict[str, Any]:
    """A policy as the server's criterion policy."""
    return {
        "name": policy.scorer,
        "direction": policy.direction,
        "threshold": policy.threshold,
        "minimum_coverage": policy.minimum_coverage,
    }


def job_config(job: ScoringJob) -> dict[str, Any]:
    """The server's ``config`` of a scoring job: its scorers by name, the policy judge's guidelines,
    the policies that gate it and the suite version it applies, and a digest of the pinned scorer
    versions and policies so two jobs compare only when they scored alike."""
    scorers = sorted(job.scorers, key=lambda scorer: scorer.name)
    policies = sorted(job.policies, key=lambda policy: policy.scorer)
    pinned: Any = [scorer.model_dump(mode="json") for scorer in scorers]
    if policies:
        pinned = {"scorers": pinned, "policies": [policy.model_dump(mode="json") for policy in policies]}
    config: dict[str, Any] = {
        "scorers": [scorer.name for scorer in job.scorers],
        "guidelines": next((str(s.criteria.get("guidelines", "")) for s in job.scorers if s.name == "policy"), ""),
        "policies": [policy_out(policy) for policy in policies],
        "definition_digest": digest(pinned),
    }
    if job.suite_id is not None and job.suite_version is not None:
        config["suite_id"] = job.suite_id
        config["suite_version"] = job.suite_version
        config["suite_version_id"] = str(suite_version_id(job.suite_id, job.suite_version))
    return config


def public_job(job: ScoringJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "run_id": job.run_id,
        "config": job_config(job),
        "state": job.state,
        "error": job.error,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "total": job.total,
        "succeeded": job.succeeded,
        "errors": job.errors,
        "cancel_requested_at": job.cancel_requested_at,
    }


def queue_out(queue: ReviewQueue) -> dict[str, Any]:
    """A review queue as the server's ``public_queue``: case results by their API ids."""
    return {
        "id": queue.id,
        "run_id": queue.run_id,
        "name": queue.name,
        "question": queue.questions[0].model_dump(mode="json"),
        "questions": [question.model_dump(mode="json") for question in queue.questions],
        "assignee_id": user_out(queue.assignee) if queue.assignee else None,
        "item_ids": [str(result_id(queue.run_id, case_id)) for case_id in queue.case_ids],
        "native_state": "persisted",
        "native_id": None,
        "created_at": queue.created_at,
    }


# --- traces ---------------------------------------------------------------------------------


def mlflow_assessment(score: Score, trace_id: str) -> dict[str, Any]:
    """A score as an MLflow feedback assessment."""
    feedback: dict[str, Any] = {"value": score.value}
    if score.error:
        feedback["error"] = {"error_message": score.error}
    found: dict[str, Any] = {
        "assessment_id": score.store.assessment_id,
        "assessment_name": score.scorer,
        "trace_id": trace_id,
        "source": {"source_type": SCORE_SOURCE[score.source], "source_id": score.source_id},
        "feedback": feedback,
        "create_time": iso(score.created_at),
        "last_update_time": iso(score.created_at),
        "valid": True,
    }
    if score.rationale:
        found["rationale"] = score.rationale
    if score.metadata:
        found["metadata"] = {key: text_of(value) for key, value in score.metadata.items()}
    return found


def trace_row(trace: Trace) -> dict[str, Any]:
    """One trace of a search result, in the server's columns."""
    return {
        "id": trace.id,
        "name": trace.name or "Trace " + trace.id[-8:],
        "state": TRACE_STATE[trace.state],
        "timestamp": iso(trace.request_time),
        "timestamp_ms": milliseconds(trace.request_time),
        "latency_ms": trace.latency_ms,
        "request": trace.request,
        "response": trace.response,
        "session_id": trace.session_id,
        "user_id": trace.user_id,
        "model": trace.model,
        "tokens": trace.tokens,
        "cost": trace.cost,
        "assessments": [
            {
                "id": score.store.assessment_id,
                "name": score.scorer,
                "value": score.value,
                "rationale": score.rationale,
                "source": {"source_type": SCORE_SOURCE[score.source], "source_id": score.source_id},
                "error": score.error or None,
            }
            for score in trace.scores
        ],
        "triage": _triage(trace.tags.get("martian.evals.triage")),
        "run_id": trace.run_id,
    }


def _triage(value: str | None) -> dict[str, Any] | None:
    try:
        found = json.loads(value) if value else None
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def trace_info(trace: Trace, experiment_id: str) -> dict[str, Any]:
    """The trace as MLflow's ``TraceInfo``."""
    metadata: dict[str, str] = dict(trace.metadata)
    for key, value in (
        ("mlflow.trace.session", trace.session_id),
        ("mlflow.trace.user", trace.user_id),
        ("mlflow.trace.model", trace.model),
        ("mlflow.sourceRun", trace.run_id),
    ):
        if value:
            metadata[key] = value
    if trace.tokens is not None:
        metadata["mlflow.trace.tokenUsage"] = json.dumps({"total_tokens": trace.tokens})
    if trace.cost is not None:
        metadata["mlflow.trace.cost"] = json.dumps({"total_cost": trace.cost})
    tags = dict(trace.tags)
    if trace.name:
        tags["mlflow.traceName"] = trace.name
    info: dict[str, Any] = {
        "trace_id": trace.id,
        "trace_location": {"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": experiment_id}},
        "request_time": iso(trace.request_time),
        "state": TRACE_STATE[trace.state],
        "request_preview": trace.request,
        "response_preview": trace.response,
        "trace_metadata": metadata,
        "tags": tags,
        "assessments": [mlflow_assessment(score, trace.id) for score in trace.scores],
    }
    if trace.latency_ms is not None:
        info["execution_duration"] = f"{trace.latency_ms / 1000}s"
    return info


def trace_view(trace: Trace, trace_id: str) -> dict[str, Any]:
    """A trace's spans as the Evals UI's span inspector reads them, at most 200, with the trace's
    usage. Refuses a trace whose spans are not one tree."""
    spans = trace.spans
    roots = [span for span in spans if not span.parent_id]
    if spans and len(roots) != 1:
        raise HTTPException(409, "Unexpected saved trace structure")
    root_start = (roots[0].start_ms or 0) if roots else 0
    rows: list[dict[str, Any]] = []
    for span in spans[:200]:
        fields = span.attributes
        start, end = span.start_ms or 0, span.end_ms or 0
        rows.append(
            {
                "id": span.id,
                "parent_id": span.parent_id or None,
                "name": span.name,
                "duration_ms": max(0, end - start),
                "offset_ms": max(0, start - root_start),
                "type": span.kind or fields.get("mlflow.spanType", "UNKNOWN"),
                "token_usage": fields.get("mlflow.chat.tokenUsage"),
                "cost": fields.get("mlflow.llm.cost"),
                "model": fields.get("gen_ai.request.model") or fields.get("mlflow.llm.model"),
                "retrieved_documents": fields.get("mlflow.retrievedDocuments"),
                "inputs": span.inputs,
                "outputs": span.outputs,
                "status": {"code": SPAN_STATUS.get(span.status, "STATUS_CODE_UNSET")},
                "attributes": fields,
                "events": [],
            }
        )
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise HTTPException(409, "Duplicate persisted span identity")
    for row in rows:
        seen = {row["id"]}
        parent = row["parent_id"]
        while parent in by_id:
            if parent in seen:
                raise HTTPException(409, "Cyclic saved trace structure")
            seen.add(parent)
            parent = by_id[parent]["parent_id"]
        row["depth"] = len(seen) - 1
    root = roots[0].attributes if roots else {}
    return {
        "trace_id": trace_id,
        "spans": rows,
        "token_usage": {"total_tokens": trace.tokens}
        if trace.tokens is not None
        else root.get("mlflow.chat.tokenUsage"),
        "estimated_cost": {"total_cost": trace.cost} if trace.cost is not None else root.get("mlflow.llm.cost"),
        "truncated": len(spans) > 200,
        "usage_note": USAGE_NOTE,
    }
