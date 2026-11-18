"""An organisation's evals data as a folder of JSON Lines files, and that folder back into a
fresh install.

``martian evals export`` and ``martian evals import martian`` run these on the local edition's data
folder, as the single local user. An install that runs the evals core in its own process calls
:func:`export_data` and :func:`import_data` with its store, its record store and the
organisation's principal. Both work only through the store interfaces (``EvalStore`` and
``RecordStore``), so they work on every binding. ``docs/data-export.md`` is the format:

- One file per kind of object, each line one object in the shared model's JSON with the project
  it belongs to (an organisation's prompts belong to none), and ``manifest.json``, written last,
  with the format's name and version and each file's line count, size and SHA-256. Run artifacts
  are files named by their SHA-256.
- An export runs in steps: the organisation's prompts, then a project's definitions, a dataset
  version, a run, its monitoring (its monitors, schedules and thresholds with their history), a
  page of traces.
  After each step the files are flushed and ``export-progress.json`` records how long each file
  is. An interrupted export is resumed by exporting into the same folder again: the files are cut
  back to the last finished step and the export carries on from there.
- An import checks the manifest and every file first and refuses an install that already holds
  data. It writes every object through the interfaces, in an order that creates what an object
  refers to before the object. The receiving store assigns its own identifiers for what it
  stores (``StoreRef``, dataset version ids, assessment ids), and the import points every
  reference at them. Monitors and schedules come back paused, and a threshold's webhook comes
  back without its secret, which an export never holds. A judge calibration's re-check that was
  queued or running comes back failed (:data:`RECHECK_INTERRUPTED`): nothing works on it in the
  fresh install. Prompt versions keep their numbers, authors and times
  (``EvalStore.import_prompt``), so a run's ``provenance.prompts`` names the same versions.
- :data:`EXPORTED_TABLES` and :data:`EXPORTED_KINDS` name each table of the record store and each
  kind of object of the two interfaces that an export carries, and the files it is in;
  :data:`LEFT_OUT_TABLES` and :data:`LEFT_OUT_KINDS` name the rest, each with why. A test fails
  when a table or a kind is in neither, so a new one is exported (a new file: a new format
  version) or left out on purpose.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from martian._version import __version__
from martian.evals.model import (
    MODEL_VERSION,
    Case,
    CaseResult,
    DatasetDraft,
    DatasetVersion,
    Page,
    Project,
    Prompt,
    PromptVersion,
    QueueItem,
    Review,
    ReviewQuestion,
    ReviewQueue,
    Run,
    Score,
    ScorerVersion,
    ScoringJob,
    StoreRef,
    Suite,
    Trace,
    TraceQuery,
)
from martian.evals.records import (
    Alert,
    BlindQueue,
    CalibrationRecheck,
    DatasetManifest,
    JudgeCalibration,
    Monitor,
    MonitoredTrace,
    MonitorProgress,
    SavedView,
    Schedule,
    ScheduledRun,
    ScheduleProgress,
    Threshold,
    ThresholdStatus,
)
from martian.evals.store import StoreError

if TYPE_CHECKING:
    from martian.evals.records import RecordStore
    from martian.evals.records.sql import SqlRecordStore
    from martian.evals.server.principal import Principal
    from martian.evals.store import EvalStore
    from martian.evals.store.mlflow_local import MlflowStore

FORMAT = "martian-evals-export"
"""The format's name, in ``manifest.json``."""

VERSION = 7
"""The format's version, in ``manifest.json``. An import reads this version only. Version 2 added
the monitoring files: monitors, the traces they took, schedules, their slots, thresholds and their
alerts. Version 3 added the dataset publications (``manifests.jsonl``), version 4 the judge
calibrations and their re-checks (``calibrations.jsonl`` and ``rechecks.jsonl``), version 5 let
``scores.jsonl`` hold a score of each source for one case, scorer and scoring job (a person's answer
beside a check's or a judge's score of the same name), which an SDK that keeps one score per scorer
and scoring job would import as one, version 6 the organisation's prompts and their versions
(``prompts.jsonl`` and ``prompt_versions.jsonl``), and version 7 the review queues with blind double
review (``blind_queues.jsonl``)."""

MANIFEST = "manifest.json"
PLAN = "export-plan.json"
PROGRESS = "export-progress.json"
ARTIFACTS = "artifacts"

FILES = (
    "prompts.jsonl",
    "prompt_versions.jsonl",
    "projects.jsonl",
    "scorers.jsonl",
    "questions.jsonl",
    "datasets.jsonl",
    "cases.jsonl",
    "manifests.jsonl",
    "suites.jsonl",
    "drafts.jsonl",
    "draft_cases.jsonl",
    "views.jsonl",
    "runs.jsonl",
    "case_results.jsonl",
    "scores.jsonl",
    "scoring_jobs.jsonl",
    "reviews.jsonl",
    "queues.jsonl",
    "blind_queues.jsonl",
    "artifacts.jsonl",
    "traces.jsonl",
    "monitors.jsonl",
    "monitored_traces.jsonl",
    "schedules.jsonl",
    "scheduled_runs.jsonl",
    "thresholds.jsonl",
    "alerts.jsonl",
    "calibrations.jsonl",
    "rechecks.jsonl",
)
"""Every file of an export, in the order an import reads them."""

EXPORTED_TABLES = {
    "martian_runs": "runs.jsonl",
    "martian_case_results": "case_results.jsonl",
    "martian_scores": "scores.jsonl",
    "martian_scoring_jobs": "scoring_jobs.jsonl",
    "martian_reviews": "reviews.jsonl",
    "martian_suites": "suites.jsonl",
    "martian_dataset_drafts": "drafts.jsonl",
    "martian_dataset_draft_cases": "draft_cases.jsonl",
    "martian_saved_views": "views.jsonl",
    "martian_dataset_manifests": "manifests.jsonl",
    "martian_monitors": "monitors.jsonl",
    "martian_monitored_traces": "monitored_traces.jsonl",
    "martian_schedules": "schedules.jsonl",
    "martian_schedule_runs": "scheduled_runs.jsonl",
    "martian_thresholds": "thresholds.jsonl",
    "martian_threshold_alerts": "alerts.jsonl",
    "martian_judge_calibrations": "calibrations.jsonl",
    "martian_calibration_rechecks": "rechecks.jsonl",
    "martian_blind_queues": "blind_queues.jsonl",
}
"""Each table of the record store (``martian.evals.records.tables``) that an export carries, and
the file its rows are in."""

