"""An MLflow tracking store copied into an organisation, so that ``martian ui`` shows what a team
kept in MLflow: its experiments, traces, evaluation datasets, assessments and evaluation runs.

``martian evals import mlflow <source>`` reads a tracking store as MLflow keeps it: a SQLite
database (``mlflow.db``), with the artifact folder its traces name, or a Postgres database. It
reads the source only, over a connection that cannot write, and copies it through the evals core's
store and record store into the local edition's data folder (:func:`copy_mlflow_store` takes any
organisation's stores).

The version check comes first, before anything is read or written. MLflow keeps its schema
revision in the source's ``alembic_version`` table. The import reads a store whose revision is the
one the installed mlflow-skinny writes, :func:`~martian.evals._mlflow.schema_head`, and refuses any
other: an older revision, because MLflow would have to migrate the store to read it, and the
import never changes the store it reads (an older MLflow cannot open a migrated store); a revision
this MLflow does not know, because a newer MLflow wrote it.

What maps where (``docs/eval-model.md`` lists every field):

- An experiment is a project, MLflow's ``Default`` experiment the default project.
- A trace is a trace with its spans, session and user, keeping MLflow's trace id.
- An assessment is a score on its trace: a person's feedback and every expectation are
  annotations, as the UI records them; a judge's or code's feedback is a score.
- An evaluation dataset is a dataset version in the default project, where the core keeps the
  organisation's datasets; each record is a case under the record's id.
- An MLflow run with traces is a run, each trace a case, its judges' and code's feedback the
  case's scores. Its id is MLflow's run id as a UUID.

Ids are derived from MLflow's, so copying the same store again copies only what is new and
reports the rest as already copied. Everything is read before anything is written, so a source
that cannot be read writes nothing.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlsplit

from mlflow.entities import Feedback, ViewType
from mlflow.exceptions import MlflowException

from martian._version import __version__
from martian.evals._digits import ascii_digits
from martian.evals._mlflow import (
    AssessmentMetadataKey,
    ReadOnlyTrackingStore,
    has_every_table,
    load_dataset_records,
    schema_head,
    schema_history,
    trace_artifact_file,
    trace_from_file,
)
from martian.evals.model import (
    Case,
    CaseResult,
    CaseStatus,
    Counts,
    Project,
    Run,
    RunState,
    Score,
    ScorerVersion,
    ScoreSource,
    StoreRef,
    Target,
    Trace,
    Value,
    digest,
)
from martian.evals.server.principal import SINGLE_USER, Principal
from martian.evals.server.projection import DEFAULT_PROJECT, derived_id
from martian.evals.store import StoreError
from martian.evals.store.mlflow_local import trace_from_mlflow

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from martian.evals.records import RecordStore
    from martian.evals.store import EvalStore

DEFAULT_EXPERIMENT = "0"
"""MLflow's ``Default`` experiment, which is copied into the default project."""

DEFAULT_WORKSPACE = "default"
"""The MLflow workspace a store without workspaces keeps everything in."""

PROJECT_LIMIT = 25
"""The most projects an organisation has besides the default one, as ``POST /projects`` allows."""

SOURCE = "mlflow_source."
"""The prefix of the trace metadata and score metadata keys that say where in MLflow an object was."""

RUN_STATES: dict[str, RunState] = {"FINISHED": "completed", "FAILED": "failed", "KILLED": "cancelled"}
"""MLflow's run status as the run's state. A run still ``RUNNING`` or ``SCHEDULED`` is skipped."""

SCORE_SOURCES: dict[str, ScoreSource] = {"HUMAN": "human", "LLM_JUDGE": "llm_judge", "CODE": "code"}
"""MLflow's assessment source types; any other is ``code``."""

VERDICTS = {"yes": True, "no": False}
"""MLflow judges' categorical answers as verdicts; ``unknown`` has none."""

COUNTED = {
    "scorers": "registered scorer",
    "label_schemas": "labeling schema",
    "review_queues": "review queue",
    "logged_models": "logged model",
    "registered_models": "registered model or prompt",
}
"""MLflow objects the core has no place for, counted in the report as not copied."""

_MLFLOW_TRACE_ID = re.compile(r"^tr-[0-9a-f]{32}$")
_MLFLOW_RUN_ID = re.compile(r"^[0-9a-f]{32}$")


class MlflowSourceError(Exception):
    """A source this import does not read, or a copy it cannot make. The message says why; nothing
    has been written."""


# --- the source and its version check ------------------------------------------------------------


@dataclass(frozen=True)
class SourceCheck:
    """What the version check found: the source, its schema revision and the revision this MLflow
    reads."""

    source: str
    """The source's URL, without a password."""
    dialect: Literal["sqlite", "postgresql"]
    revision: str
    head: str
    mlflow: str
    """The mlflow-skinny release that read the source."""


