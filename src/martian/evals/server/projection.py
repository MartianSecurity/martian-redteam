"""The shared model's objects in the response shapes of the frozen evals contract.

The server's routes read rows of its own tables and MLflow's JSON; the core's read the shared model
through the store and the record store. Every conversion between the two lives here, so the routes
stay a port of the server's and the mapping is in one place:

- The default project is the project ``DEFAULT_PROJECT``. The contract names it ``null``: a
  request without ``project_id`` reads it, and a run in it reports ``project_id: null``.
- A case result has no id of its own in the model. Its API ``id`` is a UUID derived from its run
  and case ids, and a score's ``id`` one derived from its run, case, scorer and scoring job, so
  each is the same in every response.
- A case's text (``prompt``, ``expected``, ``output``) is what the server shows: the first text
  under ``message``, ``prompt``, ``question`` or ``content`` of the inputs, and under
  ``expected_response``, ``answer`` or ``content`` of the expectations. Inputs or expectations
  with none of those keys are shown as their JSON.
- Traces are shown in MLflow's terms, as the contract's MLflow-shaped responses are: states such as
  ``OK``, assessment sources such as ``LLM_JUDGE``, and a ``TraceInfo`` built from the trace.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from martian.evals.model import CaseResult, Review, Run, Score, ScoringJob, Trace, TraceState, Value, digest
from martian.evals.server.schemas import EvalItemOut, EvalRunDetailOut, EvalRunOut

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


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _first_text(value: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        for key in keys:
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found
    return None


def prompt_text(inputs: dict[str, Any]) -> str:
    return _first_text(inputs, PROMPT_KEYS) or (_text(inputs) if inputs else "")


def expected_text(expectations: dict[str, Any]) -> str:
    raw: Any = expectations.get("expected_response", expectations)
    return _first_text(raw, EXPECTED_KEYS) or (_text(raw) if raw else "")


# --- runs and case results ------------------------------------------------------------------


def item_out(result: CaseResult) -> EvalItemOut:
    return EvalItemOut(
        id=result_id(result.run_id, result.case_id),
        dataset_item_id=result.case_id,
        position=result.position,
        status=result.status,
        prompt=prompt_text(result.inputs),
        expected=expected_text(result.expectations),
        output=_text(result.output),
        error=result.error,
        evals_trace_id=result.store.trace_id or "",
        evals_assessment_id=result.store.assessment_id or "",
        latency_ms=result.latency_ms,
        finished_at=result.finished_at,
    )


def _run_fields(run: Run) -> dict[str, Any]:
    target = run.target
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
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
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
            actor_id=review.reviewer,
            assignee_id=review.assignee,
            assessment_id=review.score_id,
            state=review.state,
            value=review.value,
            comment=review.comment,
            created_at=review.created_at,
        )

    def public(self) -> dict[str, Any]:
        return asdict(self)


def job_config(job: ScoringJob) -> dict[str, Any]:
    """The server's ``config`` of a scoring job: its scorers by name, and a digest of their pinned
    versions so two jobs compare only when they scored alike."""
    scorers = sorted(job.scorers, key=lambda scorer: scorer.name)
    return {
        "scorers": [scorer.name for scorer in job.scorers],
        "guidelines": "",
        "policies": [],
        "definition_digest": digest([scorer.model_dump(mode="json") for scorer in scorers]),
    }


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
        "cancel_requested_at": None,
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
        found["metadata"] = {key: _text(value) for key, value in score.metadata.items()}
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
