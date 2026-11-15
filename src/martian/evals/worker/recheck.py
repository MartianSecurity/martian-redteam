"""Re-checking a judge version's calibration: grading again the labelled items it rests on, with the
version as it runs now, and saving the new agreement as its calibration
(:mod:`martian.evals.calibration`).

The worker carries out a ``queued`` :class:`~martian.evals.records.CalibrationRecheck`
(``Work(kind="recheck")``):

- **The same items and the same labels.** It reads the calibration the re-check names and grades
  again each of its labelled items (``JudgeCalibration.items``), and nothing else: a case result,
  by its run and case, with the case's request, expected response and saved output, or a trace of
  live traffic, by its id, with its request and response. Each new verdict is compared with the
  label the calibration saved, so only the judge differs.
- **The judge as it runs now.** Each item is graded by the re-check's ``scorer``, the judge version
  pinned to its model when the re-check was asked for, through the worker's judge, one request per
  item and model. A panel's members each grade the item and the strict majority decides
  (:mod:`martian.evals.panel`), as in a scoring job.
- **No verdict.** An item that is no longer stored, a case result with no saved output, and an
  item the judge gave no verdict, or a verdict of another kind than the label, are counted in
  ``errors`` and not compared. No call is retried.
- **The result.** With at least :data:`~martian.evals.agreement.MINIMUM_LABELLED` compared items,
  the re-check saves a new calibration of the version: the new report, every labelled item with
  its new verdict (none for an item that got none), the model and definition it graded with, and
  ``recheck_of`` naming the calibration it re-checked. It is then the version's newest
  calibration, and the re-check is ``completed`` with its id as ``result_id``. With fewer, the
  re-check is ``failed`` with the counts in its ``error``, and the calibration stays as it was.
- **Once.** Moving it from ``queued`` to ``running`` names the state it expects, so two workers
  never both take it, and the new calibration's id is derived from the re-check's. A re-check
  whose worker fails part way is marked ``failed``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from martian.evals.agreement import MINIMUM_LABELLED, JudgeAgreement, Kind, comparable, compare
from martian.evals.model import CaseResult, ScorerVersion, Value
from martian.evals.panel import member_sources
from martian.evals.records import CalibrationRecheck, JudgeCalibration, LabelledItem, RecordStore
from martian.evals.server.projection import derived_id, expected_text, prompt_text, text_of
from martian.evals.store import EvalStore, StoreError
from martian.evals.worker import Judge, JudgeRequest, JudgeResult, Work
from martian.evals.worker.execute import _detail, _judged, _now, _panel_score, all_results, model_source, seats

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

log = logging.getLogger("martian.evals.worker")

INTERRUPTED = "Re-check interrupted; the calibration is unchanged"
MISSING = "The calibration this re-check grades again is no longer saved"


def result_id_for(recheck_id: str) -> str:
    """The id of the calibration a re-check saves, derived from the re-check's."""
    return str(derived_id("judge-recheck-result", recheck_id))


async def execute_recheck(store: EvalStore, records: RecordStore, work: Work, *, judge: Judge | None) -> None:
    """Carry out a queued re-check. Without a judge, every item has no verdict, so it fails."""
    principal, project = work.principal, work.project_id
    recheck = await records.get_recheck(principal, project, work.job_id or "")
    if recheck is None or recheck.state != "queued":
        return
    try:
        recheck = await records.update_recheck(
            principal, project, recheck.model_copy(update={"state": "running"}), expected_state="queued"
        )
    except StoreError:
        return  # another worker took it
    try:
        await _recheck(store, records, principal, project, recheck, judge)
    except Exception as error:  # noqa: BLE001 - a re-check never stays running after its worker fails
        log.exception("Re-check %s failed", recheck.id)
        current = await records.get_recheck(principal, project, recheck.id)
        if current is not None and current.state == "running":
            await _advance(
                records, principal, project, current, {"state": "failed", "error": _detail(error, INTERRUPTED)}
            )


async def _advance(
    records: RecordStore, principal: Principal, project: str, recheck: CalibrationRecheck, changes: dict[str, Any]
) -> CalibrationRecheck | None:
    """Apply ``changes`` to the re-check while it is still running, or None when it is not."""
    if changes.get("state") in ("completed", "failed"):
        changes = {**changes, "finished_at": _now()}
    try:
        return await records.update_recheck(
            principal, project, recheck.model_copy(update=changes), expected_state="running"
        )
    except StoreError as error:
        if error.kind != "conflict":
            raise
        return None