def source_url(source: str) -> str:
    """The SQLAlchemy URL of ``source``: a database URL as given (``postgresql://`` read with
    psycopg, the driver ``martian-sdk[postgres]`` installs), or a SQLite database file's path.
    Raises :class:`MlflowSourceError` for anything else."""
    scheme = urlsplit(source).scheme.lower()
    if scheme in ("http", "https", "databricks"):
        raise MlflowSourceError(
            f"{_without_password(source)} is an MLflow server: copying over its REST API is not supported. "
            "Copy from the database the server keeps instead, its --backend-store-uri"
        )
    if scheme.split("+")[0] == "postgresql":
        return "postgresql+psycopg" + source[len(scheme) :] if scheme == "postgresql" else source
    if scheme == "sqlite":
        return source
    if scheme and len(scheme) > 1:  # a Windows drive letter is a one-letter scheme
        raise MlflowSourceError(
            f"{_without_password(source)}: give a SQLite database file, a sqlite:/// URL or a postgresql:// URL "
            "of an MLflow tracking store"
        )
    path = Path(source).expanduser()
    if path.is_dir():
        raise MlflowSourceError(
            f"{path} is a folder. Give the tracking store's database file, such as {path / 'mlflow.db'}; "
            "MLflow's file store (an mlruns folder without a database) is not supported"
        )
    if not path.is_file():
        raise MlflowSourceError(f"there is no file {path}")
    return f"sqlite:///{path.resolve()}"


def read_only_engine(url: str) -> Engine:
    """An engine on the database ``url`` that cannot write: SQLite opened read-only, with writes
    switched off too; Postgres with every transaction read-only."""
    from sqlalchemy import create_engine, make_url
    from sqlalchemy.pool import NullPool

    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        database = parsed.database or ""
        if database in ("", ":memory:") or database.startswith("file:"):
            raise MlflowSourceError(f"{url}: give the path of a SQLite database file")
        path = Path(database).resolve()
        if not path.is_file():
            raise MlflowSourceError(f"there is no file {path}")

        def connect() -> sqlite3.Connection:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
            connection.execute("PRAGMA query_only = 1")
            return connection

        return create_engine("sqlite://", creator=connect, poolclass=NullPool)
    options = " ".join(filter(None, [str(parsed.query.get("options") or ""), "-c default_transaction_read_only=on"]))
    return create_engine(
        parsed.difference_update_query(["options"]), connect_args={"options": options}, poolclass=NullPool
    )


def check_source(engine: Engine, url: str) -> SourceCheck:
    """The version check, before anything else is read: the source is an MLflow tracking store
    whose schema revision is :func:`~martian.evals._mlflow.schema_head`, with every table that
    revision has, and keeps everything in MLflow's default workspace. Raises
    :class:`MlflowSourceError` saying which it is not."""
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import SQLAlchemyError

    shown = _shown(url)
    release = version("mlflow-skinny")
    try:
        tables = set(inspect(engine).get_table_names())
        if "alembic_version" not in tables or "experiments" not in tables:
            raise MlflowSourceError(f"{shown} is not an MLflow tracking store: it has no alembic_version table")
        with engine.connect() as connection:
            found = [row[0] for row in connection.execute(text("SELECT version_num FROM alembic_version"))]
    except SQLAlchemyError as error:
        raise MlflowSourceError(f"cannot read {shown}: {_reason(error)}") from None
    if len(found) != 1:
        raise MlflowSourceError(f"{shown} has {len(found)} schema revisions; an MLflow tracking store has one")
    revision, head = str(found[0]), schema_head()
    if revision != head:
        if revision in schema_history():
            raise MlflowSourceError(
                f"{shown} has MLflow schema revision {revision}, which an older MLflow wrote: mlflow-skinny "
                f"{release} reads revision {head} and would have to migrate the store, and the import never "
                "changes the store it reads (an older MLflow cannot open a migrated store). Copy the database, "
                f"upgrade the copy with `mlflow db upgrade <the copy's URL>` on mlflow-skinny {release}, and "
                "import the copy. Nothing was copied"
            )
        raise MlflowSourceError(
            f"{shown} has MLflow schema revision {revision}, which mlflow-skinny {release} does not know: a newer "
            f"MLflow wrote it, and this release reads revision {head}. Import it with a martian-sdk release "
            "whose MLflow reads that revision. Nothing was copied"
        )
    try:
        complete = has_every_table(engine)
        with engine.connect() as connection:
            workspaces = sorted(
                str(row[0]) for row in connection.execute(text("SELECT DISTINCT workspace FROM experiments"))
            )
    except SQLAlchemyError as error:
        raise MlflowSourceError(f"cannot read {shown}: {_reason(error)}") from None
    if not complete:
        raise MlflowSourceError(f"{shown} lacks tables MLflow schema revision {revision} has. Nothing was copied")
    if others := [name for name in workspaces if name != DEFAULT_WORKSPACE]:
        raise MlflowSourceError(
            f"{shown} keeps experiments in MLflow workspaces ({', '.join(others)}); copying from a store with "
            "workspaces is not supported. Nothing was copied"
        )
    dialect: Literal["sqlite", "postgresql"] = "sqlite" if engine.dialect.name == "sqlite" else "postgresql"
    return SourceCheck(source=shown, dialect=dialect, revision=revision, head=head, mlflow=release)


def _without_password(url: str) -> str:
    parts = urlsplit(url)
    if parts.password is None:
        return url
    return parts._replace(netloc=parts.netloc.replace(f":{parts.password}@", ":***@", 1)).geturl()


def _shown(url: str) -> str:
    from sqlalchemy import make_url

    return make_url(url).render_as_string(hide_password=True)


def _reason(error: Exception) -> str:
    return str(getattr(error, "orig", None) or error).splitlines()[0][:300]


# --- what the copy holds -------------------------------------------------------------------------


@dataclass(frozen=True)
class Skipped:
    """Something in the source that is not copied, and why."""

    kind: str
    name: str
    reason: str


