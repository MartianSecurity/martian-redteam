"""Each judge version's calibration (:mod:`martian.evals.calibration`): the core's own routes, which
the server's contract does not have.

- ``POST /v1/evals/judge-calibrations`` saves a judge version's current item of the agreement
  report (:func:`~martian.evals.server.agreement.agreement_reports`) as its calibration, with the
  labelled items it compared, the model the version runs on now and its definition. It takes a
  registered judge version of the project, or a built-in model judge (``correctness`` or
  ``safety`` at ``v1``); any other is ``404``, and the built-in ``policy`` judge, which has no one
  definition, is ``422``. A report that is not measured (fewer than 30 labelled items, or
  free-text verdicts) is ``422``. The calibration's id is derived from ``request_id``, so the same
  request again returns the same calibration, and the same ``request_id`` for another judge
  version is ``409``. It needs ``manageJudges``; an archived project takes none (``409``).
- ``GET /v1/evals/judge-calibrations`` lists every registered judge version of the project and
  the built-in model judges, each with the model it runs on now (or the members of its panel),
  its newest calibration and whether that calibration checks it, or why not. A judge graded by a
  panel is calibrated as one judge, on its panel's members (:mod:`martian.evals.calibration`).
- ``GET /v1/evals/judge-calibrations/{calibration_id}`` is one calibration, as a judge score's
  ``martian.calibration_id`` names it.
- ``POST /v1/evals/judge-rechecks`` asks for a re-check of a judge version's newest calibration:
  it records a ``queued`` re-check, with the version pinned to the model it runs on now, and
  submits it to the worker, which grades the calibration's labelled items again
  (:mod:`martian.evals.worker.recheck`). Each item is a model call, so the request needs
  ``acknowledge_model_cost`` (``422`` without it), a worker with a judge (``422``) and
  ``manageJudges``. A version with no calibration is ``404``; a calibration that records no
  labelled items, or the ``policy`` judge, is ``422``; a re-check of the version that is still
  queued or running is ``409``. Its id is derived from ``request_id``, as a calibration's is. It
  is the route a schedule of re-checks calls; the core keeps no schedule.
- ``GET /v1/evals/judge-rechecks/{recheck_id}`` is one re-check: its state, counts, error and the
  calibration it saved.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from martian.evals.agreement import MINIMUM_LABELLED, JudgeAgreement
from martian.evals.calibration import (
    NOT_CALIBRATED,
    calibratable,
    calibration_state,
    judge_definition,
    latest_calibrations,
)
from martian.evals.judge_settings import JudgeSettings
from martian.evals.model import ScorerVersion
from martian.evals.records import CalibrationRecheck, JudgeCalibration, LabelledItem, RecordStore
from martian.evals.server.agreement import agreement_reports
from martian.evals.server.dependencies import (
    JudgeSettingsDep,
    PrincipalDep,
    RecordsDep,
    StoreDep,
    WorkerDep,
    require_capability,
)
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id, project_key, project_out, user_out
from martian.evals.server.scoring import with_models
from martian.evals.server.suites import is_judge
from martian.evals.store import EvalStore, StoreError
from martian.evals.worker import Work
from martian.evals.worker.scorers import BUILTIN_VERSION, JUDGE_SCORERS, builtin_scorer

explorer = APIRouter(prefix="/v1/evals", tags=["evaluation explorer"])

NAME_PATTERN = r"^[a-z][a-z0-9_]{0,59}$"
NO_ACKNOWLEDGEMENT = "Model scoring requires cost acknowledgement"
NO_JUDGE = "Model judges require a judge configured for this installation's worker"
NOT_ACCEPTED = "The worker did not accept this re-check"
ACTIVE = ("queued", "running")


class CalibrationRequest(BaseModel):
    """Save a judge version's current agreement report as its calibration."""

    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(min_length=1, max_length=40)


class RecheckRequest(BaseModel):
    """Re-check a judge version's newest calibration: grade its labelled items again."""

    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(min_length=1, max_length=40)
    acknowledge_model_cost: bool = False