LEFT_OUT_TABLES = {
    "martian_work_queue": (
        "the worker's queue of runs and scoring jobs no worker has finished, and of monitors' and "
        "schedules' next turns: transient. A run exported queued or running is finished as "
        "interrupted when the local edition opens the data folder it was imported into; a monitor "
        "or a schedule is imported paused, and resuming it puts its turn back in the queue"
    ),
    "martian_suite_audits": (
        "the latest audit of each saved suite version: derived, not data of its own. It grades the "
        "built-in do-nothing and cheating replies to a published dataset with the suite version's "
        "checks, calling no target and no model, so auditing the imported suite version again over "
        "the same dataset (POST /v1/evals/suites/{suite_version_id}/audit) gives it back"
    ),
    "martian_deliveries": (
        "the outbox, store writes on their way from the record store to the store: transient. A "
        "delivered write is in the store and exported from there. A pending one is not carried: its "
        "score is exported with the record store's scores, but its trace or review queue item is "
        "exported as the store holds it, so let the outbox deliver before exporting"
    ),
    **{
        table: (
            "a red team run's probes, evidence, verifications or findings. Red team runs are not part of "
            "the export format: the export lists eval runs only, so a red team run, its case results and "
            "its scores stay out with these"
        )
        for table in (
            "martian_redteam_probes",
            "martian_redteam_evidence",
            "martian_redteam_verifications",
            "martian_redteam_findings",
        )
    },
}
"""Each table of the record store that an export leaves out, and why."""

EXPORTED_KINDS = {
    "Prompt": ("prompts.jsonl",),
    "PromptVersion": ("prompt_versions.jsonl",),
    "Project": ("projects.jsonl",),
    "ScorerVersion": ("scorers.jsonl",),
    "ReviewQuestion": ("questions.jsonl",),
    "DatasetVersion": ("datasets.jsonl",),
    "Case": ("cases.jsonl", "draft_cases.jsonl"),
    "Suite": ("suites.jsonl",),
    "DatasetDraft": ("drafts.jsonl",),
    "SavedView": ("views.jsonl",),
    "DatasetManifest": ("manifests.jsonl",),
    "Run": ("runs.jsonl",),
    "CaseResult": ("case_results.jsonl",),
    "Score": ("scores.jsonl", "traces.jsonl", "monitored_traces.jsonl"),
    "ScoringJob": ("scoring_jobs.jsonl",),
    "Review": ("reviews.jsonl",),
    "ReviewQueue": ("queues.jsonl",),
    "QueueItem": ("queues.jsonl",),
    "Trace": ("traces.jsonl",),
    "Monitor": ("monitors.jsonl",),
    "MonitorProgress": ("monitors.jsonl",),
    "MonitoredTrace": ("monitored_traces.jsonl",),
    "Schedule": ("schedules.jsonl",),
    "ScheduleProgress": ("schedules.jsonl",),
    "ScheduledRun": ("scheduled_runs.jsonl",),
    "Threshold": ("thresholds.jsonl",),
    "ThresholdStatus": ("thresholds.jsonl",),
    "Alert": ("alerts.jsonl",),
    "JudgeCalibration": ("calibrations.jsonl",),
    "CalibrationRecheck": ("rechecks.jsonl",),
    "BlindQueue": ("blind_queues.jsonl",),
}
"""Each kind of object the store (``EvalStore``) and the record store (``RecordStore``) take or
return, by model class, that an export carries, and the files it is in. Run artifacts are bytes,
not objects: ``artifacts.jsonl`` and ``artifacts/``. A threshold's webhook secret is not an object
either: the record store keeps it apart and the export never reads it (``threshold_secret``).
Prompts are the store's (MLflow's prompt registry), not the record store's, so they have no table."""

LEFT_OUT_KINDS = {
    "StoreStatus": "the store's readiness, not data",
    "Page": "one page of a listing; the export writes the objects listed",
    "TraceQuery": "a trace search, not data",
    "SessionSearch": "sessions grouped from a search's traces; the export writes the traces",
    "Counts": "a run's counts, written with its run",
    "SuiteAudit": "a suite version's latest audit, derived from the suite version and a dataset "
    "(martian_suite_audits in LEFT_OUT_TABLES)",
    "Delivery": "a store write on its way from the record store to the store (the outbox, left out)",
    "RedteamRecords": "a red team run's own records; red team runs are not part of the export format",
}
"""Each model class the two interfaces take or return that is not a kind of object an export
carries, and why."""

TRACE_PAGE = 500
"""Traces read per export step: a page of the store's trace search."""

PAGE = 1000
"""Cases or case results read per call, and the traces a monitor took, a schedule's slots or a
threshold's alerts."""

ORGANISATION = ("prompts.jsonl", "prompt_versions.jsonl")
"""The files whose lines are the organisation's, not a project's: they have no ``project_id``."""

WRITE_BATCH = 500
"""Case results or scores written per call on import."""

UNFINISHED = ("queued", "running")

RECHECK_INTERRUPTED = (
    "Interrupted: this re-check was queued or running when its data was exported, and nothing works on it "
    "in the install it was imported into; ask for it again"
)
"""The error of a judge calibration's re-check that an import finishes as failed: exported queued or
running, it would otherwise stay so, and block the next re-check of its judge version."""


class ExportError(Exception):
    """An export or import that cannot go on. The message says why."""


@dataclass(frozen=True)
class ExportSummary:
    """A finished export: its folder, whether it resumed an interrupted one, and its manifest."""

    folder: Path
    resumed: bool
    manifest: dict[str, Any]

    def lines(self, name: str) -> int:
        return int(self.manifest["files"][name]["lines"])


@dataclass(frozen=True)
class ImportSummary:
    """A finished import: the export's manifest, and for each ``StoreRef`` field (``dataset_id``
    also for dataset version ids) the identifiers of the source store and the receiving store's
    for the same objects. ``paused`` are the monitors and schedules imported paused, by id, that
    were active in the export, and ``needs_secret`` the thresholds whose webhook needs its secret
    given again (``POST /v1/evals/thresholds/{id}/secret``) before its alerts are signed."""

    folder: Path
    manifest: dict[str, Any]
    references: dict[str, dict[str, str]]
    paused: tuple[str, ...]
    needs_secret: tuple[str, ...]

    def lines(self, name: str) -> int:
        return int(self.manifest["files"][name]["lines"])


# --- writing an export ---------------------------------------------------------------------------


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


def export_line(project_id: str, **objects: Any) -> dict[str, Any]:
    """One line of an export: the project it belongs to, and each object in the shared model's
    JSON. ``martian evals push`` sends lines of the same shape."""
    return {"project_id": project_id, **{key: _json(value) for key, value in objects.items()}}


def _write_json(path: Path, value: Any) -> None:
    """Write ``path`` whole or not at all."""
    partial = path.with_name(path.name + ".partial")
    with partial.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