@dataclass(frozen=True)
class CopiedCase:
    """One trace of a run as a case: its result, the scores the run gave it, and the trace, which
    is ``None`` when an earlier run holds it."""

    result: CaseResult
    scores: list[Score]
    trace: Trace | None


@dataclass(frozen=True)
class CopiedRun:
    run: Run
    cases: list[CopiedCase]


@dataclass(frozen=True)
class CopiedProject:
    """An experiment as a project: its traces that belong to no run, and its runs."""

    project: Project
    experiment_id: str
    traces: list[Trace]
    runs: list[CopiedRun]


@dataclass(frozen=True)
class CopiedDataset:
    name: str
    mlflow_id: str
    cases: list[Case]


@dataclass
class Plan:
    """Everything the copy writes, read from the source and mapped, and what it leaves out."""

    check: SourceCheck
    projects: list[CopiedProject] = field(default_factory=list)
    datasets: list[CopiedDataset] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


# --- reading -------------------------------------------------------------------------------------


def read_mlflow_store(source: str, *, experiments: list[str] | None = None) -> Plan:
    """Check and read the tracking store ``source`` (see :func:`source_url`) and map everything it
    holds onto the shared model, writing nothing. ``experiments`` names the experiments to read,
    by name or id; every active one when it is ``None``. Raises :class:`MlflowSourceError` for a
    source the import does not read."""
    import os

    from sqlalchemy.exc import SQLAlchemyError

    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
    url = source_url(source)
    engine = read_only_engine(url)
    try:
        check = check_source(engine, url)
        try:
            reader = ReadOnlyTrackingStore(engine, url)
            return _Reader(reader, engine, check).read(experiments)
        except MlflowException as error:
            raise MlflowSourceError(f"cannot read {check.source}: {error.message[:300]}") from None
        except SQLAlchemyError as error:
            raise MlflowSourceError(f"cannot read {check.source}: {_reason(error)}") from None
    finally:
        engine.dispose()