def calibration_id_for(request_id: str) -> str:
    """The id of the calibration a request saves, derived from its ``request_id``."""
    return str(derived_id("judge-calibration", request_id))


def recheck_id_for(request_id: str) -> str:
    """The id of the re-check a request asks for, derived from its ``request_id``."""
    return str(derived_id("judge-recheck", request_id))


def calibration_out(calibration: JudgeCalibration) -> dict[str, Any]:
    return {
        "id": calibration.id,
        "project_id": project_out(calibration.project_id),
        "judge": calibration.judge,
        "version": calibration.version,
        "model": calibration.model,
        "endpoint": calibration.endpoint,
        "panel": [member.model_dump(mode="json") for member in calibration.panel],
        "definition": calibration.definition,
        "labelled": calibration.labelled,
        "items": [item.model_dump(mode="json") for item in calibration.items],
        "traces_examined": calibration.traces_examined,
        "traces_truncated": calibration.traces_truncated,
        "report": calibration.report,
        "recheck_of": calibration.recheck_of,
        "actor_id": user_out(calibration.created_by) if calibration.created_by else None,
        "created_at": calibration.created_at,
    }


def recheck_out(recheck: CalibrationRecheck) -> dict[str, Any]:
    scorer = recheck.scorer
    return {
        "id": recheck.id,
        "project_id": project_out(recheck.project_id),
        "judge": recheck.judge,
        "version": recheck.version,
        "calibration_id": recheck.calibration_id,
        "model": scorer.model,
        "endpoint": scorer.endpoint,
        "panel": [member.model_dump(mode="json") for member in scorer.panel],
        "definition": recheck.definition,
        "state": recheck.state,
        "total": recheck.total,
        "graded": recheck.graded,
        "errors": recheck.errors,
        "error": recheck.error,
        "result_id": recheck.result_id,
        "actor_id": user_out(recheck.requested_by) if recheck.requested_by else None,
        "created_at": recheck.created_at,
        "finished_at": recheck.finished_at,
    }


def labelled_items(report: JudgeAgreement) -> list[LabelledItem]:
    """The labelled items a judge version's report compared, each with its label and verdict."""
    found = []
    for pair in report.pairs:
        kind, *ids = pair.item
        where = {"run_id": ids[0], "case_id": ids[1]} if kind == "case" else {"trace_id": ids[0]}
        found.append(LabelledItem(**where, label=pair.label, verdict=pair.verdict))
    return found


def _version_order(scorer: ScorerVersion) -> tuple[str, bool, int, str]:
    version = scorer.version
    return (scorer.name, not version.isdigit(), int(version) if version.isdigit() else 0, version)


async def judge_versions(store: EvalStore, principal: Principal, key: str, name: str | None) -> list[ScorerVersion]:
    """Every registered judge version of a project that exists, and the built-in model judges, or
    one judge's, by name and then version."""
    names = sorted({s.name for s in await store.list_scorers(principal, key) if is_judge(s)})
    found = [builtin_scorer(each) for each in JUDGE_SCORERS if name in (None, each)]
    for each in names:
        if name in (None, each):
            found += [v for v in await store.scorer_versions(principal, key, each) if is_judge(v)]
    return sorted(found, key=_version_order)


async def judge_version(
    store: EvalStore, principal: Principal, key: str, name: str, version: str
) -> ScorerVersion | None:
    """A registered judge version of a project that exists, or a built-in model judge."""
    if name in JUDGE_SCORERS and version == BUILTIN_VERSION:
        return builtin_scorer(name)
    versions = await store.scorer_versions(principal, key, name)
    return next((v for v in versions if is_judge(v) and v.version == version), None)