class _Files:
    """The export's files, appended to. ``sizes`` and ``lines`` are each file's length and line
    count; on opening, each file is cut back to the length given, the end of the last finished
    step."""

    def __init__(self, folder: Path, sizes: dict[str, int], lines: dict[str, int]) -> None:
        self.folder = folder
        self.sizes = dict(sizes)
        self.lines = dict(lines)
        self._handles: dict[str, IO[bytes]] = {}
        for name in FILES:
            path = folder / name
            path.touch()
            if path.stat().st_size < self.sizes[name]:
                raise ExportError(f"{path} is shorter than the export in progress wrote it; start a new export")
            os.truncate(path, self.sizes[name])
            self._handles[name] = path.open("ab")

    def write(self, name: str, line: dict[str, Any]) -> None:
        data = json.dumps(line, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        self._handles[name].write(data)
        self.sizes[name] += len(data)
        self.lines[name] += 1

    def artifact(self, data: bytes) -> str:
        """Keep a run artifact as ``artifacts/<sha256>``; the same content is kept once."""
        sha = hashlib.sha256(data).hexdigest()
        path = self.folder / ARTIFACTS / sha
        if not path.is_file():
            partial = path.with_name(sha + ".partial")
            with partial.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, path)
        return sha

    def commit(self) -> None:
        for handle in self._handles.values():
            handle.flush()
            os.fsync(handle.fileno())

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()


def _start(folder: Path, principal: Principal) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The plan and progress of the export in progress in ``folder``, or None to start one.
    A complete export, or a folder that holds anything else, is refused."""
    if (folder / MANIFEST).exists():
        raise ExportError(f"{folder} already holds a complete export; choose a new folder")
    if (folder / PROGRESS).is_file():
        try:
            plan = json.loads((folder / PLAN).read_text(encoding="utf-8"))
            progress = json.loads((folder / PROGRESS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ExportError(f"the export in progress in {folder} cannot be read; start a new export") from None
        if (plan.get("format"), plan.get("version")) != (FORMAT, VERSION):
            raise ExportError(f"{folder} holds an export in another format; start a new export")
        if plan.get("organisation_id") != principal.organisation_id:
            raise ExportError(f"the export in progress in {folder} is another organisation's")
        return plan, progress
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise ExportError(f"{folder} is not an empty folder and holds no export in progress; choose an empty folder")
    return None


async def _plan(store: EvalStore, records: RecordStore, principal: Principal) -> dict[str, Any]:
    """What the export covers: each project of the store with its dataset versions and runs."""
    projects = []
    for project in await store.list_projects(principal):
        runs: dict[str, None] = {}
        cursor = None
        while True:
            page = await records.list_runs(principal, project.id, limit=500, cursor=cursor)
            # Newest first: a run created meanwhile shifts the pages, so a run can come twice.
            runs.update(dict.fromkeys(run.id for run in page.items))
            if (cursor := page.next_cursor) is None:
                break
        datasets = [dataset.id for dataset in await store.list_datasets(principal, project.id)]
        projects.append({"id": project.id, "datasets": datasets, "runs": list(runs)})
    return {
        "format": FORMAT,
        "version": VERSION,
        "organisation_id": principal.organisation_id,
        "started_at": datetime.now(UTC).isoformat(),
        "projects": projects,
    }


def _steps(plan: dict[str, Any]) -> list[tuple[str, str, str]]:
    steps: list[tuple[str, str, str]] = [("prompts", "", "")]
    for project in plan["projects"]:
        steps.append(("project", project["id"], ""))
        steps += [("dataset", project["id"], dataset_id) for dataset_id in project["datasets"]]
        steps += [("run", project["id"], run_id) for run_id in project["runs"]]
        # Before the traces: every trace a monitor took is then among the traces exported.
        steps.append(("monitoring", project["id"], ""))
        steps.append(("traces", project["id"], ""))
    return steps


async def export_data(store: EvalStore, records: RecordStore, principal: Principal, folder: Path) -> ExportSummary:
    """Export everything the principal's organisation (or the single local user) holds in the
    store and the record store into ``folder``: a new or empty folder, or the folder of an export
    that was interrupted, which this resumes."""
    folder = folder.expanduser().resolve()
    found = _start(folder, principal)
    resumed = found is not None
    if found is None:
        (folder / ARTIFACTS).mkdir(parents=True, exist_ok=True)
        plan = await _plan(store, records, principal)
        progress: dict[str, Any] = {
            "step": 0,
            "cursor": None,
            "sizes": dict.fromkeys(FILES, 0),
            "lines": dict.fromkeys(FILES, 0),
        }
        _write_json(folder / PLAN, plan)
        _write_json(folder / PROGRESS, progress)
    else:
        plan, progress = found
        (folder / ARTIFACTS).mkdir(exist_ok=True)
    files = _Files(folder, progress["sizes"], progress["lines"])
    try:
        steps = _steps(plan)
        runs = {project["id"]: set(project["runs"]) for project in plan["projects"]}
        while progress["step"] < len(steps):
            kind, project_id, item = steps[progress["step"]]
            if kind == "traces":
                cursor = progress["cursor"]
                while True:
                    cursor = await _traces(store, principal, files, project_id, cursor, runs[project_id])
                    if cursor is None:
                        break
                    files.commit()
                    progress = {**progress, "cursor": cursor, "sizes": dict(files.sizes), "lines": dict(files.lines)}
                    _write_json(folder / PROGRESS, progress)
            elif kind == "prompts":
                await _prompts(store, principal, files)
            elif kind == "project":
                await _project(store, records, principal, files, project_id)
            elif kind == "dataset":
                await _dataset(store, principal, files, project_id, item)
            elif kind == "monitoring":
                await _monitoring(records, principal, files, project_id, runs[project_id])
            else:
                await _run(store, records, principal, files, project_id, item)
            files.commit()
            progress = {
                "step": progress["step"] + 1,
                "cursor": None,
                "sizes": dict(files.sizes),
                "lines": dict(files.lines),
            }
            _write_json(folder / PROGRESS, progress)
    finally:
        files.close()
    manifest = _finish(folder, plan, files)
    return ExportSummary(folder=folder, resumed=resumed, manifest=manifest)


async def _prompts(store: EvalStore, principal: Principal, files: _Files) -> None:
    """The organisation's prompts, by name, each with every version, oldest first."""
    for prompt in await store.list_prompts(principal):
        files.write("prompts.jsonl", {"prompt": _json(prompt)})
        for version in await store.prompt_versions(principal, prompt.name):
            files.write("prompt_versions.jsonl", {"version": _json(version)})


async def _project(
    store: EvalStore, records: RecordStore, principal: Principal, files: _Files, project_id: str
) -> None:
    """The project and what is defined in it: scorers, questions, suites, drafts, dataset
    publications and views, and its judges' calibrations and their re-checks, newest first."""
    project = await store.get_project(principal, project_id)
    if project is None:
        raise ExportError(f"project {project_id} is no longer in the store")
    files.write("projects.jsonl", export_line(project_id, project=project))
    for latest in await store.list_scorers(principal, project_id):
        for scorer in await store.scorer_versions(principal, project_id, latest.name):
            files.write("scorers.jsonl", export_line(project_id, scorer=scorer))
    for question in await store.list_questions(principal, project_id):
        files.write("questions.jsonl", export_line(project_id, question=question))
    for latest_suite in await records.list_suites(principal, project_id):
        for version in range(1, latest_suite.version + 1):
            suite = await records.get_suite(principal, project_id, latest_suite.id, version=version)
            if suite is not None:
                files.write("suites.jsonl", export_line(project_id, suite=suite))
    for draft in await records.list_drafts(principal, project_id):
        files.write("drafts.jsonl", export_line(project_id, draft=draft))
        offset = 0
        while True:
            page = await records.draft_cases(principal, project_id, draft.id, offset=offset, limit=PAGE)
            for case in page.items:
                files.write("draft_cases.jsonl", export_line(project_id, draft_id=draft.id, case=case))
            if page.next_cursor is None:
                break
            offset += PAGE
    for manifest in await records.list_publications(principal, project_id):
        files.write("manifests.jsonl", export_line(project_id, manifest=manifest))
    for view in await records.list_views(principal, project_id):
        files.write("views.jsonl", export_line(project_id, view=view))
    for calibration in await records.list_calibrations(principal, project_id):
        files.write("calibrations.jsonl", export_line(project_id, calibration=calibration))
    for recheck in await records.list_rechecks(principal, project_id):
        files.write("rechecks.jsonl", export_line(project_id, recheck=recheck))