class _Reader:
    def __init__(self, store: Any, engine: Engine, check: SourceCheck) -> None:
        self.store = store
        self.engine = engine
        self.plan = Plan(check=check)
        self.records: dict[str, list[Any]] = {}

    def read(self, selected: list[str] | None) -> Plan:
        experiments = self._experiments()
        wanted = set(selected or [])
        if unknown := sorted(wanted - {e.name for e in experiments} - {e.experiment_id for e in experiments}):
            raise MlflowSourceError(f"{self.plan.check.source} has no experiment {', '.join(unknown)}")
        chosen = []
        for experiment in experiments:
            label = f"{experiment.name} ({experiment.experiment_id})"
            if wanted and not wanted & {experiment.name, experiment.experiment_id}:
                self._skip("experiment", label, "not selected")
            elif experiment.lifecycle_stage != "active":
                traces = len(self._trace_infos(experiment.experiment_id))
                self._skip("experiment", label, f"deleted in MLflow, with {_count(traces, 'trace')}")
            else:
                chosen.append(experiment)
        if chosen or not wanted:
            self._datasets([e.experiment_id for e in chosen] if wanted else None)
        for experiment in chosen:
            project = self._project(experiment)
            if project is None:
                self._skip("experiment", f"{experiment.name} ({experiment.experiment_id})", "no traces or runs")
            else:
                self.plan.projects.append(project)
        self._uncopied()
        return self.plan

    def _skip(self, kind: str, name: str, reason: str) -> None:
        self.plan.skipped.append(Skipped(kind=kind, name=name, reason=reason))

    # --- experiments, traces and runs ------------------------------------------------------------

    def _experiments(self) -> list[Any]:
        found, token = [], None
        while True:
            page = self.store.search_experiments(view_type=ViewType.ALL, max_results=1000, page_token=token)
            found += list(page)
            if not (token := page.token):
                return sorted(found, key=lambda e: int(e.experiment_id) if ascii_digits(e.experiment_id) else 0)

    def _trace_infos(self, experiment_id: str, filter_string: str | None = None) -> list[Any]:
        found, token = [], None
        while True:
            page, token = self.store.search_traces(
                locations=[experiment_id], filter_string=filter_string, max_results=500, page_token=token
            )
            found += page
            if not token:
                return sorted(found, key=lambda info: (info.request_time or 0, info.trace_id))

    def _runs(self, experiment_id: str) -> list[Any]:
        found, token = [], None
        while True:
            page = self.store.search_runs([experiment_id], None, ViewType.ALL, max_results=1000, page_token=token)
            found += list(page)
            if not (token := page.token):
                return sorted(found, key=lambda r: (r.info.start_time or 0, r.info.run_id))

    def _project(self, experiment: Any) -> CopiedProject | None:
        experiment_id = experiment.experiment_id
        if experiment_id == DEFAULT_EXPERIMENT:
            project = Project(id=DEFAULT_PROJECT, name="Default", description="Organization evaluation project")
        else:
            project = Project(
                id=str(derived_id("project", "mlflow", experiment_id, str(experiment.creation_time))),
                name=experiment.name[:120],
                description=(experiment.tags or {}).get("mlflow.note.content", ""),
            )
        infos = self._trace_infos(experiment_id)
        owners: dict[str, str] = {}  # MLflow trace id -> the MLflow run that holds it as a case
        runs = []
        for mlflow_run in self._runs(experiment_id):
            info = mlflow_run.info
            label = f"{info.run_name or info.run_id} ({experiment.name})"
            if info.lifecycle_stage != "active":
                self._skip("run", label, "deleted in MLflow")
                continue
            linked = self._trace_infos(experiment_id, f"run_id = '{info.run_id}'")
            data = mlflow_run.data
            if not linked:
                kept = " and ".join(
                    part
                    for part in (_count(len(data.params), "parameter"), _count(len(data.metrics), "metric"))
                    if not part.startswith("0 ")
                )
                self._skip("run", label, "no traces" + (f"; MLflow keeps its {kept}" if kept else ""))
                continue
            if info.status not in RUN_STATES:
                self._skip("run", label, f"still {info.status} in MLflow; copy it again once it ends")
                continue
            runs.append((mlflow_run, linked))
            for linked_info in linked:
                owners.setdefault(linked_info.trace_id, info.run_id)
        traces = {info.trace_id: self._trace(info, project.id, owners.get(info.trace_id)) for info in infos}
        copied_runs = [
            self._run(mlflow_run, linked, project, experiment, traces, owners) for mlflow_run, linked in runs
        ]
        loose = [trace for mlflow_id, (trace, _) in traces.items() if mlflow_id not in owners]
        if not loose and not copied_runs:
            return None
        return CopiedProject(project=project, experiment_id=experiment_id, traces=loose, runs=copied_runs)

    def _trace(self, info: Any, project_id: str, owner: str | None, *, note: bool = True) -> tuple[Trace, Any]:
        """A trace in the model, with its assessments as scores, and MLflow's trace with its spans.
        ``owner`` is the MLflow run that holds the trace as a case."""
        try:
            path = trace_artifact_file(info)
        except MlflowException:
            path = ""
        if path is None:
            mlflow_trace = self.store.get_trace(info.trace_id, allow_partial=True)
        else:
            mlflow_trace = trace_from_file(info, '{"spans": []}')
            local = Path(unquote(urlsplit(path).path)) if path.startswith("file:") else Path(path)
            if path and local.is_absolute() and local.is_file():
                try:
                    mlflow_trace = trace_from_file(info, local.read_text(encoding="utf-8"))
                except (OSError, ValueError, KeyError, TypeError) as error:
                    raise MlflowSourceError(
                        f"trace {info.trace_id}: cannot read its spans in {local}: {error}"
                    ) from None
            elif note:
                where = f"in {path}, which is not a file on this machine" if path else "in no artifact location"
                self._skip("trace", info.trace_id, f"copied without its spans: MLflow keeps them {where}")
        found = trace_from_mlflow(mlflow_trace, project_id)
        trace_id = info.trace_id if _MLFLOW_TRACE_ID.match(info.trace_id) else "tr-" + digest(info.trace_id)[:32]
        scores = [
            assessment_score(assessment, run_id=run_uuid(owner) if owner and made_in(assessment, owner) else None)
            for assessment in self._valid(info, note=note)
        ]
        trace = found.model_copy(
            update={
                "id": trace_id,
                "run_id": None if owner is None else run_uuid(owner),
                "metadata": {
                    **found.metadata,
                    f"{SOURCE}trace_id": info.trace_id,
                    f"{SOURCE}experiment_id": str(info.experiment_id),
                },
                "scores": [score.model_copy(update={"trace_id": trace_id}) for score in scores],
            }
        )
        return trace, mlflow_trace

    def _valid(self, info: Any, *, note: bool = True) -> list[Any]:
        """The trace's assessments, oldest first, without those a later one overrides: MLflow
        keeps a corrected assessment and marks it invalid."""
        found = []
        for assessment in sorted(info.assessments or [], key=lambda a: (a.create_time_ms or 0, a.assessment_id or "")):
            if assessment.valid is False:
                if note:
                    self._skip("assessment", f"{assessment.name} on {info.trace_id}", "a later assessment overrides it")
                continue
            found.append(assessment)
        return found

    def _run(
        self,
        mlflow_run: Any,
        linked: list[Any],
        project: Project,
        experiment: Any,
        traces: dict[str, tuple[Trace, Any]],
        owners: dict[str, str],
    ) -> CopiedRun:
        info, data = mlflow_run.info, mlflow_run.data
        run_id = run_uuid(info.run_id)
        inputs = [dataset_input.dataset for dataset_input in mlflow_run.inputs.dataset_inputs or []]
        records = self._run_records(inputs)
        ordered: list[tuple[tuple[int, int], CopiedCase]] = []
        taken: dict[str, int] = {}
        for arrival, linked_info in enumerate(linked):
            holds = owners.get(linked_info.trace_id) == info.run_id
            if holds:
                trace, mlflow_trace = traces[linked_info.trace_id]
            else:  # an earlier run holds the trace as its case; this run's case has no trace
                trace, mlflow_trace = self._trace(linked_info, project.id, info.run_id, note=False)
            case = _case(trace, mlflow_trace, run_id=run_id, records=records, taken=taken)
            ordered.append(((case.result.position, arrival), case if holds else replace(case, trace=None)))
        ordered.sort(key=lambda pair: pair[0])
        cases = [
            replace(case, result=case.result.model_copy(update={"position": index}))
            for index, (_, case) in enumerate(ordered)
        ]
        statuses = [case.result.status for case in cases]
        counts = Counts(
            cases=len(cases),
            passed=statuses.count("passed"),
            failed=statuses.count("failed"),
            errors=statuses.count("error"),
            skipped=statuses.count("skipped"),
        )
        scorers: dict[tuple[str, ScoreSource], ScorerVersion] = {}
        for case in cases:
            for score in case.scores:
                judge = score.source == "llm_judge"
                scorers.setdefault(
                    (score.scorer, score.source),
                    ScorerVersion(
                        name=score.scorer,
                        kind=score.source,
                        type="mlflow",
                        model=(score.source_id or None) if judge else None,
                    ),
                )
        started = _from_ms(info.start_time)
        tags = data.tags or {}
        run = Run(
            id=run_id,
            project_id=project.id,
            code=f"mlflow-{info.run_name or info.run_id[:8]}",
            source="local",
            state=RUN_STATES[info.status],
            dataset_name=inputs[0].name if inputs else "",
            dataset_digest=digest(
                sorted(
                    (
                        {"id": c.result.case_id, "inputs": c.result.inputs, "expectations": c.result.expectations}
                        for c in cases
                    ),
                    key=lambda entry: str(entry["id"]),
                )
            ),
            target=Target(kind="stored"),
            scorers=list(scorers.values()),
            counts=counts,
            truncated=bool(records) and len(cases) < len(records),
            provenance={
                "mlflow": {
                    "source": self.plan.check.source,
                    "schema_revision": self.plan.check.revision,
                    "experiment_id": experiment.experiment_id,
                    "experiment": experiment.name,
                    "run_id": info.run_id,
                    "run_name": info.run_name,
                    "status": info.status,
                    "user": info.user_id or tags.get("mlflow.user"),
                    "run_type": tags.get("mlflow.runType"),
                    "metrics": dict(data.metrics or {}),
                    "params": dict(data.params or {}),
                    "tags": {key: value for key, value in tags.items() if not key.startswith("mlflow.")},
                    "datasets": [
                        {"name": d.name, "digest": d.digest, "source_type": d.source_type, "source": d.source}
                        for d in inputs
                    ],
                },
                "sdk_version": __version__,
            },
            created_at=started,
            started_at=started,
            finished_at=_from_ms(info.end_time),
        )
        return CopiedRun(run=run, cases=cases)

    def _run_records(self, inputs: list[Any]) -> list[Any]:
        """The records of the evaluation dataset of this store that the run evaluated, if it names one."""
        for dataset in inputs:
            try:
                source = json.loads(dataset.source or "{}")
            except ValueError:
                continue
            dataset_id = source.get("dataset_id") if isinstance(source, dict) else None
            if isinstance(dataset_id, str) and dataset_id in self.records:
                return self.records[dataset_id]
        return []

    # --- datasets --------------------------------------------------------------------------------

    def _datasets(self, experiment_ids: list[str] | None) -> None:
        found, token = [], None
        while True:
            page = self.store.search_datasets(experiment_ids=experiment_ids, max_results=1000, page_token=token)
            found += list(page)
            if not (token := page.token):
                break
        for dataset in sorted(found, key=lambda d: (d.created_time or 0, d.dataset_id)):
            records, token = [], None
            while True:
                page, token = load_dataset_records(self.store, dataset.dataset_id, token)
                records += page
                if not token:
                    break
            self.records[dataset.dataset_id] = records
            cases = []
            for record in records:
                if not isinstance(record.inputs, dict) or not record.inputs:
                    self._skip("record", f"{record.dataset_record_id} of {dataset.name}", "no inputs")
                    continue
                cases.append(dataset_case(record))
            if not cases:
                self._skip("dataset", dataset.name, "no records with inputs")
                continue
            self.plan.datasets.append(CopiedDataset(name=dataset.name[:200], mlflow_id=dataset.dataset_id, cases=cases))

    # --- what has no place in the core -----------------------------------------------------------

    def _uncopied(self) -> None:
        from sqlalchemy import text

        with self.engine.connect() as connection:
            for table, noun in COUNTED.items():
                (count,) = connection.execute(text(f"SELECT COUNT(*) FROM {table}")).one()  # noqa: S608 - fixed names
                if count:
                    self._skip("", _count(int(count), noun), "the core has no place for them")


