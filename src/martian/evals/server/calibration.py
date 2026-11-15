"""Each judge version's calibration (:mod:`martian.evals.calibration`): the core's own routes, which
the server's contract does not have.

- ``POST /v1/evals/judge-calibrations`` saves a judge version's current item of the agreement
  report (:func:`~martian.evals.server.agreement.project_agreement`) as its calibration, with the
  model the version runs on now. It takes a registered judge version of the project, or a built-in
  model judge (``correctness``, ``safety`` or ``policy`` at ``v1``); any other is ``404``. A report
  that is not measured (fewer than 30 labelled items, or free-text verdicts) is ``422``. The
  calibration's id is derived from ``request_id``, so the same request again returns the same
  calibration, and the same ``request_id`` for another judge version is ``409``. It needs
  ``manageJudges``; an archived project takes none (``409``).
- ``GET /v1/evals/judge-calibrations`` lists every registered judge version of the project and
  the built-in model judges, each with the model it runs on now (or the members of its panel),
  its newest calibration and whether that calibration checks it. A judge graded by a panel is
  calibrated as one judge, on its panel's members (:mod:`martian.evals.calibration`).
- ``GET /v1/evals/judge-calibrations/{calibration_id}`` is one calibration, as a judge score's
  ``martian.calibration_id`` names it.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from martian.evals.agreement import MINIMUM_LABELLED
from martian.evals.calibration import calibration_state, latest_calibrations
from martian.evals.judge_settings import JudgeSettings
from martian.evals.model import ScorerVersion
from martian.evals.records import JudgeCalibration, RecordStore
from martian.evals.server.agreement import project_agreement
from martian.evals.server.dependencies import (
    JudgeSettingsDep,
    PrincipalDep,
    RecordsDep,
    StoreDep,
    require_capability,
)
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id, project_key, project_out, user_out
from martian.evals.server.scoring import with_models
from martian.evals.server.suites import is_judge
from martian.evals.store import EvalStore, StoreError
from martian.evals.worker.scorers import BUILTIN_VERSION, JUDGE_SCORERS, builtin_scorer

explorer = APIRouter(prefix="/v1/evals", tags=["evaluation explorer"])

NAME_PATTERN = r"^[a-z][a-z0-9_]{0,59}$"


class CalibrationRequest(BaseModel):
    """Save a judge version's current agreement report as its calibration."""

    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(min_length=1, max_length=40)


def calibration_id_for(request_id: str) -> str:
    """The id of the calibration a request saves, derived from its ``request_id``."""
    return str(derived_id("judge-calibration", request_id))


def calibration_out(calibration: JudgeCalibration) -> dict[str, Any]:
    return {
        "id": calibration.id,
        "project_id": project_out(calibration.project_id),
        "judge": calibration.judge,
        "version": calibration.version,
        "model": calibration.model,
        "endpoint": calibration.endpoint,
        "panel": [member.model_dump(mode="json") for member in calibration.panel],
        "labelled": calibration.labelled,
        "traces_examined": calibration.traces_examined,
        "traces_truncated": calibration.traces_truncated,
        "report": calibration.report,
        "actor_id": user_out(calibration.created_by) if calibration.created_by else None,
        "created_at": calibration.created_at,
    }


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
    calibration, with the model it runs on now, or return the calibration already saved under
    ``calibration_id``. A refusal is a ``StoreError``."""
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
    report = await project_agreement(store, records, principal, key, name)
    item = next((each for each in report["items"] if each["version"] == version), None)
    if item is not None and item["status"] == "not_comparable":
        raise StoreError("invalid", "Free-text verdicts are not compared, so this judge has no calibration")
    if item is None or item["status"] != "measured":
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
        labelled=item["labelled"],
        traces_examined=report["traces_examined"],
        traces_truncated=report["traces_truncated"],
        report=item,
        created_by=principal.user_id,
        created_at=datetime.now(UTC),
    )
    return await records.save_calibration(principal, key, calibration)


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
        status, reason = calibration_state(latest, scorer.model, scorer.endpoint, scorer.panel)
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


__all__ = [
    "CalibrationRequest",
    "calibration_id_for",
    "calibration_out",
    "explorer",
    "judge_version",
    "judge_versions",
    "record_calibration",
]