async def _dataset(store: EvalStore, principal: Principal, files: _Files, project_id: str, dataset_id: str) -> None:
    dataset = await store.get_dataset(principal, project_id, dataset_id)
    if dataset is None:
        raise ExportError(f"dataset {dataset_id} of project {project_id} is no longer in the store")
    files.write("datasets.jsonl", export_line(project_id, dataset=dataset))
    offset = 0
    while True:
        page = await store.read_cases(principal, project_id, dataset_id, offset=offset, limit=PAGE)
        for case in page.items:
            files.write("cases.jsonl", export_line(project_id, dataset_id=dataset_id, case=case))
        if page.next_cursor is None:
            return
        offset += PAGE


def _chain_order(reviews: list[Review]) -> list[Review]:
    """The reviews with each one after the review it replaces, as an import adds them."""
    written: set[str] = set()
    ordered: list[Review] = []
    waiting = list(reviews)
    while waiting:
        ready = [r for r in waiting if r.previous_id is None or r.previous_id in written]
        if not ready:
            raise ExportError(f"review {waiting[0].id} replaces a review the record store does not hold")
        ordered += ready
        written.update(r.id for r in ready)
        waiting = [r for r in waiting if r.id not in written]
    return ordered


async def run_records(
    store: EvalStore,
    records: RecordStore,
    principal: Principal,
    write: Callable[[str, dict[str, Any]], None],
    project_id: str,
    run_id: str,
) -> Run | None:
    """Write the lines of the run itself, as the record store and the store hold it, and of its
    case results, scores and scoring jobs: ``write(file name, line)`` for each, in the files'
    order. An export writes them into its files, and ``martian evals push`` sends the same lines
    (:mod:`martian.evals.push`). Returns the run as the store holds it, or None."""
    run = await records.get_run(principal, project_id, run_id)
    if run is None:
        raise ExportError(f"run {run_id} of project {project_id} is no longer in the record store")
    native = await store.get_run(principal, project_id, run_id)
    write("runs.jsonl", export_line(project_id, run=run, store_run=native))
    offset = 0
    while True:
        page = await records.list_results(principal, project_id, run_id, offset=offset, limit=PAGE)
        for result in page.items:
            write("case_results.jsonl", export_line(project_id, result=result))
        if page.next_cursor is None:
            break
        offset += PAGE
    for score in await records.list_scores(principal, project_id, run_id):
        write("scores.jsonl", export_line(project_id, score=score))
    for job in await records.list_scoring_jobs(principal, project_id, run_id):
        write("scoring_jobs.jsonl", export_line(project_id, job=job))
    return native


async def _run(
    store: EvalStore, records: RecordStore, principal: Principal, files: _Files, project_id: str, run_id: str
) -> None:
    """The run in the record store and in the store, its case results, scores, scoring jobs,
    reviews, review queues and artifacts."""
    native = await run_records(store, records, principal, files.write, project_id, run_id)
    reviews = _chain_order(await records.list_reviews(principal, project_id, run_id, current_only=False))
    for review in reviews:
        files.write("reviews.jsonl", export_line(project_id, review=review))
    # The store cannot list queues: a run's queues are the ones its reviews name.
    for queue_id in dict.fromkeys(r.queue_id for r in reviews if r.queue_id is not None):
        queue = await store.get_queue(principal, project_id, queue_id)
        if queue is not None:
            items = await store.queue_items(principal, project_id, queue_id)
            files.write("queues.jsonl", export_line(project_id, queue=queue, items=items))
    for blind in await records.list_blind_queues(principal, project_id, run_id=run_id):
        files.write("blind_queues.jsonl", export_line(project_id, blind_queue=blind))
    if native is not None:
        for name in await store.list_artifacts(principal, project_id, run_id):
            data = await store.get_artifact(principal, project_id, run_id, name)
            if data is None:
                raise ExportError(f"artifact {name} of run {run_id} is no longer in the store")
            sha = files.artifact(data)
            files.write(
                "artifacts.jsonl", export_line(project_id, run_id=run_id, name=name, sha256=sha, size=len(data))
            )


async def _listed[T](read: Callable[..., Awaitable[Page[T]]], *args: Any) -> AsyncIterator[T]:
    """Every item of a record store listing, page by page: ``read(*args, limit=, cursor=)``."""
    cursor: str | None = None
    while True:
        page = await read(*args, limit=PAGE, cursor=cursor)
        for item in page.items:
            yield item
        if (cursor := page.next_cursor) is None:
            return


async def _monitoring(
    records: RecordStore, principal: Principal, files: _Files, project_id: str, runs: set[str]
) -> None:
    """The project's monitors with their progress and every trace each took, its schedules with
    their progress and every slot, and the thresholds on either with their status and every alert.
    Never a threshold's webhook secret: this does not read it. A slot whose run started after the
    export did stops it, as a trace of such a run does: the export would not hold that run."""
    for monitor in await records.list_monitors(principal, project_id):
        progress = await records.monitor_progress(principal, project_id, monitor.id)
        if progress is None:
            raise ExportError(f"monitor {monitor.id} of project {project_id} is no longer in the record store")
        files.write("monitors.jsonl", export_line(project_id, monitor=monitor, progress=progress))
        async for taken in _listed(records.list_monitored_traces, principal, project_id, monitor.id):
            files.write("monitored_traces.jsonl", export_line(project_id, trace=taken))
    for schedule in await records.list_schedules(principal, project_id):
        schedule_progress = await records.schedule_progress(principal, project_id, schedule.id)
        if schedule_progress is None:
            raise ExportError(f"schedule {schedule.id} of project {project_id} is no longer in the record store")
        files.write("schedules.jsonl", export_line(project_id, schedule=schedule, progress=schedule_progress))
        async for entry in _listed(records.list_scheduled_runs, principal, project_id, schedule.id):
            if entry.run_id is not None and entry.run_id not in runs:
                raise ExportError(
                    f"schedule {schedule.id} started run {entry.run_id}, which started after this export did. "
                    "Pause the project's schedules and export again into a new folder"
                )
            files.write("scheduled_runs.jsonl", export_line(project_id, scheduled_run=entry))
    for threshold in await records.list_thresholds(principal, project_id):
        status = await records.threshold_status(principal, project_id, threshold.id)
        if status is None:
            raise ExportError(f"threshold {threshold.id} of project {project_id} is no longer in the record store")
        files.write("thresholds.jsonl", export_line(project_id, threshold=threshold, status=status))
        async for alert in _listed(records.list_alerts, principal, project_id, threshold.id):
            files.write("alerts.jsonl", export_line(project_id, alert=alert))