# --- mapping -------------------------------------------------------------------------------------


def run_uuid(mlflow_run_id: str) -> str:
    """A run's id: MLflow's run id, 32 hexadecimal digits, as the UUID it is."""
    if _MLFLOW_RUN_ID.match(mlflow_run_id):
        return str(uuid.UUID(mlflow_run_id))
    return str(derived_id("run", "mlflow", mlflow_run_id))


def dataset_case(record: Any) -> Case:
    """An evaluation dataset record as a case under the record's id."""
    source = record.source
    metadata = (
        {
            f"{SOURCE}source": {
                "type": str(getattr(source.source_type, "value", source.source_type)),
                "data": source.source_data,
            }
        }
        if source is not None
        else {}
    )
    return Case(
        id=record.dataset_record_id,
        inputs=dict(record.inputs),
        expectations=dict(record.expectations or {}),
        outputs=record.outputs or None,
        tags={str(key): str(value) for key, value in (record.tags or {}).items() if not str(key).startswith("mlflow.")},
        metadata=metadata,
    )


def assessment_score(assessment: Any, *, run_id: str | None = None) -> Score:
    """An MLflow assessment as a score, naming the run ``run_id`` when it is one of the run's
    scores (:func:`made_in`). A person's feedback and every expectation are annotations
    (``metadata["martian.annotation"]`` ``feedback`` or ``expectation``), as the UI records them.
    A dict or list value is its JSON text; a number that is not finite is no value, with an error
    saying what it was."""
    source = SCORE_SOURCES.get(str(assessment.source.source_type), "code")
    expectation = not isinstance(assessment, Feedback)
    raw = assessment.value
    value, error = _value(raw)
    if isinstance(assessment, Feedback) and assessment.error is not None:
        error = assessment.error.error_message or assessment.error.error_code or "error"
    metadata: dict[str, Any] = {
        f"{SOURCE}assessment_id": assessment.assessment_id,
        f"{SOURCE}created_at": _iso(_from_ms(assessment.create_time_ms)),
    }
    for key, found in (
        (f"{SOURCE}span_id", assessment.span_id),
        (f"{SOURCE}metadata", dict(assessment.metadata or {}) or None),
        (f"{SOURCE}overrides", assessment.overrides),
    ):
        if found:
            metadata[key] = found
    if expectation or source == "human":
        metadata["martian.annotation"] = "expectation" if expectation else "feedback"
    return Score(
        run_id=run_id,
        scorer=assessment.name,
        source=source,
        source_id=assessment.source.source_id or "",
        value=value,
        passed=None if expectation else verdict(value),
        rationale=(assessment.rationale or "") if not expectation else "",
        error=error,
        metadata=metadata,
        created_at=_from_ms(assessment.create_time_ms),
    )


