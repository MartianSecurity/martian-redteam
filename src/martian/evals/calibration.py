"""A judge version's calibration: its agreement with people's labels, saved so that the product can
say when the judge was last checked, and every score which check applied to it.

- **Saved on request, never by itself.** A calibration is the judge version's item of the
  agreement report (:mod:`martian.evals.agreement`, ``GET /v1/evals/judge-agreement``) at the
  moment someone saves it: ``POST /v1/evals/judge-calibrations`` on the evals core, or
  :func:`save_calibration` from the SDK. Only a measured report is saved, one that rests on at
  least :data:`~martian.evals.agreement.MINIMUM_LABELLED` labelled items of a judge whose
  verdicts are compared (not free text). A registered judge version or a built-in model judge
  (``correctness`` and ``safety``, version ``v1``) can have one.
- **What it holds.** The report's figures, the labelled items it rests on, each with its label
  and the verdict compared with it, when it was saved, the model the judge version ran on then
  (its own, or the default judge model when it names none, as a scoring job admitted then would
  pin it) and the digest of the version's definition then. The record store keeps every
  calibration (:class:`~martian.evals.records.JudgeCalibration`); the newest of a judge version
  is its calibration.
- **Checked or unchecked.** A judge version is ``checked`` when its newest calibration was
  measured on the model it runs on now and with the definition it has now, and ``unchecked``
  otherwise, with the reason: ``none`` when it has no calibration; ``model_changed`` when the
  model it runs on is not the one its calibration was measured on, such as a judge that names no
  model after the default judge model changed; and
  ``definition_changed`` when what the version means is not what it meant then
  (:func:`judge_definition`), such as a built-in judge whose rubric a release of the SDK changed.
  A new version of a judge, with a new prompt, rubric or model, has no calibration until one is
  saved. The built-in ``policy`` judge takes its guidelines from each scoring request, so it has
  no one definition to calibrate: saving one is refused, and its scores name no calibration.
- **Re-checked on request.** A re-check grades again exactly the labelled items a calibration
  rests on (``JudgeCalibration.items``), with the judge version as it runs now, compares the new
  verdicts with the same labels and, when that is measured, saves the result as the version's
  newest calibration (``recheck_of`` names the one it re-checked). It calls a model for each item,
  so it is asked for explicitly, with the cost acknowledged: ``POST /v1/evals/judge-rechecks`` on
  the evals core, which the worker carries out, or :func:`recheck_calibration` from the SDK.
  Re-checking on a schedule is not part of the core.
- **Scores name the calibration that applied.** Every judge score the core makes records, under
  :data:`CALIBRATION_KEY` in its metadata (the assessment's ``provenance``), the id of the
  calibration that applied when it was made: the judge version's newest when the scoring job
  started, if it checks the version as the job runs it (its model and its definition), else
  :data:`NO_CALIBRATION`.
  A suite-file run's judges are checks of a suite file, not judge versions the core registers, so
  their scores name :data:`NO_CALIBRATION`.
- **A panel is calibrated as one judge.** A judge version graded by a panel of judge models
  (:mod:`martian.evals.panel`) has one calibration, of the panel's verdicts, measured on the
  panel's members (``JudgeCalibration.panel``); it checks the version while the version runs on
  exactly those members. Its members are not calibrated one by one: the panel's verdict, not a
  member's, is what a score and a gate use, and the members' errors are not independent, so their
  own error rates do not give the panel's.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from martian.evals.model import ScorerVersion, digest
from martian.evals.records import CalibrationRecheck, JudgeCalibration, RecordStore

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

CALIBRATION_KEY = "martian.calibration_id"
"""The metadata key of a judge score that names the calibration that applied when it was made."""

NO_CALIBRATION = "none"
"""What a judge score names under :data:`CALIBRATION_KEY` when no calibration applied."""

State = Literal["checked", "unchecked"]
Reason = Literal["none", "model_changed", "definition_changed"]

NOT_CALIBRATED = (
    "The policy judge takes its guidelines from each scoring request, so it has no one definition to calibrate; "
    "register the policy as a judge to calibrate it"
)
"""Why the built-in ``policy`` judge has no calibration."""

_NOT_DEFINITION = ("operation_id",)
"""Criteria that identify a registered version rather than define it."""


def calibratable(scorer: ScorerVersion) -> bool:
    """Whether a judge version has one definition, so that a calibration can check it: every one
    but the built-in ``policy`` judge, whose guidelines come with each scoring request."""
    from martian.evals.worker.scorers import is_builtin

    return not (is_builtin(scorer) and scorer.name == "policy")


def judge_definition(scorer: ScorerVersion) -> str:
    """The digest of what a judge version means, apart from the model it runs on: its kind and
    type, its parameters and criteria (a registered judge's instructions, kind, options and range),
    and for a built-in model judge the rubric the core's judge grades it by. A calibration records
    it, so that a version whose definition changed in place is no longer checked by it."""
    from martian.evals.worker.judges import RUBRICS
    from martian.evals.worker.scorers import is_builtin

    return digest(
        {
            "kind": scorer.kind,
            "type": scorer.type,
            "parameters": scorer.parameters,
            "criteria": {key: value for key, value in scorer.criteria.items() if key not in _NOT_DEFINITION},
            "rubric": RUBRICS.get(scorer.name) if is_builtin(scorer) else None,
        }
    )


def calibration_state(latest: JudgeCalibration | None, scorer: ScorerVersion) -> tuple[State, Reason | None]:
    """Whether a judge version, ``scorer`` pinned to the model it runs on now (or its panel's
    members), is checked by its newest calibration ``latest``, and why not."""
    if latest is None or not calibratable(scorer):
        return "unchecked", "none"
    if (latest.model, latest.endpoint, list(latest.panel)) != (scorer.model, scorer.endpoint, list(scorer.panel)):
        return "unchecked", "model_changed"
    if latest.definition != judge_definition(scorer):
        return "unchecked", "definition_changed"
    return "checked", None


def applied_calibration(latest: JudgeCalibration | None, scorer: ScorerVersion) -> str:
    """What a judge score made by ``scorer``, pinned to the model (or panel) it ran on, names under
    :data:`CALIBRATION_KEY`: the id of the judge version's newest calibration ``latest`` when it
    checks that model and definition, else :data:`NO_CALIBRATION`."""
    if latest is not None and calibration_state(latest, scorer)[0] == "checked":
        return latest.id
    return NO_CALIBRATION


def newest(calibrations: Iterable[JudgeCalibration]) -> dict[tuple[str, str], JudgeCalibration]:
    """The newest calibration of each judge version, by name and version, from calibrations listed
    newest first, as ``RecordStore.list_calibrations`` lists them."""
    found: dict[tuple[str, str], JudgeCalibration] = {}
    for calibration in calibrations:
        found.setdefault((calibration.judge, calibration.version), calibration)
    return found


async def latest_calibrations(
    records: RecordStore, principal: Principal, project_id: str, *, judge: str | None = None
) -> dict[tuple[str, str], JudgeCalibration]:
    """The newest calibration of each judge version of a project, or of one judge's versions."""
    return newest(await records.list_calibrations(principal, project_id, judge=judge))


def save_calibration(
    judge: str,
    version: str | int,
    *,
    data_dir: str | Path | None = None,
    project_id: str | None = None,
    request_id: str | None = None,
) -> JudgeCalibration:
    """Save a judge version's current agreement with people's labels as its calibration, in the
    local edition's data folder (``data_dir``, else ``MARTIAN_EVALS_HOME``, else
    ``~/.martian/evals``), as the single local user, in the default project unless ``project_id``
    names another. It is what ``POST /v1/evals/judge-calibrations`` does: the version's report is
    computed again from the folder's runs, reviews and traces, and saved with its labelled items,
    the model the version runs on now (the default judge model in the folder's ``settings.json``
    for a judge that names none) and its definition. ``request_id`` makes a repeated call return
    the same calibration; without one, each call saves a new one. No model is called.

    Raises :class:`~martian.evals.store.StoreError`: ``not_found`` for a judge version the project
    has not registered and that is no built-in model judge, or a project that does not exist;
    ``invalid`` for a report that is not measured (fewer than 30 labelled items, or a free-text
    judge) or the built-in ``policy`` judge; ``conflict`` for an archived project, or a
    ``request_id`` used for another judge version."""
    from martian.evals.judge_settings import FileJudgeSettings
    from martian.evals.local import SETTINGS_FILE, data_folder, open_stores
    from martian.evals.server.calibration import calibration_id_for, record_calibration
    from martian.evals.server.principal import SINGLE_USER
    from martian.evals.server.projection import project_key

    home = data_folder(data_dir)
    key = project_key(uuid.UUID(project_id) if project_id else None)
    store, records = open_stores(home)
    try:
        return asyncio.run(
            record_calibration(
                store,
                records,
                FileJudgeSettings(home / SETTINGS_FILE),
                SINGLE_USER,
                key,
                judge,
                str(version),
                calibration_id=calibration_id_for(request_id or str(uuid.uuid4())),
            )
        )
    finally:
        records.close()


def recheck_calibration(
    judge: str,
    version: str | int,
    *,
    acknowledge_model_cost: bool = False,
    data_dir: str | Path | None = None,
    project_id: str | None = None,
    request_id: str | None = None,
) -> CalibrationRecheck:
    """Re-check a judge version's calibration in the local edition's data folder (as
    :func:`save_calibration` finds it), in this process: grade again, with the SDK judge, exactly
    the labelled items the version's newest calibration rests on, on the model the version runs on
    now (the default judge model in the folder's ``settings.json`` for a judge that names none),
    compare the new verdicts with the same labels, and save the result as its newest calibration
    when it is measured. It is what ``POST /v1/evals/judge-rechecks`` asks the core's worker to do.

    Each item is a model call on the key in this process's environment, so the call is refused
    unless ``acknowledge_model_cost`` is True. ``request_id`` makes a repeated call return the
    same re-check. Returns the re-check, ``completed`` with ``result_id`` naming the new
    calibration, or ``failed`` with the reason in ``error``, the calibration then unchanged.

    Raises :class:`~martian.evals.store.StoreError` before any model call: ``invalid`` without the
    acknowledgement, or for a calibration that records no labelled items; ``not_found`` for a
    judge version with no calibration, or that the project has not registered; ``conflict`` for a
    re-check of the version that is still running, or a ``request_id`` used for another one."""
    from martian.evals.judge_settings import FileJudgeSettings
    from martian.evals.local import SETTINGS_FILE, data_folder, open_stores
    from martian.evals.server.calibration import admit_recheck, recheck_id_for
    from martian.evals.server.principal import SINGLE_USER
    from martian.evals.server.projection import project_key
    from martian.evals.worker import Work, sdk_judge
    from martian.evals.worker.recheck import execute_recheck

    home = data_folder(data_dir)
    key = project_key(uuid.UUID(project_id) if project_id else None)
    store, records = open_stores(home)

    async def recheck() -> CalibrationRecheck:
        admitted, created = await admit_recheck(
            store,
            records,
            FileJudgeSettings(home / SETTINGS_FILE),
            SINGLE_USER,
            key,
            judge,
            str(version),
            recheck_id=recheck_id_for(request_id or str(uuid.uuid4())),
            acknowledged=acknowledge_model_cost,
        )
        if created:
            work = Work(kind="recheck", principal=SINGLE_USER, project_id=key, run_id="", job_id=admitted.id)
            await execute_recheck(store, records, work, judge=sdk_judge())
        found = await records.get_recheck(SINGLE_USER, key, admitted.id)
        assert found is not None
        return found

    try:
        return asyncio.run(recheck())
    finally:
        records.close()


__all__ = [
    "CALIBRATION_KEY",
    "NOT_CALIBRATED",
    "NO_CALIBRATION",
    "Reason",
    "State",
    "applied_calibration",
    "calibratable",
    "calibration_state",
    "judge_definition",
    "latest_calibrations",
    "newest",
    "recheck_calibration",
    "save_calibration",
]