async def _traces(
    store: EvalStore, principal: Principal, files: _Files, project_id: str, cursor: str | None, runs: set[str]
) -> str | None:
    """One page of the project's traces, oldest first, each with its spans and scores. Returns the
    cursor of the next page. A trace of a run the export does not cover stops it: an import could
    not record the trace without its run."""
    query = TraceQuery(project_id=project_id, oldest_first=True, limit=TRACE_PAGE, cursor=cursor)
    page = await store.search_traces(principal, query)
    for listed in page.items:
        trace = await store.get_trace(principal, project_id, listed.id)
        if trace is None:
            raise ExportError(f"trace {listed.id} of project {project_id} is no longer in the store")
        if trace.run_id is not None and trace.run_id not in runs:
            raise ExportError(
                f"trace {trace.id} belongs to run {trace.run_id}, which started after this export did. "
                "Export again into a new folder once no run is in progress"
            )
        files.write("traces.jsonl", export_line(project_id, trace=trace))
    return page.next_cursor


def _digest(path: Path) -> tuple[str, int, int]:
    """A file's SHA-256, size and line count."""
    sha, size, lines = hashlib.sha256(), 0, 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            sha.update(chunk)
            size += len(chunk)
            lines += chunk.count(b"\n")
    return sha.hexdigest(), size, lines


def _finish(folder: Path, plan: dict[str, Any], files: _Files) -> dict[str, Any]:
    """Write the manifest, which makes the export complete, and remove the progress files."""
    entries = {}
    for name in FILES:
        sha, size, lines = _digest(folder / name)
        if (size, lines) != (files.sizes[name], files.lines[name]):
            raise ExportError(f"{folder / name} changed while the export wrote it; start a new export")
        entries[name] = {"lines": lines, "bytes": size, "sha256": sha}
    stored: dict[str, int] = {}
    for _, line in _read(folder, "artifacts.jsonl"):
        stored[line["sha256"]] = line["size"]
    for leftover in (folder / ARTIFACTS).glob("*.partial"):
        leftover.unlink()
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "model_version": MODEL_VERSION,
        "sdk_version": __version__,
        "organisation_id": plan["organisation_id"],
        "started_at": plan["started_at"],
        "completed_at": datetime.now(UTC).isoformat(),
        "files": entries,
        "artifacts": {"files": len(stored), "bytes": sum(stored.values())},
    }
    _write_json(folder / MANIFEST, manifest)
    (folder / PROGRESS).unlink()
    (folder / PLAN).unlink()
    return manifest


# --- reading an export ---------------------------------------------------------------------------


def _read(folder: Path, name: str) -> Iterator[tuple[int, dict[str, Any]]]:
    with (folder / name).open("rb") as handle:
        for number, raw in enumerate(handle, 1):
            try:
                line = json.loads(raw)
            except ValueError:
                raise ExportError(f"{name} line {number} is not JSON") from None
            if not isinstance(line, dict):
                raise ExportError(f"{name} line {number} is not an object")
            if name not in ORGANISATION and not isinstance(line.get("project_id"), str):
                raise ExportError(f"{name} line {number} names no project")
            yield number, line


def _model[M: BaseModel](kind: type[M], value: Any, name: str, number: int) -> M:
    try:
        return kind.model_validate(value)
    except ValidationError as error:
        raise ExportError(f"{name} line {number} is not a valid {kind.__name__}: {error.errors()[0]['msg']}") from None


def _objects[M: BaseModel](folder: Path, name: str, key: str, kind: type[M]) -> Iterator[tuple[str, M]]:
    for number, line in _read(folder, name):
        yield line["project_id"], _model(kind, line.get(key), name, number)


def read_manifest(folder: Path) -> dict[str, Any]:
    """The manifest of the complete export in ``folder``, once every file it names is checked
    against it: present, of its size and line count, with its SHA-256. Raises :class:`ExportError`."""
    path = folder / MANIFEST
    if not path.is_file():
        if (folder / PROGRESS).is_file():
            raise ExportError(f"the export in {folder} is not finished; run the export into it again to finish it")
        raise ExportError(f"{folder} is not a Martian evals export: it has no {MANIFEST}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ExportError(f"{path} cannot be read as JSON") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ExportError(f"{folder} is not a Martian evals export: its manifest names another format")
    if manifest.get("version") != VERSION:
        raise ExportError(
            f"{folder} is an export of format version {manifest.get('version')}; this SDK reads version {VERSION}"
        )
    listed = manifest.get("files") or {}
    for name in FILES:
        entry = listed.get(name)
        if not isinstance(entry, dict) or not (folder / name).is_file():
            raise ExportError(f"{folder} is missing {name}")
        sha, size, lines = _digest(folder / name)
        if (sha, size, lines) != (entry.get("sha256"), entry.get("bytes"), entry.get("lines")):
            raise ExportError(f"{folder / name} is not the file the export wrote: its size or SHA-256 differs")
    for number, line in _read(folder, "artifacts.jsonl"):
        sha = str(line.get("sha256", ""))
        artifact = folder / ARTIFACTS / sha
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha) or not artifact.is_file():
            raise ExportError(f"artifacts.jsonl line {number} names an artifact the export does not hold")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != sha:
            raise ExportError(f"{artifact} is not the artifact the export wrote: its SHA-256 differs")
    return manifest


# --- importing an export ---------------------------------------------------------------------------


@dataclass
class References:
    """The receiving store's identifiers for the source store's, by ``StoreRef`` field. An import
    and a push (:mod:`martian.evals.server.push`) point references at the receiving store's with it."""

    maps: dict[str, dict[str, str]] = field(
        default_factory=lambda: {"experiment_id": {}, "run_id": {}, "dataset_id": {}, "assessment_id": {}}
    )

    def add(self, kind: str, old: str | None, new: str | None) -> None:
        if old is not None and new is not None:
            self.maps[kind][old] = new

    def get(self, kind: str, old: str | None) -> str | None:
        """The receiving store's identifier; one the source store could not resolve either is
        kept as it was."""
        return None if old is None else self.maps[kind].get(old, old)

    def ref(self, old: StoreRef) -> StoreRef:
        return StoreRef(
            experiment_id=self.get("experiment_id", old.experiment_id),
            run_id=self.get("run_id", old.run_id),
            dataset_id=self.get("dataset_id", old.dataset_id),
            trace_id=old.trace_id,
            assessment_id=self.get("assessment_id", old.assessment_id),
        )


