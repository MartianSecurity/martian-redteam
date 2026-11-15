"""A judge version's calibration: its agreement with people's labels, saved so that the product can
say when the judge was last checked, and every score which check applied to it.

- **Saved on request, never by itself.** A calibration is the judge version's item of the
  agreement report (:mod:`martian.evals.agreement`, ``GET /v1/evals/judge-agreement``) at the
  moment someone saves it: ``POST /v1/evals/judge-calibrations`` on the evals core, or
  :func:`save_calibration` from the SDK. Only a measured report is saved, one that rests on at
  least :data:`~martian.evals.agreement.MINIMUM_LABELLED` labelled items of a judge whose
  verdicts are compared (not free text). A registered judge version or a built-in model judge
  (``correctness``, ``safety`` and ``policy``, version ``v1``) can have one.
- **What it holds.** The report's figures, the number of labelled items, when it was saved and
  the model the judge version ran on then: its own, or the default judge model when it names none,
  as a scoring job admitted then would pin it. The record store keeps every calibration
  (:class:`~martian.evals.records.JudgeCalibration`); the newest of a judge version is its
  calibration.
- **Checked or unchecked.** A judge version is ``checked`` when its newest calibration was
  measured on the model it runs on now, and ``unchecked`` otherwise: ``none`` when it has no
  calibration, ``model_changed`` when the model it runs on is not the one its calibration was
  measured on, such as a judge that names no model after the default judge model changed. A new
  version of a judge, with a new prompt, rubric or model, has no calibration until one is saved.
- **Scores name the calibration that applied.** Every judge score the core makes records, under
  :data:`CALIBRATION_KEY` in its metadata (the assessment's ``provenance``), the id of the
  calibration that applied when it was made: the judge version's newest when the scoring job
  started, if it was measured on the model the job runs the judge on, else :data:`NO_CALIBRATION`.
  A suite-file run's judges are checks of a suite file, not judge versions the core registers, so
  their scores name :data:`NO_CALIBRATION`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from martian.evals.records import JudgeCalibration, RecordStore

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

CALIBRATION_KEY = "martian.calibration_id"
"""The metadata key of a judge score that names the calibration that applied when it was made."""

NO_CALIBRATION = "none"
"""What a judge score names under :data:`CALIBRATION_KEY` when no calibration applied."""

State = Literal["checked", "unchecked"]
Reason = Literal["none", "model_changed"]


def calibration_state(
    latest: JudgeCalibration | None, model: str | None, endpoint: str | None
) -> tuple[State, Reason | None]:
    """Whether a judge version that runs on ``model`` at ``endpoint`` now is checked by its newest
    calibration ``latest``, and why not."""
    if latest is None:
        return "unchecked", "none"
    if (latest.model, latest.endpoint) != (model, endpoint):
        return "unchecked", "model_changed"
    return "checked", None


def applied_calibration(latest: JudgeCalibration | None, model: str | None, endpoint: str | None) -> str:
    """What a judge score made on ``model`` at ``endpoint`` names under :data:`CALIBRATION_KEY`:
    the id of the judge version's newest calibration ``latest`` when it was measured on that
    model, else :data:`NO_CALIBRATION`."""
    if latest is not None and calibration_state(latest, model, endpoint)[0] == "checked":
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
    computed again from the folder's runs, reviews and traces, and saved with the model the version
    runs on now, the default judge model in the folder's ``settings.json`` for a judge that names
    none. ``request_id`` makes a repeated call return the same calibration; without one, each call
    saves a new one. No model is called.

    Raises :class:`~martian.evals.store.StoreError`: ``not_found`` for a judge version the project
    has not registered and that is no built-in model judge, or a project that does not exist;
    ``invalid`` for a report that is not measured (fewer than 30 labelled items, or a free-text
    judge); ``conflict`` for an archived project, or a ``request_id`` used for another judge
    version."""
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


__all__ = [
    "CALIBRATION_KEY",
    "NO_CALIBRATION",
    "Reason",
    "State",
    "applied_calibration",
    "calibration_state",
    "latest_calibrations",
    "newest",
    "save_calibration",
]