def assessment_key(score: Score) -> tuple[str, ScoreSource, str | None]:
    """Which MLflow assessment a copied score is: its name, its source and the source's own
    assessment id (``metadata["mlflow_source.assessment_id"]``). A trace may hold a code scorer's,
    a judge's and a person's feedback of one name, so a name alone does not say which."""
    found = score.metadata.get(f"{SOURCE}assessment_id")
    return score.scorer, score.source, found if isinstance(found, str) else None


def verdict(value: Value) -> bool | None:
    """A feedback value's verdict: a boolean's own, MLflow judges' ``yes`` and ``no``; none for
    numbers and other text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return VERDICTS.get(value.strip().lower())
    return None


def _value(raw: Any) -> tuple[Value, str]:
    if raw is None or isinstance(raw, bool | int | str):
        return raw, ""
    if isinstance(raw, float):
        return (raw, "") if math.isfinite(raw) else (None, f"the value was {raw}")
    return json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str), ""


def made_in(assessment: Any, mlflow_run_id: str) -> bool:
    """Whether an assessment is a score of the MLflow run whose case its trace is: a judge's or
    code's feedback that MLflow records as made in that run (``mlflow.assessment.sourceRunId``),
    or that names no run. A person's feedback and expectations are annotations, not scores."""
    if not isinstance(assessment, Feedback) or str(assessment.source.source_type) == "HUMAN":
        return False
    made = (assessment.metadata or {}).get(AssessmentMetadataKey.SOURCE_RUN_ID)
    return made is None or made == mlflow_run_id


def _case(trace: Trace, mlflow_trace: Any, *, run_id: str, records: list[Any], taken: dict[str, int]) -> CopiedCase:
    """A trace of a run as a case, with the trace's scores that name the run. The case's id is the
    id of the dataset record with the same inputs, when the run evaluated an evaluation dataset of
    the store, else ``case-`` and a digest of the inputs, so that runs over the same inputs pair;
    a repeat within the run adds `` (2)``. Its position is the record's, else after the records."""
    root = next((span for span in mlflow_trace.data.spans if span.parent_id is None), None)
    found = root.inputs if root is not None else trace.request
    inputs: dict[str, Any] = found if isinstance(found, dict) else {"request": found}
    output = root.outputs if root is not None else trace.response
    record = next((index for index, r in enumerate(records) if r.inputs == inputs), None)
    base = records[record].dataset_record_id if record is not None else "case-" + digest(inputs)[:12]
    taken[base] = taken.get(base, 0) + 1
    case_id = base if taken[base] == 1 else f"{base} ({taken[base]})"
    expectations = {
        score.scorer: score.value for score in trace.scores if score.metadata.get("martian.annotation") == "expectation"
    }
    marked = [
        score.model_copy(update={"case_id": case_id}) if score.run_id == run_id else score for score in trace.scores
    ]
    scores = [score for score in marked if score.run_id == run_id]
    error = ""
    if trace.state == "error":
        description = getattr(getattr(root, "status", None), "description", "") if root is not None else ""
        error = description or "the trace ended in an error"
    finished = (
        trace.request_time + timedelta(milliseconds=trace.latency_ms or 0) if trace.request_time is not None else None
    )
    result = CaseResult(
        run_id=run_id,
        case_id=case_id,
        position=record if record is not None else len(records),
        status=_status(trace, scores),
        inputs=inputs,
        expectations=expectations,
        output=output,
        error=error,
        latency_ms=trace.latency_ms,
        finished_at=finished,
    )
    held = trace.model_copy(update={"tags": {**trace.tags, "martian.case_id": case_id}, "scores": marked})
    return CopiedCase(result=result, scores=scores, trace=held)


def _status(trace: Trace, scores: list[Score]) -> CaseStatus:
    """``error`` when the trace failed; ``failed`` when a score fails; ``error`` when one has an
    error; ``passed`` when one passes; ``skipped`` when none gives a verdict."""
    if trace.state == "error":
        return "error"
    verdicts = [score.passed for score in scores if score.passed is not None]
    if False in verdicts:
        return "failed"
    if any(score.error for score in scores):
        return "error"
    return "passed" if verdicts else "skipped"


