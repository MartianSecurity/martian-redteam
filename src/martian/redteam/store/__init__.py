"""Red team runs in the evals core's record store.

A red team run is recorded as an eval run of kind ``redteam`` (:class:`martian.evals.model.Run`):

- each attempt is a case result, keyed by the attempt's id, with ``passed`` when the target resisted,
  ``failed`` on a hit and ``error`` when the attempt failed or no check could decide it;
- each check result is a score of that case: ``value`` is whether the check hit, ``passed`` the
  opposite, and its kind, level and evidence are in ``metadata``;
- what only red team has (probes, evidence, verifications, findings) is in the record store's red
  team tables (:class:`martian.evals.records.RedteamRecords`);
- the run's own red team fields (purpose, catalogue, counts, budget) are in its ``provenance``.

So the same store, worker, resume and run list serve both, and listings keep the two kinds apart.
The local edition's data folder holds them (:meth:`RedteamStore.local`); a server binds its own
record store. Needs the ``evals`` extra.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from martian.evals.model import CaseResult, CaseStatus, Page, Run, Score
from martian.evals.model import Counts as EvalCounts
from martian.evals.records import RecordStore, RedteamRecords
from martian.evals.server.principal import SINGLE_USER, Principal
from martian.evals.server.projection import DEFAULT_PROJECT
from martian.evals.store import StoreError
from martian.redteam.engine.budget import ResumeError
from martian.redteam.model import (
    Attempt,
    AttemptOutcome,
    CheckResult,
    Evidence,
    Finding,
    Probe,
    RedteamRun,
    Verification,
)

UNDETERMINED = "no check could decide the attempt"
"""The error of the case result of an undetermined attempt: never a pass, never a hit."""

_STATUS: dict[AttemptOutcome, CaseStatus] = {
    "hit": "failed",
    "resisted": "passed",
    "error": "error",
    "undetermined": "error",
    "stopped": "error",
}
_LISTS = ("probes", "attempts", "evidence", "verifications", "findings")
_ATTEMPT_IN_CASE = ("id", "probe_id", "input", "check_results")


def as_eval_run(run: RedteamRun) -> Run:
    """The run's record in the record store: an eval run of kind ``redteam``."""
    header = run.model_dump(mode="json", exclude=set(_LISTS))
    counts = run.counts
    return Run(
        id=run.id,
        project_id=run.project_id,
        kind="redteam",
        source="console" if run.source == "campaign" else run.source,
        state=run.state,
        counts=EvalCounts(
            cases=counts.attempts,
            passed=counts.resisted,
            failed=counts.hits,
            errors=counts.errors + counts.undetermined + counts.stopped,
        ),
        provenance={"redteam": header},
        created_at=run.started_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def as_case_result(run_id: str, position: int, attempt: Attempt) -> CaseResult:
    output = attempt.model_dump(mode="json", exclude=set(_ATTEMPT_IN_CASE))
    error = attempt.error or (UNDETERMINED if attempt.outcome == "undetermined" else "")
    if attempt.outcome == "stopped" and not error:
        error = f"stopped: the budget's {attempt.stopped_by} ran out"
    if attempt.outcome == "error" and not error:
        error = f"{attempt.error_kind or 'attempt'} error"
    return CaseResult(
        run_id=run_id,
        case_id=attempt.id,
        position=position,
        status=_STATUS[attempt.outcome],
        inputs={"probe_id": attempt.probe_id, "input": attempt.input},
        output=output,
        error=error,
        finished_at=attempt.finished_at,
    )


def as_score(run_id: str, attempt: Attempt, position: int, result: CheckResult) -> Score:
    return Score(
        run_id=run_id,
        case_id=attempt.id,
        trace_id=attempt.trace_id,
        scorer=result.check_id,
        source="llm_judge" if result.kind == "judge" else "code",
        value=result.hit,
        passed=None if result.hit is None else not result.hit,
        rationale=result.rationale,
        error=result.error,
        metadata={
            "redteam": True,
            "position": position,
            "kind": result.kind,
            "level": None if result.level is None else int(result.level),
            "executed": result.executed,
            "evidence_ids": result.evidence_ids,
            "detail": result.detail,
            "source": result.source,
            "verifier": result.verifier,
        },
    )


def _check_result(score: Score) -> CheckResult:
    meta = score.metadata
    return CheckResult(
        check_id=score.scorer,
        kind=meta["kind"],
        hit=score.value if isinstance(score.value, bool) else None,
        level=meta.get("level"),
        executed=meta.get("executed"),
        rationale=score.rationale,
        error=score.error,
        evidence_ids=list(meta.get("evidence_ids", [])),
        detail=dict(meta.get("detail", {})),
        source=meta.get("source", ""),
        verifier=meta.get("verifier", ""),
    )


def _attempt(result: CaseResult, scores: list[Score]) -> Attempt:
    mine = sorted(
        (score for score in scores if score.case_id == result.case_id and score.metadata.get("redteam")),
        key=lambda score: score.metadata.get("position", 0),
    )
    output: dict[str, Any] = dict(result.output or {})
    return Attempt.model_validate(
        {
            **output,
            "id": result.case_id,
            "probe_id": result.inputs["probe_id"],
            "input": result.inputs.get("input"),
            "check_results": [_check_result(score) for score in mine],
        }
    )


class RedteamStore:
    """Red team runs in a record store (:class:`martian.evals.records.RecordStore`)."""

    def __init__(self, records: RecordStore) -> None:
        self.records = records

    @classmethod
    def local(cls, home: str | Path | None = None) -> RedteamStore:
        """The local edition's store: ``martian.db`` in the data folder (``home``, else
        ``MARTIAN_EVALS_HOME``, else ``~/.martian/evals``), the record store ``martian ui`` serves."""
        from martian.evals.local import data_folder
        from martian.evals.records.sql import SqlRecordStore

        folder = data_folder(home)
        folder.mkdir(parents=True, exist_ok=True)
        return cls(SqlRecordStore(f"sqlite:///{folder / 'martian.db'}"))

    def close(self) -> None:
        close = getattr(self.records, "close", None)
        if close is not None:
            close()

    async def save(self, run: RedteamRun, *, principal: Principal = SINGLE_USER) -> RedteamRun:
        """Record the run, replacing an earlier record of the same id: its run, its attempts as case
        results with their checks as scores, and its probes, evidence, verifications and findings."""
        record = as_eval_run(run)
        existing = await self.records.get_run(principal, run.project_id, run.id)
        if existing is None:
            await self.records.create_run(principal, record)
        else:
            if existing.kind != "redteam":
                raise StoreError("conflict", "an eval run already has this id")
            await self.records.update_run(principal, record.model_copy(update={"created_at": existing.created_at}))
        results = [as_case_result(run.id, position, attempt) for position, attempt in enumerate(run.attempts)]
        scores = [
            as_score(run.id, attempt, position, result)
            for attempt in run.attempts
            for position, result in enumerate(attempt.check_results)
        ]
        if results:
            await self.records.put_results(principal, run.project_id, results, scores=scores)
        await self.records.put_redteam_records(
            principal,
            run.project_id,
            run.id,
            RedteamRecords(
                probes=[item.model_dump(mode="json") for item in run.probes],
                evidence=[item.model_dump(mode="json") for item in run.evidence],
                verifications=[item.model_dump(mode="json") for item in run.verifications],
                findings=[item.model_dump(mode="json") for item in run.findings],
            ),
        )
        return run

    async def claim_resume(self, earlier: RedteamRun, *, principal: Principal = SINGLE_USER) -> None:
        """Durably mark the verified checkpoint as running before any resumed model work."""
        conflict = "The saved run changed before it could resume. Read its current state and try again."
        saved = await self.records.get_run(principal, earlier.project_id, earlier.id)
        if (
            saved is None
            or saved.kind != "redteam"
            or saved.state != earlier.state
            or saved.finished_at != earlier.finished_at
        ):
            raise ResumeError(conflict)
        try:
            await self.records.update_run(
                principal,
                saved.model_copy(update={"state": "running", "finished_at": None}),
                expected_checkpoint=saved,
            )
        except StoreError as error:
            if error.kind not in {"conflict", "not_found"}:
                raise
            raise ResumeError(conflict) from None

    async def get(self, project_id: str, run_id: str, *, principal: Principal = SINGLE_USER) -> RedteamRun | None:
        """The red team run, rebuilt from its records; ``None`` when there is none of this id."""
        record = await self.records.get_run(principal, project_id, run_id)
        if record is None or record.kind != "redteam":
            return None
        results: list[CaseResult] = []
        offset = 0
        while True:
            page = await self.records.list_results(principal, project_id, run_id, offset=offset, limit=1000)
            results.extend(page.items)
            if page.next_cursor is None:
                break
            offset += len(page.items)
        scores = await self.records.list_scores(principal, project_id, run_id)
        extra = await self.records.get_redteam_records(principal, project_id, run_id)
        header = dict(record.provenance.get("redteam", {}))
        header.update(
            {
                "state": record.state,
                "finished_at": record.finished_at,
                "probes": [Probe.model_validate(item) for item in extra.probes],
                "attempts": [_attempt(result, scores) for result in results],
                "evidence": [Evidence.model_validate(item) for item in extra.evidence],
                "verifications": [Verification.model_validate(item) for item in extra.verifications],
                "findings": [Finding.model_validate(item) for item in extra.findings],
            }
        )
        return RedteamRun.model_validate(header)

    async def list(
        self,
        project_id: str = DEFAULT_PROJECT,
        *,
        principal: Principal = SINGLE_USER,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Run]:
        """The project's red team runs, newest first, as their run records. Eval runs are not listed."""
        return await self.records.list_runs(principal, project_id, limit=limit, cursor=cursor, kind="redteam")


def finished(run: RedteamRun) -> RedteamRun:
    """The run with its end time set, if it has none."""
    return run if run.finished_at else run.model_copy(update={"finished_at": datetime.now(UTC)})


__all__ = ["UNDETERMINED", "RedteamStore", "as_case_result", "as_eval_run", "as_score", "finished"]