class _Material:
    """The text of the labelled items, read once per run."""

    def __init__(self, store: EvalStore, records: RecordStore, principal: Principal, project: str) -> None:
        self._store, self._records = store, records
        self._principal, self._project = principal, project
        self._runs: dict[str, dict[str, CaseResult]] = {}

    async def of(self, item: LabelledItem) -> tuple[str, str, str] | None:
        """The request, expected response and output to grade, or None when there is none."""
        if item.trace_id is not None:
            trace = await self._store.get_trace(self._principal, self._project, item.trace_id)
            return (trace.request, "", trace.response) if trace is not None else None
        assert item.run_id is not None
        if item.run_id not in self._runs:
            run = await self._records.get_run(self._principal, self._project, item.run_id)
            results = await all_results(self._records, self._principal, run) if run is not None else []
            self._runs[item.run_id] = {result.case_id: result for result in results}
        result = self._runs[item.run_id].get(item.case_id or "")
        if result is None or result.status not in ("passed", "failed"):
            return None
        return prompt_text(result.inputs), expected_text(result.expectations), text_of(result.output)


async def _grade(
    judge: Judge, principal: Principal, scorer: ScorerVersion, recheck_id: str, text: tuple[str, str, str], what: str
) -> tuple[Value, list[str]]:
    """The judge version's verdict on one item, and the models that gave it, or no verdict."""
    prompt, expected, output = text
    answers: dict[tuple[str | None, str | None], JudgeResult | str | None] = {}
    for model, endpoint in seats(scorer):
        request = JudgeRequest(
            principal=principal,
            prompt=prompt,
            expected=expected,
            output=output,
            scorers=(scorer,),
            model=model,
            endpoint=endpoint,
            job_id=recheck_id,
        )
        answers[(model, endpoint)] = await _judged(judge, request, what)
    if scorer.panel:
        passed, _, error, _, metadata = _panel_score(scorer, answers)
        return (None, []) if error else (passed, member_sources(metadata))
    answer = answers.get((scorer.model, scorer.endpoint))
    if isinstance(answer, JudgeResult) and scorer.name in answer.verdicts:
        return answer.verdicts[scorer.name].value, [model_source(answer)]
    return None, []


async def _recheck(
    store: EvalStore,
    records: RecordStore,
    principal: Principal,
    project: str,
    recheck: CalibrationRecheck,
    judge: Judge | None,
) -> None:
    calibration = await records.get_calibration(principal, project, recheck.calibration_id)
    if calibration is None:
        await _advance(records, principal, project, recheck, {"state": "failed", "error": MISSING})
        return
    kind: Kind = calibration.report.get("kind") or "boolean"
    scorer, material = recheck.scorer, _Material(store, records, principal, project)
    items: list[LabelledItem] = []
    models: set[str] = set()
    judged = incomparable = errors = 0
    for position, item in enumerate(calibration.items, 1):
        verdict: Value = None
        text = await material.of(item)
        if text is not None and judge is not None:
            what = f"item {position} of re-check {recheck.id}"
            verdict, sources = await _grade(judge, principal, scorer, recheck.id, text, what)
            models.update(sources)
        if verdict is not None:
            judged += 1
            if not (comparable(kind, verdict) and comparable(kind, item.label)):
                incomparable, verdict = incomparable + 1, None
        errors += verdict is None
        items.append(item.model_copy(update={"verdict": verdict}))
        moved = await _advance(records, principal, project, recheck, {"graded": len(items) - errors, "errors": errors})
        if moved is None:
            return  # someone else finished it meanwhile
        recheck = moved
    compared = [(each.verdict, each.label) for each in items if each.verdict is not None]
    agreement = compare(compared, kind)
    if agreement.status != "measured":
        reason = (
            f"Only {len(compared)} of the {len(items)} labelled items got a verdict to compare, and "
            f"{MINIMUM_LABELLED} are needed; the calibration is unchanged"
        )
        await _advance(records, principal, project, recheck, {"state": "failed", "error": reason})
        return
    report = JudgeAgreement(
        judge=recheck.judge,
        version=recheck.version,
        models=tuple(sorted(models)),
        judged=judged,
        incomparable=incomparable,
        agreement=agreement,
        panel=bool(scorer.panel),
    )
    saved = await records.save_calibration(
        principal,
        project,
        JudgeCalibration(
            id=result_id_for(recheck.id),
            project_id=project,
            judge=recheck.judge,
            version=recheck.version,
            model=scorer.model,
            endpoint=scorer.endpoint,
            panel=list(scorer.panel),
            definition=recheck.definition,
            labelled=agreement.labelled,
            items=items,
            report=report.as_dict(),
            recheck_of=calibration.id,
            created_by=recheck.requested_by,
            created_at=_now(),
        ),
    )
    await _advance(records, principal, project, recheck, {"state": "completed", "result_id": saved.id})


__all__ = ["INTERRUPTED", "MISSING", "execute_recheck", "result_id_for"]