async def record_calibration(
    store: EvalStore,
    records: RecordStore,
    settings: JudgeSettings | None,
    principal: Principal,
    key: str,
    name: str,
    version: str,
    *,
    calibration_id: str,
) -> JudgeCalibration:
    """Save the judge version's current item of the project's agreement report as its
    calibration, with the labelled items it compared, the model it runs on now and its
    definition, or return the calibration already saved under ``calibration_id``. A refusal is a
    ``StoreError``."""
    project = await store.get_project(principal, key)
    if project is None:
        raise StoreError("not_found", "Not found")
    if existing := await records.get_calibration(principal, key, calibration_id):
        if (existing.judge, existing.version) != (name, version):
            raise StoreError("conflict", "request_id belongs to another calibration")
        return existing
    if project.archived_at is not None:
        raise StoreError("conflict", "This evaluation project is archived or unavailable")
    scorer = await judge_version(store, principal, key, name, version)
    if scorer is None:
        raise StoreError("not_found", "Judge version not found")
    if not calibratable(scorer):
        raise StoreError("invalid", NOT_CALIBRATED)
    reports, examined, truncated = await agreement_reports(store, records, principal, key, name)
    found = next((each for each in reports if each.version == version), None)
    item = found.as_dict() if found else None
    if item is not None and item["status"] == "not_comparable":
        raise StoreError("invalid", "Free-text verdicts are not compared, so this judge has no calibration")
    if found is None or item is None or item["status"] != "measured":
        labelled = item["labelled"] if item else 0
        raise StoreError("invalid", f"Too few labelled items to calibrate: {labelled} of the {MINIMUM_LABELLED} needed")
    (pinned,) = await with_models(settings, principal, [scorer])
    calibration = JudgeCalibration(
        id=calibration_id,
        project_id=key,
        judge=name,
        version=version,
        model=pinned.model,
        endpoint=pinned.endpoint,
        panel=list(pinned.panel),
        definition=judge_definition(pinned),
        labelled=item["labelled"],
        items=labelled_items(found),
        traces_examined=examined,
        traces_truncated=truncated,
        report=item,
        created_by=principal.user_id,
        created_at=datetime.now(UTC),
    )
    return await records.save_calibration(principal, key, calibration)


async def admit_recheck(
    store: EvalStore,
    records: RecordStore,
    settings: JudgeSettings | None,
    principal: Principal,
    key: str,
    name: str,
    version: str,
    *,
    recheck_id: str,
    acknowledged: bool,
) -> tuple[CalibrationRecheck, bool]:
    """Record a ``queued`` re-check of the judge version's newest calibration, with the version
    pinned to the model it runs on now, or return the re-check already recorded under
    ``recheck_id``; and whether it is new. No model is called. A refusal is a ``StoreError``."""
    if not acknowledged:
        raise StoreError("invalid", NO_ACKNOWLEDGEMENT)
    project = await store.get_project(principal, key)
    if project is None:
        raise StoreError("not_found", "Not found")
    if existing := await records.get_recheck(principal, key, recheck_id):
        if (existing.judge, existing.version) != (name, version):
            raise StoreError("conflict", "request_id belongs to another re-check")
        return existing, False
    if project.archived_at is not None:
        raise StoreError("conflict", "This evaluation project is archived or unavailable")
    scorer = await judge_version(store, principal, key, name, version)
    if scorer is None:
        raise StoreError("not_found", "Judge version not found")
    if not calibratable(scorer):
        raise StoreError("invalid", NOT_CALIBRATED)
    latest = (await latest_calibrations(records, principal, key, judge=name)).get((name, version))
    if latest is None:
        raise StoreError("not_found", "This judge version has no calibration to re-check")
    if not latest.items:
        raise StoreError("invalid", "This calibration records no labelled items to grade again; save a new one")
    running = await records.list_rechecks(principal, key, judge=name)
    if any(each.version == version and each.state in ACTIVE for each in running):
        raise StoreError("conflict", "A re-check of this judge version is already queued or running")
    (pinned,) = await with_models(settings, principal, [scorer])
    recheck = CalibrationRecheck(
        id=recheck_id,
        project_id=key,
        judge=name,
        version=version,
        calibration_id=latest.id,
        scorer=pinned,
        definition=judge_definition(pinned),
        state="queued",
        total=len(latest.items),
        requested_by=principal.user_id,
        created_at=datetime.now(UTC),
    )
    saved = await records.create_recheck(principal, key, recheck)
    # The same request made at the same time may have recorded it first; that one is submitted.
    return saved, saved == recheck