def score_key(score: Score) -> str:
    """A score's content without its ``store`` references and its time, which a store may
    assign: the same score read back from two stores has the same key."""
    return json.dumps(score.model_dump(mode="json", exclude={"store", "created_at"}), sort_keys=True)


async def _holds_traces(store: EvalStore, principal: Principal, project_id: str) -> bool:
    """Whether the project holds a trace. A page of a trace search may hold none and still have a
    ``next_cursor``, so the search reads on until it finds one or ends."""
    query = TraceQuery(project_id=project_id, limit=1)
    while True:
        page = await store.search_traces(principal, query)
        if page.items:
            return True
        if page.next_cursor is None:
            return False
        query = query.model_copy(update={"cursor": page.next_cursor})


async def _refuse_data(store: EvalStore, records: RecordStore, principal: Principal, exported: list[Project]) -> None:
    """Refuse an install that holds data for the principal: an import goes into a fresh one."""
    present = {project.id: project for project in await store.list_projects(principal)}
    for project in exported:
        if (existing := present.get(project.id)) is not None and existing.name != project.name:
            raise ExportError(
                f"the install has a project {project.id} named {existing.name!r}, and the export's is {project.name!r}"
            )
    for project_id in sorted(set(present) | {project.id for project in exported}):
        found = []
        if (await records.list_runs(principal, project_id, limit=1)).items:
            found.append("runs")
        if await records.list_suites(principal, project_id):
            found.append("suites")
        if await records.list_drafts(principal, project_id):
            found.append("dataset drafts")
        if await records.list_publications(principal, project_id):
            found.append("dataset publications")
        if await records.list_views(principal, project_id):
            found.append("saved views")
        if await records.list_monitors(principal, project_id):
            found.append("monitors")
        if await records.list_schedules(principal, project_id):
            found.append("schedules")
        if await records.list_thresholds(principal, project_id):
            found.append("thresholds")
        if await records.list_calibrations(principal, project_id):
            found.append("judge calibrations")
        if await records.list_rechecks(principal, project_id):
            found.append("calibration re-checks")
        if project_id in present:
            if await store.list_datasets(principal, project_id):
                found.append("datasets")
            if await _holds_traces(store, principal, project_id):
                found.append("traces")
            if await store.list_scorers(principal, project_id):
                found.append("scorers")
            if await store.list_questions(principal, project_id):
                found.append("review questions")
        if found:
            raise ExportError(
                f"the install already holds data: project {project_id} has {', '.join(found)}. "
                "Import into a fresh install"
            )
    # Prompts are the organisation's, not a project's.
    if await store.list_prompts(principal):
        raise ExportError("the install already holds data: it has prompts. Import into a fresh install")


class _Groups[T]:
    """Consecutive lines of a file that belong to one object, such as a dataset version's cases."""

    def __init__(self, lines: Iterator[tuple[tuple[str, str], T]]) -> None:
        self._lines = lines
        self._next: tuple[tuple[str, str], T] | None = next(lines, None)

    def take(self, key: tuple[str, str]) -> list[T]:
        found = []
        while self._next is not None and self._next[0] == key:
            found.append(self._next[1])
            self._next = next(self._lines, None)
        return found

    def done(self) -> bool:
        return self._next is None


def _cases(folder: Path, name: str, key: str) -> _Groups[Case]:
    def lines() -> Iterator[tuple[tuple[str, str], Case]]:
        for number, line in _read(folder, name):
            yield (line["project_id"], str(line.get(key))), _model(Case, line.get("case"), name, number)

    return _Groups(lines())