def _from_ms(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value / 1000, UTC) if value is not None else None


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when is not None else None


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


# --- writing -------------------------------------------------------------------------------------


@dataclass
class CopyReport:
    """What a copy wrote, what was there already, and what the source holds that it left out."""

    plan: Plan
    copied: dict[str, int] = field(default_factory=dict)
    present: dict[str, int] = field(default_factory=dict)
    runs: list[Run] = field(default_factory=list)
    dataset_versions: list[tuple[str, int | None]] = field(default_factory=list)

    def add(self, kind: str, *, new: bool, number: int = 1) -> None:
        counts = self.copied if new else self.present
        counts[kind] = counts.get(kind, 0) + number


def _wait[T](work: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(work)


def copy_mlflow_store(
    plan: Plan, store: EvalStore, records: RecordStore, principal: Principal = SINGLE_USER
) -> CopyReport:
    """Write what :func:`read_mlflow_store` read into the organisation of ``principal``, through
    its store and record store. What is there already, from an earlier copy, is left as it is.
    Raises :class:`MlflowSourceError`, before writing, when the organisation cannot take the
    projects."""
    report = CopyReport(plan=plan)
    _wait(_check_projects(store, principal, plan))
    for project in plan.projects:
        new = _wait(store.get_project(principal, project.project.id)) is None
        if new:
            _wait(store.create_project(principal, project.project.model_copy(update={"created_at": datetime.now(UTC)})))
        report.add("projects", new=new)
    if plan.datasets:
        _wait(
            store.create_project(
                principal, Project(id=DEFAULT_PROJECT, name="Default", description="Organization evaluation project")
            )
        )
        before = {d.id for d in _wait(store.list_datasets(principal, DEFAULT_PROJECT))}
        for dataset in plan.datasets:
            saved = _wait(store.create_dataset(principal, DEFAULT_PROJECT, dataset.name, dataset.cases))
            report.add("datasets", new=saved.id not in before)
            report.dataset_versions.append((saved.name, saved.version))
    for project in plan.projects:
        for trace in project.traces:
            _wait(_trace(store, principal, trace, report))
        for copied in project.runs:
            run = _copy_run(store, records, principal, copied, report)
            report.runs.append(run)
    return report


async def _check_projects(store: EvalStore, principal: Principal, plan: Plan) -> None:
    existing = {project.id: project for project in await store.list_projects(principal)}
    new = [p.project for p in plan.projects if p.project.id not in existing and p.project.id != DEFAULT_PROJECT]
    others = sum(project_id != DEFAULT_PROJECT for project_id in existing)
    if others + len(new) > PROJECT_LIMIT:
        raise MlflowSourceError(
            f"the copy makes a project of each of {_count(len(new), 'new experiment')}, and the organisation "
            f"has {_count(others, 'project')} besides the default one: at most {PROJECT_LIMIT} fit. Choose "
            "experiments with --experiment. Nothing was copied"
        )
    if archived := sorted(
        p.project.name for p in plan.projects if (found := existing.get(p.project.id)) and found.archived_at
    ):
        raise MlflowSourceError(
            f"the projects an earlier copy made of {', '.join(archived)} are archived and take no more work. "
            "Nothing was copied"
        )


async def _trace(store: EvalStore, principal: Principal, trace: Trace, report: CopyReport) -> Trace:
    """Record a trace, with its scores, unless an earlier copy did."""
    found = await store.get_trace(principal, trace.project_id, trace.id)
    if found is not None:
        report.add("traces", new=False)
        return found
    saved = await store.record_trace(principal, trace)
    report.add("traces", new=True)
    for score in trace.scores:
        kind = (
            "expectations"
            if score.metadata.get("martian.annotation") == "expectation"
            else ("annotations" if score.metadata.get("martian.annotation") else "scores")
        )
        report.add(kind, new=True)
    return saved


def _copy_run(
    store: EvalStore, records: RecordStore, principal: Principal, copied: CopiedRun, report: CopyReport
) -> Run:
    """Record a run as ``martian evals run`` does: ``running`` while its cases are written, then
    MLflow's state. One an earlier copy recorded is left as it is. A copy that stops early leaves
    the run ``failed``, or ``cancelled`` when interrupted."""
    final = copied.run
    existing = _wait(records.get_run(principal, final.project_id, final.id))
    if existing is not None:
        report.add("runs", new=False)
        report.add("traces", new=False, number=sum(case.trace is not None for case in copied.cases))
        return existing
    started = final.model_copy(
        update={"state": "running", "counts": Counts(cases=final.counts.cases), "finished_at": None}
    )
    run = _wait(_start(store, records, principal, started))
    try:
        _wait(_cases(store, records, principal, run, copied.cases, report))
    except BaseException as error:
        stopped = (
            {"state": "cancelled"}
            if isinstance(error, KeyboardInterrupt)
            else {"state": "failed", "error": f"the copy stopped early: {type(error).__name__}"}
        )
        _wait(_end(store, records, principal, run.model_copy(update=stopped)))
        raise
    finished = _wait(
        _end(
            store,
            records,
            principal,
            run.model_copy(update={"state": final.state, "counts": final.counts, "finished_at": final.finished_at}),
        )
    )
    report.add("runs", new=True)
    return finished


async def _start(store: EvalStore, records: RecordStore, principal: Principal, run: Run) -> Run:
    run = await store.start_run(principal, run.model_copy(update={"requested_by": principal.user_id}))
    return await records.create_run(principal, run)


async def _cases(
    store: EvalStore,
    records: RecordStore,
    principal: Principal,
    run: Run,
    cases: list[CopiedCase],
    report: CopyReport,
) -> None:
    results: list[CaseResult] = []
    scores: list[Score] = []
    linked: list[str] = []
    for case in cases:
        if case.trace is None:
            results.append(case.result)
            scores += case.scores
            continue
        saved = await _trace(store, principal, case.trace, report)
        stored = {assessment_key(score): score.store for score in saved.scores}
        scores += [
            score.model_copy(update={"trace_id": saved.id, "store": stored.get(assessment_key(score), StoreRef())})
            for score in case.scores
        ]
        results.append(case.result.model_copy(update={"store": StoreRef(trace_id=saved.id)}))
        linked.append(saved.id)
    if linked:
        await store.link_traces(principal, run.project_id, run.id, linked)
    await records.put_results(principal, run.project_id, results)
    if scores:
        await records.put_scores(principal, run.project_id, scores)
    report.add("case results", new=True, number=len(results))


async def _end(store: EvalStore, records: RecordStore, principal: Principal, run: Run) -> Run:
    finished = await records.update_run(principal, run, expected_state="running")
    await store.finish_run(principal, run.project_id, run.id, state=run.state, counts=run.counts)
    return finished


def report_lines(report: CopyReport, *, written: bool) -> list[str]:
    """The report as ``martian evals import mlflow`` prints it: the version check, what each
    experiment, run and dataset became, the counts written and already there, and what was left
    out and why."""
    plan = report.plan
    check = plan.check
    lines = [
        f"Read {check.source}: MLflow schema revision {check.revision}, the one mlflow-skinny {check.mlflow} reads."
    ]
    if not written:
        lines.append("To copy (nothing was written, --dry-run):")
    else:
        lines.append("Copied, or there from an earlier copy:" if report.present else "Copied:")
    runs = {run.id: run for run in report.runs}
    for project in plan.projects:
        traces = len(project.traces) + sum(case.trace is not None for run in project.runs for case in run.cases)
        lines.append(
            f"  experiment {project.experiment_id} as project {project.project.name}: "
            f"{_count(traces, 'trace')}, {_count(len(project.runs), 'run')}"
        )
        for copied in project.runs:
            run = runs.get(copied.run.id, copied.run)
            counts = run.counts
            lines.append(
                f"    run {run.code} ({run.state}): {_count(counts.cases, 'case')}: {counts.passed} passed, "
                f"{counts.failed} failed, {counts.errors} errors, {counts.skipped} without a verdict"
            )
    versions = dict(report.dataset_versions)
    for dataset in plan.datasets:
        version = f", version {versions[dataset.name]}" if versions.get(dataset.name) else ""
        lines.append(f"  dataset {dataset.name}: {_count(len(dataset.cases), 'case')}{version}")
    if written:
        lines.append("  New: " + (_tally(report.copied) or "nothing"))
        if report.present:
            lines.append("  There from an earlier copy, left as they were: " + _tally(report.present))
    groups: dict[tuple[str, str], list[str]] = {}
    for skipped in plan.skipped:
        groups.setdefault((skipped.kind, skipped.reason), []).append(skipped.name)
    if groups:
        lines.append("Not copied:")
    for (kind, reason), names in groups.items():
        if len(names) == 1:
            lines.append(f"  {kind} {names[0]}: {reason}" if kind else f"  {names[0]}: {reason}")
        else:
            shown = ", ".join(names[:3]) + (", ..." if len(names) > 3 else "")
            lines.append(f"  {len(names)} {kind}s ({shown}): {reason}")
    return lines


def _tally(counts: dict[str, int]) -> str:
    order = ["projects", "datasets", "traces", "scores", "annotations", "expectations", "runs", "case results"]
    return ", ".join(
        f"{counts[kind]} {kind if counts[kind] != 1 else kind.removesuffix('s')}" for kind in order if counts.get(kind)
    )


def import_mlflow_store(
    source: str,
    home: Path,
    *,
    experiments: list[str] | None = None,
    dry_run: bool = False,
) -> CopyReport:
    """Check and read the tracking store ``source``, then copy it into the local edition's data
    folder ``home`` as the single local user. With ``dry_run``, only read it."""
    from martian.evals.local import LocalEditionError, open_stores

    plan = read_mlflow_store(source, experiments=experiments)
    if dry_run:
        return CopyReport(plan=plan)
    try:
        store, records = open_stores(home)
    except LocalEditionError:
        raise
    except Exception as error:  # noqa: BLE001 - a folder may fail to open in many ways; say which
        raise LocalEditionError(f"could not open the local data folder {home}: {type(error).__name__}") from None
    try:
        return copy_mlflow_store(plan, store, records)
    except StoreError as error:
        raise MlflowSourceError(f"the copy stopped: {error.detail}") from None
    finally:
        records.close()


__all__ = [
    "CopiedCase",
    "CopiedDataset",
    "CopiedProject",
    "CopiedRun",
    "CopyReport",
    "MlflowSourceError",
    "Plan",
    "Skipped",
    "SourceCheck",
    "assessment_key",
    "assessment_score",
    "check_source",
    "copy_mlflow_store",
    "dataset_case",
    "import_mlflow_store",
    "read_mlflow_store",
    "read_only_engine",
    "run_uuid",
    "source_url",
    "verdict",
]