@explorer.get("/judge-calibrations", response_model=None)
async def judge_calibrations(
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    settings: JudgeSettingsDep,
    project_id: uuid.UUID | None = None,
    name: Annotated[str | None, Query(pattern=NAME_PATTERN)] = None,
) -> dict[str, Any]:
    key = project_key(project_id)
    if await store.get_project(principal, key) is None:
        if key != DEFAULT_PROJECT:
            raise HTTPException(404, "Not found")
        versions = [builtin_scorer(each) for each in JUDGE_SCORERS if name in (None, each)]
    else:
        versions = await judge_versions(store, principal, key, name)
    pinned = await with_models(settings, principal, versions)
    newest = await latest_calibrations(records, principal, key, judge=name)
    items = []
    for scorer in pinned:
        latest = newest.get((scorer.name, scorer.version))
        status, reason = calibration_state(latest, scorer)
        items.append(
            {
                "judge": scorer.name,
                "version": scorer.version,
                "model": scorer.model,
                "endpoint": scorer.endpoint,
                "panel": [member.model_dump(mode="json") for member in scorer.panel],
                "status": status,
                "reason": reason,
                "calibration": calibration_out(latest) if latest else None,
            }
        )
    return {"project_id": project_out(key), "minimum": MINIMUM_LABELLED, "items": items}


@explorer.post(
    "/judge-calibrations",
    status_code=201,
    response_model=None,
    dependencies=[Depends(require_capability("manageJudges"))],
)
async def save_judge_calibration(
    body: CalibrationRequest,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    settings: JudgeSettingsDep,
) -> dict[str, Any]:
    saved = await record_calibration(
        store,
        records,
        settings,
        principal,
        project_key(body.project_id),
        body.name,
        body.version,
        calibration_id=calibration_id_for(str(body.request_id)),
    )
    return calibration_out(saved)


@explorer.get("/judge-calibrations/{calibration_id}", response_model=None)
async def judge_calibration(
    calibration_id: uuid.UUID,
    principal: PrincipalDep,
    records: RecordsDep,
    project_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    found = await records.get_calibration(principal, project_key(project_id), str(calibration_id))
    if found is None:
        raise HTTPException(404, "Not found")
    return calibration_out(found)


@explorer.post(
    "/judge-rechecks",
    status_code=202,
    response_model=None,
    dependencies=[Depends(require_capability("manageJudges"))],
)
async def recheck_judge_calibration(
    body: RecheckRequest,
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    settings: JudgeSettingsDep,
    worker: WorkerDep,
) -> dict[str, Any]:
    if not worker.judges:
        raise HTTPException(422, NO_JUDGE)
    key = project_key(body.project_id)
    recheck, created = await admit_recheck(
        store,
        records,
        settings,
        principal,
        key,
        body.name,
        body.version,
        recheck_id=recheck_id_for(str(body.request_id)),
        acknowledged=body.acknowledge_model_cost,
    )
    if created:
        # Imported here: the worker's own modules import the server's, so the server's cannot
        # import them while the package loads.
        from martian.evals.worker.execute import fail_unfinished

        work = Work(kind="recheck", principal=principal, project_id=key, run_id="", job_id=recheck.id)
        try:
            await worker.submit(work)
        except Exception:
            await fail_unfinished(records, work, NOT_ACCEPTED)
            raise
    return recheck_out(recheck)


@explorer.get("/judge-rechecks/{recheck_id}", response_model=None)
async def judge_recheck(
    recheck_id: uuid.UUID,
    principal: PrincipalDep,
    records: RecordsDep,
    project_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    found = await records.get_recheck(principal, project_key(project_id), str(recheck_id))
    if found is None:
        raise HTTPException(404, "Not found")
    return recheck_out(found)


__all__ = [
    "CalibrationRequest",
    "RecheckRequest",
    "admit_recheck",
    "calibration_id_for",
    "calibration_out",
    "explorer",
    "judge_version",
    "judge_versions",
    "labelled_items",
    "recheck_id_for",
    "recheck_out",
    "record_calibration",
]