def _batches[T](items: Iterator[T], size: int) -> Iterator[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


async def _put_batches[R: (CaseResult, Score)](
    folder: Path,
    name: str,
    key: str,
    kind: type[R],
    refs: References,
    write: Callable[[Principal, str, list[R]], Awaitable[None]],
    principal: Principal,
) -> None:
    """Write a file's case results or scores in batches, each pointing at the receiving store."""
    lines = ((p, o.model_copy(update={"store": refs.ref(o.store)})) for p, o in _objects(folder, name, key, kind))
    for batch in _batches(lines, WRITE_BATCH):
        for project_id in dict.fromkeys(p for p, _ in batch):
            await write(principal, project_id, [o for p, o in batch if p == project_id])


async def import_data(store: EvalStore, records: RecordStore, principal: Principal, folder: Path) -> ImportSummary:
    """Import the complete export in ``folder`` into the store and the record store, as the
    principal's organisation (or the single local user), which must hold no data yet. Every file
    is checked against the manifest before anything is written."""
    folder = folder.expanduser().resolve()
    return await _import(store, records, principal, folder, read_manifest(folder))


async def _import(
    store: EvalStore, records: RecordStore, principal: Principal, folder: Path, manifest: dict[str, Any]
) -> ImportSummary:
    projects = [project for _, project in _objects(folder, "projects.jsonl", "project", Project)]
    await _refuse_data(store, records, principal, projects)
    refs = References()
    await _import_prompts(store, principal, folder)

    # Projects, open until everything is written into them; then the archived ones are archived.
    for project in projects:
        created = await store.create_project(principal, project.model_copy(update={"archived_at": None}))
        refs.add("experiment_id", project.store.experiment_id, created.store.experiment_id)
    for project_id, scorer in _objects(folder, "scorers.jsonl", "scorer", ScorerVersion):
        registered = await store.register_scorer(principal, project_id, scorer)
        if registered.version != scorer.version:
            raise ExportError(f"scorer {scorer.name} of project {project_id} got version {registered.version}")
    for project_id, question in _objects(folder, "questions.jsonl", "question", ReviewQuestion):
        await store.save_question(principal, project_id, question)
    await _import_datasets(store, principal, folder, refs)
    await _import_manifests(records, principal, folder, refs)
    for project_id, suite in _objects(folder, "suites.jsonl", "suite", Suite):
        saved = await records.save_suite(principal, project_id, suite)
        if saved.version != suite.version:
            raise ExportError(f"suite {suite.id} of project {project_id} got version {saved.version}")
    await _import_runs(store, records, principal, folder, refs)
    await _import_traces(store, principal, folder, refs)
    await _put_batches(folder, "case_results.jsonl", "result", CaseResult, refs, records.put_results, principal)
    await _put_batches(folder, "scores.jsonl", "score", Score, refs, records.put_scores, principal)
    for project_id, job in _objects(folder, "scoring_jobs.jsonl", "job", ScoringJob):
        await records.create_scoring_job(principal, project_id, job)
    # A queue with blind double review stays blind: recorded before its reviews and its queue.
    for project_id, blind in _objects(folder, "blind_queues.jsonl", "blind_queue", BlindQueue):
        await records.add_blind_queue(principal, project_id, blind)
    for project_id, review in _objects(folder, "reviews.jsonl", "review", Review):
        await records.add_review(principal, project_id, review)
    await _import_queues(store, principal, folder)
    for number, line in _read(folder, "artifacts.jsonl"):
        data = (folder / ARTIFACTS / str(line["sha256"])).read_bytes()
        if not isinstance(line.get("run_id"), str) or not isinstance(line.get("name"), str):
            raise ExportError(f"artifacts.jsonl line {number} names no run or artifact")
        await store.put_artifact(principal, line["project_id"], line["run_id"], line["name"], data)
    await _import_drafts(records, principal, folder, refs)
    for project_id, view in _objects(folder, "views.jsonl", "view", SavedView):
        await records.save_view(principal, project_id, view)
    paused = await _import_monitors(records, principal, folder, refs)
    paused += await _import_schedules(records, principal, folder, refs)
    needs_secret = await _import_thresholds(records, principal, folder)
    await _import_calibrations(records, principal, folder)
    for project in projects:
        if project.archived_at is not None:
            await store.archive_project(principal, project.id)
    return ImportSummary(
        folder=folder, manifest=manifest, references=refs.maps, paused=paused, needs_secret=needs_secret
    )


async def _import_prompts(store: EvalStore, principal: Principal, folder: Path) -> None:
    """Each prompt with its versions as the export holds them: numbers, texts, commit messages,
    authors and times (``EvalStore.import_prompt``)."""

    def versions() -> Iterator[tuple[tuple[str, str], PromptVersion]]:
        for number, line in _read(folder, "prompt_versions.jsonl"):
            version = _model(PromptVersion, line.get("version"), "prompt_versions.jsonl", number)
            yield ("", version.name), version

    grouped = _Groups(versions())
    for number, line in _read(folder, "prompts.jsonl"):
        prompt = _model(Prompt, line.get("prompt"), "prompts.jsonl", number)
        found = grouped.take(("", prompt.name))
        try:
            stored = await store.import_prompt(principal, prompt, found)
        except StoreError as error:
            raise ExportError(f"prompt {prompt.name} did not import: {error}") from None
        if [(v.version, v.digest) for v in stored] != [(v.version, v.digest) for v in found]:
            raise ExportError(f"prompt {prompt.name} did not import whole")
    if not grouped.done():
        raise ExportError("prompt_versions.jsonl holds versions of a prompt prompts.jsonl does not list")


async def _import_datasets(store: EvalStore, principal: Principal, folder: Path, refs: References) -> None:
    """Each dataset version with its cases, in the order of the export, so each gets its version."""
    cases = _cases(folder, "cases.jsonl", "dataset_id")
    for project_id, dataset in _objects(folder, "datasets.jsonl", "dataset", DatasetVersion):
        found = cases.take((project_id, dataset.id))
        created = await store.create_dataset(principal, project_id, dataset.name, found)
        if (created.digest, created.version, created.case_count) != (
            dataset.digest,
            dataset.version,
            dataset.case_count,
        ):
            raise ExportError(f"dataset {dataset.name} v{dataset.version} of project {project_id} did not import whole")
        refs.add("dataset_id", dataset.id, created.id)
        refs.add("dataset_id", dataset.store.dataset_id, created.store.dataset_id)
        refs.add("experiment_id", dataset.store.experiment_id, created.store.experiment_id)
    if not cases.done():
        raise ExportError("cases.jsonl holds cases of a dataset version datasets.jsonl does not list")


async def _import_manifests(records: RecordStore, principal: Principal, folder: Path, refs: References) -> None:
    """Each dataset publication, with its start time; a published one is finished again as the
    receiving store's dataset version, so a run of that version is still checked against it."""
    for project_id, manifest in _objects(folder, "manifests.jsonl", "manifest", DatasetManifest):
        await records.begin_publication(principal, project_id, manifest)
        if manifest.state == "published" and manifest.dataset_id is not None:
            dataset_id = refs.get("dataset_id", manifest.dataset_id)
            await records.finish_publication(principal, project_id, manifest.id, str(dataset_id))


async def _import_runs(
    store: EvalStore, records: RecordStore, principal: Principal, folder: Path, refs: References
) -> None:
    """Each run in the store, then in the record store pointing at it."""
    for number, line in _read(folder, "runs.jsonl"):
        project_id = line["project_id"]
        run = _model(Run, line.get("run"), "runs.jsonl", number)
        if line.get("store_run") is not None:
            native = _model(Run, line["store_run"], "runs.jsonl", number)
            started = await store.start_run(principal, native)
            if native.state not in UNFINISHED and native.finished_at is not None:
                await store.finish_run(principal, project_id, native.id, state=native.state, counts=native.counts)
            refs.add("run_id", native.store.run_id, started.store.run_id)
            refs.add("experiment_id", native.store.experiment_id, started.store.experiment_id)
        await records.create_run(principal, run.model_copy(update={"store": refs.ref(run.store)}))


async def _import_traces(store: EvalStore, principal: Principal, folder: Path, refs: References) -> None:
    """Each trace with its spans and scores, linked to its run."""
    linked: dict[tuple[str, str], list[str]] = {}
    for project_id, trace in _objects(folder, "traces.jsonl", "trace", Trace):
        saved = await store.record_trace(principal, trace)
        stored: dict[str, list[Score]] = {}
        for score in saved.scores:
            stored.setdefault(score_key(score), []).append(score)
        for score in trace.scores:
            if matches := stored.get(score_key(score)):
                refs.add("assessment_id", score.store.assessment_id, matches.pop(0).store.assessment_id)
        if trace.run_id is not None:
            linked.setdefault((project_id, trace.run_id), []).append(trace.id)
    for (project_id, run_id), trace_ids in linked.items():
        for start in range(0, len(trace_ids), WRITE_BATCH):
            await store.link_traces(principal, project_id, run_id, trace_ids[start : start + WRITE_BATCH])


async def _import_queues(store: EvalStore, principal: Principal, folder: Path) -> None:
    for number, line in _read(folder, "queues.jsonl"):
        project_id = line["project_id"]
        queue = _model(ReviewQueue, line.get("queue"), "queues.jsonl", number)
        items = [_model(QueueItem, item, "queues.jsonl", number) for item in line.get("items") or []]
        traces = {item.case_id: item.trace_id for item in items}
        if set(traces) != set(queue.case_ids):
            raise ExportError(f"queues.jsonl line {number} does not give one trace per case of the queue")
        await store.create_queue(principal, project_id, queue, [traces[case_id] for case_id in queue.case_ids])
        for item in items:
            if item.status == "completed":
                await store.set_queue_item(principal, project_id, queue.id, item.trace_id, "completed")


async def _import_drafts(records: RecordStore, principal: Principal, folder: Path, refs: References) -> None:
    """Each draft with its cases, published again when it was."""
    cases = _cases(folder, "draft_cases.jsonl", "draft_id")
    for project_id, draft in _objects(folder, "drafts.jsonl", "draft", DatasetDraft):
        source = refs.get("dataset_id", draft.source_dataset_id)
        saved = await records.create_draft(
            principal, project_id, draft.model_copy(update={"source_dataset_id": source})
        )
        if found := cases.take((project_id, draft.id)):
            saved = await records.put_draft_cases(
                principal, project_id, draft.id, found, expected_revision=saved.revision
            )
        if draft.state == "published" and draft.published_digest is not None:
            await records.publish_draft(
                principal, project_id, draft.id, expected_revision=saved.revision, digest=draft.published_digest
            )
    if not cases.done():
        raise ExportError("draft_cases.jsonl holds cases of a draft drafts.jsonl does not list")


async def _import_monitors(
    records: RecordStore, principal: Principal, folder: Path, refs: References
) -> tuple[str, ...]:
    """Each monitor, paused, with its progress, then every trace each took, its scores pointing at
    the receiving store's. The ids of those that were active."""
    active = []
    for number, line in _read(folder, "monitors.jsonl"):
        project_id = line["project_id"]
        monitor = _model(Monitor, line.get("monitor"), "monitors.jsonl", number)
        progress = _model(MonitorProgress, line.get("progress"), "monitors.jsonl", number)
        # Paused: an import scores nothing. Resuming it scores the traces recorded from then on.
        await records.create_monitor(principal, project_id, monitor.model_copy(update={"state": "paused"}))
        await records.set_monitor_progress(principal, project_id, monitor.id, progress)
        if monitor.state == "active":
            active.append(monitor.id)
    for project_id, taken in _objects(folder, "monitored_traces.jsonl", "trace", MonitoredTrace):
        scores = [score.model_copy(update={"store": refs.ref(score.store)}) for score in taken.scores]
        await records.finish_monitored_trace(principal, project_id, taken.model_copy(update={"scores": scores}))
    return tuple(active)


async def _import_schedules(
    records: RecordStore, principal: Principal, folder: Path, refs: References
) -> tuple[str, ...]:
    """Each schedule, paused and pinned to the receiving store's dataset version, with its
    progress, then every slot it ran or skipped. The ids of those that were active."""
    active = []
    for number, line in _read(folder, "schedules.jsonl"):
        project_id = line["project_id"]
        schedule = _model(Schedule, line.get("schedule"), "schedules.jsonl", number)
        progress = _model(ScheduleProgress, line.get("progress"), "schedules.jsonl", number)
        # Paused: an import runs nothing. Resuming it runs the slots from then on.
        dataset_id = refs.get("dataset_id", schedule.dataset_id)
        await records.create_schedule(
            principal, project_id, schedule.model_copy(update={"state": "paused", "dataset_id": dataset_id})
        )
        await records.set_schedule_progress(principal, project_id, schedule.id, progress)
        if schedule.state == "active":
            active.append(schedule.id)
    for project_id, entry in _objects(folder, "scheduled_runs.jsonl", "scheduled_run", ScheduledRun):
        await records.add_scheduled_run(principal, project_id, entry)
    return tuple(active)


async def _import_thresholds(records: RecordStore, principal: Principal, folder: Path) -> tuple[str, ...]:
    """Each threshold with its status and its alerts as the export holds them, delivered or not.
    A webhook comes back without its secret, which the export does not hold. The ids of the
    thresholds with a webhook, whose secret is to be given again."""

    def alerts() -> Iterator[tuple[tuple[str, str], Alert]]:
        for number, line in _read(folder, "alerts.jsonl"):
            alert = _model(Alert, line.get("alert"), "alerts.jsonl", number)
            yield (line["project_id"], alert.threshold_id), alert

    grouped = _Groups(alerts())
    needs_secret = []
    for number, line in _read(folder, "thresholds.jsonl"):
        project_id = line["project_id"]
        threshold = _model(Threshold, line.get("threshold"), "thresholds.jsonl", number)
        status = _model(ThresholdStatus, line.get("status"), "thresholds.jsonl", number)
        await records.create_threshold(principal, project_id, threshold, webhook_secret=None)
        found = grouped.take((project_id, threshold.id))
        if not await records.set_threshold_status(
            principal, project_id, threshold.id, status, expected_sequence=0, alerts=found
        ):
            raise ExportError(f"threshold {threshold.id} of project {project_id} did not import whole")
        for alert in found:  # each as its last delivery attempt left it
            await records.finish_alert(principal, project_id, alert)
        if threshold.webhook is not None:
            needs_secret.append(threshold.id)
    if not grouped.done():
        raise ExportError("alerts.jsonl holds alerts of a threshold thresholds.jsonl does not list")
    return tuple(needs_secret)


async def _import_calibrations(records: RecordStore, principal: Principal, folder: Path) -> None:
    """Each judge calibration and each re-check as the export holds it, who made it and when
    included; a re-check that was queued or running is failed (:data:`RECHECK_INTERRUPTED`)."""
    for project_id, calibration in _objects(folder, "calibrations.jsonl", "calibration", JudgeCalibration):
        await records.save_calibration(principal, project_id, calibration)
    for project_id, recheck in _objects(folder, "rechecks.jsonl", "recheck", CalibrationRecheck):
        if recheck.state in UNFINISHED:
            changes = {"state": "failed", "error": RECHECK_INTERRUPTED, "finished_at": datetime.now(UTC)}
            recheck = recheck.model_copy(update=changes)
        await records.create_recheck(principal, project_id, recheck)


# --- the local edition -------------------------------------------------------------------------------


def _local_stores(home: Path) -> tuple[MlflowStore, SqlRecordStore]:
    from martian.evals.local import LocalEditionError, open_stores

    try:
        return open_stores(home)
    except LocalEditionError:
        raise
    except Exception as error:  # noqa: BLE001 - a folder may fail to open in many ways; say which
        raise LocalEditionError(f"could not open the local data folder {home}: {type(error).__name__}") from None


def export_local(home: Path, folder: Path) -> ExportSummary:
    """Export the local edition's data folder ``home`` into ``folder``, as the single local user,
    or resume the export in progress there."""
    from martian.evals.server.principal import SINGLE_USER

    if not home.is_dir():
        raise ExportError(f"there is no data folder at {home}")
    store, records = _local_stores(home)
    try:
        return asyncio.run(export_data(store, records, SINGLE_USER, folder))
    finally:
        records.close()


def import_local(folder: Path, home: Path) -> ImportSummary:
    """Import the export in ``folder`` into the local edition's data folder ``home``, which must
    hold no data yet. The export is checked before the data folder is opened or created."""
    from martian.evals.server.principal import SINGLE_USER

    folder = folder.expanduser().resolve()
    manifest = read_manifest(folder)
    store, records = _local_stores(home)
    try:
        return asyncio.run(_import(store, records, SINGLE_USER, folder, manifest))
    finally:
        records.close()


__all__ = [
    "EXPORTED_KINDS",
    "EXPORTED_TABLES",
    "FILES",
    "FORMAT",
    "LEFT_OUT_KINDS",
    "LEFT_OUT_TABLES",
    "RECHECK_INTERRUPTED",
    "VERSION",
    "ExportError",
    "ExportSummary",
    "ImportSummary",
    "References",
    "export_data",
    "export_line",
    "export_local",
    "import_data",
    "import_local",
    "read_manifest",
    "run_records",
    "score_key",
]
