"""The record store on SQLite or Postgres, with one migration history for both.

``SqlRecordStore("sqlite:///evals/martian.db")`` for the local edition, or a Postgres URL
(``postgresql+psycopg://...``, with ``martian-sdk[postgres]``) whose tables go in ``schema``. The
store migrates the database to the newest revision when it starts, unless ``migrate=False``
(for installs that migrate in a separate job with :func:`upgrade`).

Every statement filters on the organisation's space. On Postgres each transaction also sets that
space for row-level security, which filters the same rows again and applies to the tables' owner;
a role that bypasses row-level security, such as a superuser, is not held by that second guard.

A write the database refuses, such as a value out of range or a duplicate key, raises
``StoreError`` naming the method and the table, never a value written. The engine hides statement
values from SQLAlchemy's errors and log lines, so any other database failure, such as a lost
connection, raises SQLAlchemy's error without them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Connection,
    Engine,
    Select,
    Table,
    create_engine,
    delete,
    func,
    insert,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.exc import DataError, IntegrityError

from martian.evals._digits import ascii_digits
from martian.evals._sql import check_schema, create_schema, database_refusal, is_postgres, operation_name, postgres_url
from martian.evals.model import (
    Case,
    CaseResult,
    DatasetDraft,
    Page,
    Review,
    Run,
    RunKind,
    RunState,
    Score,
    ScoringJob,
    Suite,
)
from martian.evals.records import (
    THRESHOLD_WINDOW_BOUND,
    Alert,
    BlindQueue,
    CalibrationRecheck,
    DatasetManifest,
    Delivery,
    JudgeCalibration,
    Monitor,
    MonitoredTrace,
    MonitorProgress,
    RecheckState,
    RedteamRecords,
    SavedView,
    Schedule,
    ScheduledRun,
    ScheduleProgress,
    SuiteAudit,
    Threshold,
    ThresholdStatus,
)
from martian.evals.records.admission import LAUNCHED, RunAdmission
from martian.evals.records.events import Event, EventBus, InProcessEvents, space_of
from martian.evals.records.tables import (
    REDTEAM_TABLES,
    blind_queues,
    calibration_rechecks,
    case_results,
    dataset_draft_cases,
    dataset_drafts,
    dataset_manifests,
    deliveries,
    judge_calibrations,
    monitored_traces,
    monitors,
    redteam_evidence,
    redteam_findings,
    redteam_probes,
    redteam_verifications,
    reviews,
    runs,
    saved_views,
    schedule_runs,
    schedules,
    scoring_jobs,
    suite_audits,
    suites,
    threshold_alerts,
    thresholds,
    work_queue,
)
from martian.evals.records.tables import scores as score_rows
from martian.evals.store import StoreError, check_trace_deletion, cursor_offset

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

T = TypeVar("T")
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = connection
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Migrate the database to ``revision`` (the newest by default)."""
    with engine.begin() as connection:
        command.upgrade(_config(connection), revision)


def downgrade(engine: Engine, revision: str = "base") -> None:
    with engine.begin() as connection:
        command.downgrade(_config(connection), revision)


def engine_for(database_url: str, *, schema: str = "martian") -> Engine:
    """An engine whose unqualified tables are in ``schema`` on Postgres, created if missing. It
    keeps statement values out of SQLAlchemy's errors and log lines: they are customer content."""
    if is_postgres(database_url):
        create_schema(database_url, check_schema(schema))
        return create_engine(postgres_url(database_url, schema), hide_parameters=True)
    return create_engine(database_url, connect_args={"timeout": 30}, hide_parameters=True)


def _now() -> datetime:
    return datetime.now(UTC)


def _json(model: Any) -> Any:
    return model.model_dump(mode="json")


_ACTIVE = ("queued", "running")


def _count(connection: Connection, rows: Select[Any], up_to: int | None) -> int:
    """The number of ``rows``, one statement, reading no more than ``up_to`` of them."""
    if up_to is not None:
        rows = rows.limit(up_to)
    return connection.execute(select(func.count()).select_from(rows.subquery())).scalar_one()


def _page[M](items: list[M], offset: int, limit: int, total: int) -> Page[M]:
    return Page[M](items=items, next_cursor=str(offset + limit) if offset + limit < total else None)


def _bounds(offset: int, limit: int, maximum: int = 1000) -> None:
    if offset < 0 or not 1 <= limit <= maximum:
        raise StoreError("invalid", f"offset must be 0 or more and limit between 1 and {maximum}")


class SqlRecordStore:
    def __init__(
        self,
        database_url: str,
        *,
        schema: str = "martian",
        events: EventBus | None = None,
        migrate: bool = True,
        admission: RunAdmission | None = None,
    ) -> None:
        self._postgres = is_postgres(database_url)
        self.admission = admission
        self._engine = engine_for(database_url, schema=schema)
        if migrate:
            upgrade(self._engine)
        self.events: EventBus = events or InProcessEvents()

    def close(self) -> None:
        self._engine.dispose()

    @property
    def local_database_path(self) -> Path | None:
        """The file shared by local processes, if this store has one."""
        database = self._engine.url.database
        if self._postgres or not database or database == ":memory:":
            return None
        return Path(database).resolve()

    # --- plumbing ----------------------------------------------------------------------------

    async def _run(self, principal: Principal, work: Callable[[Connection, str], T]) -> T:
        """Run ``work`` in one transaction, on a thread. A write the database refuses becomes a
        ``StoreError`` naming the method ``work`` belongs to (``database_refusal``)."""
        space = space_of(principal)
        operation = operation_name(work)

        def transaction() -> T:
            try:
                with self._engine.begin() as connection:
                    if self._postgres:
                        connection.execute(text("SELECT set_config('martian.space', :space, true)"), {"space": space})
                    return work(connection, space)
            except (DataError, IntegrityError) as error:
                refused = database_refusal(operation, error)
            # Raised outside the except clause, so that the database's error, which holds the
            # values written, does not stay on it as its context.
            raise refused

        return await asyncio.to_thread(transaction)

    def _emit(
        self, principal: Principal, kind: Any, project_id: str, record_id: str, *, run_id: str | None = None
    ) -> None:
        event = Event(kind=kind, project_id=project_id, id=record_id, run_id=run_id)
        self.events.publish(space_of(principal), event)

    @staticmethod
    def _one(connection: Connection, table: Table, space: str, project_id: str, **keys: Any) -> Any:
        query = select(table.c.record).where(table.c.space == space, table.c.project_id == project_id)
        for name, value in keys.items():
            query = query.where(table.c[name] == value)
        return connection.execute(query).scalar_one_or_none()

    def _require_run(self, connection: Connection, space: str, project_id: str, run_id: str | None) -> None:
        if run_id is None or self._one(connection, runs, space, project_id, id=run_id) is None:
            raise StoreError("not_found", "run not found")

    def _upsert(self, connection: Connection, table: Table, values: dict[str, Any], keys: list[str]) -> None:
        from sqlalchemy.dialects import postgresql, sqlite

        dialect_insert: Any = postgresql.insert if connection.dialect.name == "postgresql" else sqlite.insert
        statement = dialect_insert(table).values(**values)
        changed = {name: statement.excluded[name] for name in values if name not in keys}
        connection.execute(statement.on_conflict_do_update(index_elements=keys, set_=changed))

    # --- runs --------------------------------------------------------------------------------

    async def create_run(self, principal: Principal, run: Run) -> Run:
        if (
            self.admission is not None
            and run.source == LAUNCHED
            and await self.get_run(principal, run.project_id, run.id) is None
        ):
            run = await self.admission.admit(principal, run)

        def work(connection: Connection, space: str) -> tuple[Run, bool]:
            if stored := self._one(connection, runs, space, run.project_id, id=run.id):
                return Run.model_validate(stored), False
            saved = run.model_copy(update={"created_at": run.created_at or _now()})
            connection.execute(
                insert(runs).values(
                    space=space,
                    project_id=run.project_id,
                    id=run.id,
                    state=run.state,
                    source=run.source,
                    kind=run.kind,
                    created_at=saved.created_at,
                    record=_json(saved),
                )
            )
            return saved, True

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "run", run.project_id, run.id)
        return saved

    async def get_run(self, principal: Principal, project_id: str, run_id: str) -> Run | None:
        def work(connection: Connection, space: str) -> Run | None:
            stored = self._one(connection, runs, space, project_id, id=run_id)
            return Run.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def list_runs(
        self,
        principal: Principal,
        project_id: str,
        *,
        states: list[RunState] | None = None,
        limit: int = 50,
        cursor: str | None = None,
        kind: RunKind | None = "eval",
    ) -> Page[Run]:
        offset = cursor_offset(cursor)
        _bounds(offset, limit, 500)

        def work(connection: Connection, space: str) -> Page[Run]:
            where = [runs.c.space == space, runs.c.project_id == project_id]
            if states:
                where.append(runs.c.state.in_(states))
            if kind is not None:
                where.append(runs.c.kind == kind)
            total = connection.execute(select(func.count()).select_from(runs).where(*where)).scalar_one()
            rows: Sequence[Any] = (
                connection.execute(
                    select(runs.c.record)
                    .where(*where)
                    .order_by(runs.c.created_at.desc(), runs.c.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return _page([Run.model_validate(r) for r in rows], offset, limit, total)

        return await self._run(principal, work)

    async def count_active_runs(self, principal: Principal, *, up_to: int | None = None) -> int:
        def work(connection: Connection, space: str) -> int:
            return _count(connection, select(literal(1)).where(runs.c.space == space, runs.c.state.in_(_ACTIVE)), up_to)

        return await self._run(principal, work)

    async def update_run(
        self,
        principal: Principal,
        run: Run,
        *,
        expected_state: RunState | None = None,
        expected_checkpoint: Run | None = None,
    ) -> Run:
        def work(connection: Connection, space: str) -> Run:
            stored = self._one(connection, runs, space, run.project_id, id=run.id)
            if stored is None:
                raise StoreError("not_found", "run not found")
            saved = run.model_copy(update={"created_at": Run.model_validate(stored).created_at})
            statement = update(runs).where(
                runs.c.space == space, runs.c.project_id == run.project_id, runs.c.id == run.id
            )
            if expected_state is not None:
                statement = statement.where(runs.c.state == expected_state)
            if expected_checkpoint is not None:
                statement = statement.where(
                    runs.c.state == expected_checkpoint.state,
                    runs.c.record["finished_at"].as_string() == _json(expected_checkpoint)["finished_at"],
                )
            changed = connection.execute(statement.values(state=run.state, record=_json(saved))).rowcount
            if changed == 0:
                raise StoreError("conflict", "the run is no longer in the expected state")
            return saved

        saved = await self._run(principal, work)
        self._emit(principal, "run", run.project_id, run.id)
        return saved

    async def list_finished_runs(
        self,
        principal: Principal,
        project_id: str,
        *,
        created_before: datetime,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[Run]:
        offset = cursor_offset(cursor)
        _bounds(offset, limit, 500)

        def work(connection: Connection, space: str) -> Page[Run]:
            where = [
                runs.c.space == space,
                runs.c.project_id == project_id,
                runs.c.created_at < created_before.astimezone(UTC),
                runs.c.state.not_in(_ACTIVE),
            ]
            total = connection.execute(select(func.count()).select_from(runs).where(*where)).scalar_one()
            rows: Sequence[Any] = (
                connection.execute(
                    select(runs.c.record)
                    .where(*where)
                    .order_by(runs.c.created_at, runs.c.id)
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return _page([Run.model_validate(r) for r in rows], offset, limit, total)

        return await self._run(principal, work)

    async def delete_run(self, principal: Principal, project_id: str, run_id: str) -> dict[str, int]:
        from martian.evals.records.queue import CLAIMED  # the queue's module imports this one

        def work(connection: Connection, space: str) -> tuple[dict[str, int], list[str]]:
            stored = self._one(connection, runs, space, project_id, id=run_id)
            if stored is None:
                raise StoreError("not_found", "run not found")
            stored_run = Run.model_validate(stored)
            if stored_run.state in _ACTIVE:
                raise StoreError("conflict", "the run is queued or running; cancel it, or let it end, first")
            jobs = (
                scoring_jobs.c.space == space,
                scoring_jobs.c.project_id == project_id,
                scoring_jobs.c.run_id == run_id,
            )
            if connection.execute(select(scoring_jobs.c.id).where(*jobs, scoring_jobs.c.state.in_(_ACTIVE))).first():
                raise StoreError(
                    "conflict", "a scoring job of the run is queued or running; cancel it, or let it end, first"
                )
            # Run and scoring work name their run; a worker holds work while its claim's lease runs.
            its_work = (
                work_queue.c.space == space,
                work_queue.c.project_id == project_id,
                work_queue.c.work["run_id"].as_string() == run_id,
            )
            held = work_queue.c.state == CLAIMED, work_queue.c.lease_expires_at >= _now()
            if connection.execute(select(work_queue.c.id).where(*its_work, *held)).first():
                raise StoreError("conflict", "a worker is still on the run's work; let it end first")
            slots = (schedule_runs.c.space == space, schedule_runs.c.project_id == project_id)
            schedule_ids: list[str] = list(
                connection.execute(
                    select(schedule_runs.c.schedule_id).where(*slots, schedule_runs.c.run_id == run_id).distinct()
                ).scalars()
            )
            removed: dict[str, int] = {}
            for table, key in (
                (case_results, case_results.c.run_id),
                (score_rows, score_rows.c.run_id),
                (scoring_jobs, scoring_jobs.c.run_id),
                (reviews, reviews.c.run_id),
                (blind_queues, blind_queues.c.run_id),
                (schedule_runs, schedule_runs.c.run_id),
                # Only a red team run has rows in the red team tables.
                *((table, table.c.run_id) for table in REDTEAM_TABLES if stored_run.kind == "redteam"),
            ):
                removed[table.name] = connection.execute(
                    delete(table).where(table.c.space == space, table.c.project_id == project_id, key == run_id)
                ).rowcount
            removed[work_queue.name] = connection.execute(delete(work_queue).where(*its_work)).rowcount
            removed[deliveries.name] = connection.execute(
                delete(deliveries).where(
                    deliveries.c.space == space,
                    deliveries.c.project_id == project_id,
                    deliveries.c.kind == "score",
                    deliveries.c.record[("payload", "score", "run_id")].as_string() == run_id,
                )
            ).rowcount
            # Last, and only while it is still finished: a stopped run resumed meanwhile is queued
            # again, and the whole delete is undone.
            removed[runs.name] = connection.execute(
                delete(runs).where(
                    runs.c.space == space,
                    runs.c.project_id == project_id,
                    runs.c.id == run_id,
                    runs.c.state.not_in(_ACTIVE),
                )
            ).rowcount
            if removed[runs.name] != 1:
                raise StoreError("conflict", "the run changed while it was being deleted; try again")
            return removed, schedule_ids

        removed, schedule_ids = await self._run(principal, work)
        self._emit(principal, "run", project_id, run_id)
        for schedule_id in schedule_ids:
            self._emit(principal, "schedule", project_id, schedule_id)
        return removed

    # --- red team records --------------------------------------------------------------------

    _REDTEAM_PARTS = (
        ("probes", redteam_probes, "id"),
        ("evidence", redteam_evidence, "id"),
        ("verifications", redteam_verifications, "probe_id"),
        ("findings", redteam_findings, "id"),
    )

    async def put_redteam_records(
        self, principal: Principal, project_id: str, run_id: str, records: RedteamRecords
    ) -> None:
        def work(connection: Connection, space: str) -> None:
            stored = self._one(connection, runs, space, project_id, id=run_id)
            if stored is None:
                raise StoreError("not_found", "run not found")
            if Run.model_validate(stored).kind != "redteam":
                raise StoreError("invalid", "only a red team run has red team records")
            for part, table, key in self._REDTEAM_PARTS:
                where = (table.c.space == space, table.c.project_id == project_id, table.c.run_id == run_id)
                connection.execute(delete(table).where(*where))
                items: list[dict[str, Any]] = getattr(records, part)
                if len({item[key] for item in items}) != len(items):
                    raise StoreError("invalid", f"two red team {part} of the run share one {key}")
                rows = []
                for position, item in enumerate(items):
                    row = {
                        "space": space,
                        "project_id": project_id,
                        "run_id": run_id,
                        key: item[key],
                        "position": position,
                        "record": item,
                    }
                    if table is redteam_findings:
                        row["level"] = int(item["evidence_level"])
                        row["status"] = item.get("status", "open")
                    rows.append(row)
                if rows:
                    connection.execute(insert(table), rows)

        await self._run(principal, work)
        self._emit(principal, "run", project_id, run_id)

    async def get_redteam_records(self, principal: Principal, project_id: str, run_id: str) -> RedteamRecords:
        def work(connection: Connection, space: str) -> RedteamRecords:
            if self._one(connection, runs, space, project_id, id=run_id) is None:
                raise StoreError("not_found", "run not found")
            found: dict[str, list[dict[str, Any]]] = {}
            for part, table, _ in self._REDTEAM_PARTS:
                found[part] = list(
                    connection.execute(
                        select(table.c.record)
                        .where(table.c.space == space, table.c.project_id == project_id, table.c.run_id == run_id)
                        .order_by(table.c.position)
                    ).scalars()
                )
            return RedteamRecords(**found)

        return await self._run(principal, work)

    # --- case results and scores ------------------------------------------------------------

    async def put_results(
        self, principal: Principal, project_id: str, results: list[CaseResult], *, scores: list[Score] | None = None
    ) -> None:
        def work(connection: Connection, space: str) -> tuple[set[str], set[str]]:
            touched = set()
            for result in results:
                self._require_run(connection, space, project_id, result.run_id)
                self._upsert(
                    connection,
                    case_results,
                    {
                        "space": space,
                        "project_id": project_id,
                        "run_id": result.run_id,
                        "case_id": result.case_id,
                        "position": result.position,
                        "status": result.status,
                        "record": _json(result),
                    },
                    ["space", "project_id", "run_id", "case_id"],
                )
                touched.add(result.run_id)
            return touched, self._put_scores(connection, space, project_id, scores or [])

        results_of, scores_of = await self._run(principal, work)
        for run_id in results_of:
            self._emit(principal, "case_results", project_id, run_id)
        for run_id in scores_of:
            self._emit(principal, "scores", project_id, run_id)

    async def list_results(
        self, principal: Principal, project_id: str, run_id: str, *, offset: int = 0, limit: int = 100
    ) -> Page[CaseResult]:
        _bounds(offset, limit)

        def work(connection: Connection, space: str) -> Page[CaseResult]:
            self._require_run(connection, space, project_id, run_id)
            where = [
                case_results.c.space == space,
                case_results.c.project_id == project_id,
                case_results.c.run_id == run_id,
            ]
            total = connection.execute(select(func.count()).select_from(case_results).where(*where)).scalar_one()
            rows: Sequence[Any] = (
                connection.execute(
                    select(case_results.c.record)
                    .where(*where)
                    .order_by(case_results.c.position, case_results.c.case_id)
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return _page([CaseResult.model_validate(r) for r in rows], offset, limit, total)

        return await self._run(principal, work)

    def _put_scores(self, connection: Connection, space: str, project_id: str, scores: list[Score]) -> set[str]:
        touched = set()
        for score in scores:
            if score.run_id is None or score.case_id is None:
                raise StoreError("invalid", "a stored score names its run and case")
            self._require_run(connection, space, project_id, score.run_id)
            saved = score.model_copy(update={"created_at": score.created_at or _now()})
            self._upsert(
                connection,
                score_rows,
                {
                    "space": space,
                    "project_id": project_id,
                    "run_id": score.run_id,
                    "case_id": score.case_id,
                    "scorer": score.scorer,
                    "scoring_job_id": score.scoring_job_id or "",
                    "source": score.source,
                    "created_at": saved.created_at,
                    "record": _json(saved),
                },
                ["space", "project_id", "run_id", "case_id", "scorer", "scoring_job_id", "source"],
            )
            touched.add(score.run_id)
        return touched

    async def put_scores(self, principal: Principal, project_id: str, scores: list[Score]) -> None:
        def work(connection: Connection, space: str) -> set[str]:
            return self._put_scores(connection, space, project_id, scores)

        for run_id in await self._run(principal, work):
            self._emit(principal, "scores", project_id, run_id)

    async def list_scores(
        self, principal: Principal, project_id: str, run_id: str, *, case_id: str | None = None
    ) -> list[Score]:
        def work(connection: Connection, space: str) -> list[Score]:
            self._require_run(connection, space, project_id, run_id)
            query = select(score_rows.c.record).where(
                score_rows.c.space == space, score_rows.c.project_id == project_id, score_rows.c.run_id == run_id
            )
            if case_id is not None:
                query = query.where(score_rows.c.case_id == case_id)
            query = query.order_by(
                score_rows.c.case_id, score_rows.c.scorer, score_rows.c.scoring_job_id, score_rows.c.source
            )
            found: Sequence[Any] = connection.execute(query).scalars().all()
            return [Score.model_validate(r) for r in found]

        return await self._run(principal, work)

    # --- scoring jobs ------------------------------------------------------------------------

    async def create_scoring_job(self, principal: Principal, project_id: str, job: ScoringJob) -> ScoringJob:
        def work(connection: Connection, space: str) -> tuple[ScoringJob, bool]:
            if stored := self._one(connection, scoring_jobs, space, project_id, id=job.id):
                return ScoringJob.model_validate(stored), False
            self._require_run(connection, space, project_id, job.run_id)
            saved = job.model_copy(update={"created_at": job.created_at or _now()})
            connection.execute(
                insert(scoring_jobs).values(
                    space=space,
                    project_id=project_id,
                    id=job.id,
                    run_id=job.run_id,
                    state=job.state,
                    created_at=saved.created_at,
                    record=_json(saved),
                )
            )
            return saved, True

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "scoring_job", project_id, job.id, run_id=job.run_id)
        return saved

    async def update_scoring_job(
        self, principal: Principal, project_id: str, job: ScoringJob, *, expected_state: RunState | None = None
    ) -> ScoringJob:
        def work(connection: Connection, space: str) -> ScoringJob:
            stored = self._one(connection, scoring_jobs, space, project_id, id=job.id)
            if stored is None:
                raise StoreError("not_found", "scoring job not found")
            saved = job.model_copy(update={"created_at": ScoringJob.model_validate(stored).created_at})
            statement = update(scoring_jobs).where(
                scoring_jobs.c.space == space, scoring_jobs.c.project_id == project_id, scoring_jobs.c.id == job.id
            )
            if expected_state is not None:
                statement = statement.where(scoring_jobs.c.state == expected_state)
            if connection.execute(statement.values(state=job.state, record=_json(saved))).rowcount == 0:
                raise StoreError("conflict", "the scoring job is no longer in the expected state")
            return saved

        saved = await self._run(principal, work)
        self._emit(principal, "scoring_job", project_id, job.id, run_id=job.run_id)
        return saved

    async def list_scoring_jobs(self, principal: Principal, project_id: str, run_id: str) -> list[ScoringJob]:
        def work(connection: Connection, space: str) -> list[ScoringJob]:
            self._require_run(connection, space, project_id, run_id)
            rows: Sequence[Any] = (
                connection.execute(
                    select(scoring_jobs.c.record)
                    .where(
                        scoring_jobs.c.space == space,
                        scoring_jobs.c.project_id == project_id,
                        scoring_jobs.c.run_id == run_id,
                    )
                    .order_by(scoring_jobs.c.created_at, scoring_jobs.c.id)
                )
                .scalars()
                .all()
            )
            return [ScoringJob.model_validate(r) for r in rows]

        return await self._run(principal, work)

    async def count_active_scoring_jobs(self, principal: Principal, *, up_to: int | None = None) -> int:
        def work(connection: Connection, space: str) -> int:
            active = (
                select(literal(1))
                .select_from(
                    scoring_jobs.join(
                        runs,
                        (runs.c.space == scoring_jobs.c.space)
                        & (runs.c.project_id == scoring_jobs.c.project_id)
                        & (runs.c.id == scoring_jobs.c.run_id),
                    )
                )
                .where(
                    scoring_jobs.c.space == space,
                    scoring_jobs.c.state.in_(_ACTIVE),
                    runs.c.space == space,
                    runs.c.state == "completed",
                )
            )
            return _count(connection, active, up_to)

        return await self._run(principal, work)

    # --- reviews -----------------------------------------------------------------------------

    async def add_review(self, principal: Principal, project_id: str, review: Review) -> Review:
        def work(connection: Connection, space: str) -> tuple[Review, bool]:
            if stored := self._one(connection, reviews, space, project_id, id=review.id):
                return Review.model_validate(stored), False
            self._require_run(connection, space, project_id, review.run_id)
            if review.previous_id is not None:
                previous = self._one(connection, reviews, space, project_id, id=review.previous_id)
                if previous is None:
                    raise StoreError("not_found", "the review it replaces was not found")
                earlier = Review.model_validate(previous)
                if (earlier.run_id, earlier.case_id, earlier.criterion) != (
                    review.run_id,
                    review.case_id,
                    review.criterion,
                ):
                    raise StoreError("invalid", "a review replaces one of the same case and criterion")
            saved = review.model_copy(update={"created_at": review.created_at or _now()})
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(reviews).values(
                            space=space,
                            project_id=project_id,
                            id=review.id,
                            run_id=review.run_id,
                            case_id=review.case_id,
                            previous_id=review.previous_id,
                            created_at=saved.created_at,
                            record=_json(saved),
                        )
                    )
            except IntegrityError:
                pass  # raised below, without the database's error, which holds the review
            else:
                return saved, True
            raise StoreError("conflict", "that review was already replaced")

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "review", project_id, review.id)
        return saved

    async def list_reviews(
        self,
        principal: Principal,
        project_id: str,
        run_id: str,
        *,
        case_id: str | None = None,
        current_only: bool = True,
    ) -> list[Review]:
        def work(connection: Connection, space: str) -> list[Review]:
            self._require_run(connection, space, project_id, run_id)
            query = select(reviews.c.record, reviews.c.id).where(
                reviews.c.space == space, reviews.c.project_id == project_id, reviews.c.run_id == run_id
            )
            if case_id is not None:
                query = query.where(reviews.c.case_id == case_id)
            rows: Sequence[Any] = connection.execute(query.order_by(reviews.c.created_at, reviews.c.id)).all()
            found = [Review.model_validate(row.record) for row in rows]
            if not current_only:
                return found
            replaced = {r.previous_id for r in found if r.previous_id is not None}
            return [r for r in found if r.id not in replaced]

        return await self._run(principal, work)

    # --- suites ------------------------------------------------------------------------------

    def _latest_suite(self, connection: Connection, space: str, project_id: str, suite_id: str) -> Suite | None:
        stored = connection.execute(
            select(suites.c.record)
            .where(suites.c.space == space, suites.c.project_id == project_id, suites.c.id == suite_id)
            .order_by(suites.c.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return Suite.model_validate(stored) if stored else None

    async def save_suite(self, principal: Principal, project_id: str, suite: Suite) -> Suite:
        def work(connection: Connection, space: str) -> tuple[Suite, bool]:
            latest = self._latest_suite(connection, space, project_id, suite.id)
            if latest is not None and latest.digest == suite.digest:
                return latest, False
            saved = suite.model_copy(
                update={
                    "version": (latest.version + 1) if latest else 1,
                    "created_at": suite.created_at or _now(),
                    "created_by": suite.created_by or principal.user_id,
                }
            )
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(suites).values(
                            space=space,
                            project_id=project_id,
                            id=suite.id,
                            version=saved.version,
                            digest=suite.digest,
                            created_at=saved.created_at,
                            record=_json(saved),
                        )
                    )
            except IntegrityError:
                pass  # raised below, without the database's error, which holds the suite
            else:
                return saved, True
            raise StoreError("conflict", "another version of the suite was saved at the same time")

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "suite", project_id, suite.id)
        return saved

    async def get_suite(
        self, principal: Principal, project_id: str, suite_id: str, *, version: int | None = None
    ) -> Suite | None:
        def work(connection: Connection, space: str) -> Suite | None:
            if version is None:
                return self._latest_suite(connection, space, project_id, suite_id)
            stored = self._one(connection, suites, space, project_id, id=suite_id, version=version)
            return Suite.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def list_suites(self, principal: Principal, project_id: str) -> list[Suite]:
        def work(connection: Connection, space: str) -> list[Suite]:
            rows: Sequence[Any] = (
                connection.execute(
                    select(suites.c.record)
                    .where(suites.c.space == space, suites.c.project_id == project_id)
                    .order_by(suites.c.id, suites.c.version)
                )
                .scalars()
                .all()
            )
            latest: dict[str, Suite] = {}
            for row in rows:
                suite = Suite.model_validate(row)
                latest[suite.id] = suite
            return sorted(latest.values(), key=lambda s: (s.name, s.id))

        return await self._run(principal, work)

    async def put_suite_audit(self, principal: Principal, project_id: str, audit: SuiteAudit) -> SuiteAudit:
        def work(connection: Connection, space: str) -> SuiteAudit:
            if self._one(connection, suites, space, project_id, id=audit.suite_id, version=audit.suite_version) is None:
                raise StoreError("not_found", "suite version not found")
            saved = audit.model_copy(
                update={"created_at": audit.created_at or _now(), "created_by": audit.created_by or principal.user_id}
            )
            values = {
                "space": space,
                "project_id": project_id,
                "suite_id": audit.suite_id,
                "suite_version": audit.suite_version,
                "created_at": saved.created_at,
                "record": _json(saved),
            }
            self._upsert(connection, suite_audits, values, ["space", "project_id", "suite_id", "suite_version"])
            return saved

        saved = await self._run(principal, work)
        self._emit(principal, "suite", project_id, audit.suite_id)
        return saved

    async def get_suite_audit(
        self, principal: Principal, project_id: str, suite_id: str, version: int
    ) -> SuiteAudit | None:
        def work(connection: Connection, space: str) -> SuiteAudit | None:
            stored = self._one(connection, suite_audits, space, project_id, suite_id=suite_id, suite_version=version)
            return SuiteAudit.model_validate(stored) if stored else None

        return await self._run(principal, work)

    # --- dataset drafts ----------------------------------------------------------------------

    def _draft(self, connection: Connection, space: str, project_id: str, draft_id: str) -> DatasetDraft:
        stored = self._one(connection, dataset_drafts, space, project_id, id=draft_id)
        if stored is None:
            raise StoreError("not_found", "draft not found")
        return DatasetDraft.model_validate(stored)

    def _save_draft(
        self, connection: Connection, space: str, draft: DatasetDraft, *, expected_revision: int | None
    ) -> None:
        statement = update(dataset_drafts).where(
            dataset_drafts.c.space == space,
            dataset_drafts.c.project_id == draft.project_id,
            dataset_drafts.c.id == draft.id,
        )
        if expected_revision is not None:
            statement = statement.where(dataset_drafts.c.revision == expected_revision)
        values = {"revision": draft.revision, "published": draft.state == "published", "record": _json(draft)}
        if connection.execute(statement.values(**values)).rowcount == 0:
            raise StoreError("conflict", "the draft was changed by someone else; reload it")

    def _editable(self, draft: DatasetDraft, expected_revision: int) -> None:
        if draft.state == "published":
            raise StoreError("conflict", "a published draft takes no more edits")
        if draft.revision != expected_revision:
            raise StoreError("conflict", "the draft was changed by someone else; reload it")

    async def create_draft(self, principal: Principal, project_id: str, draft: DatasetDraft) -> DatasetDraft:
        if draft.project_id != project_id:
            raise StoreError("invalid", "the draft belongs to another project")

        def work(connection: Connection, space: str) -> tuple[DatasetDraft, bool]:
            if stored := self._one(connection, dataset_drafts, space, project_id, id=draft.id):
                return DatasetDraft.model_validate(stored), False
            now = _now()
            saved = draft.model_copy(
                update={
                    "revision": 1,
                    "state": "editing",
                    "case_count": 0,
                    "published_digest": None,
                    "created_by": draft.created_by or principal.user_id,
                    "updated_by": principal.user_id,
                    "created_at": draft.created_at or now,
                    "updated_at": now,
                }
            )
            connection.execute(
                insert(dataset_drafts).values(
                    space=space, project_id=project_id, id=draft.id, revision=1, published=False, record=_json(saved)
                )
            )
            return saved, True

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "draft", project_id, draft.id)
        return saved

    async def get_draft(self, principal: Principal, project_id: str, draft_id: str) -> DatasetDraft | None:
        def work(connection: Connection, space: str) -> DatasetDraft | None:
            stored = self._one(connection, dataset_drafts, space, project_id, id=draft_id)
            return DatasetDraft.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def list_drafts(self, principal: Principal, project_id: str) -> list[DatasetDraft]:
        def work(connection: Connection, space: str) -> list[DatasetDraft]:
            rows: Sequence[Any] = (
                connection.execute(
                    select(dataset_drafts.c.record).where(
                        dataset_drafts.c.space == space, dataset_drafts.c.project_id == project_id
                    )
                )
                .scalars()
                .all()
            )
            return sorted((DatasetDraft.model_validate(r) for r in rows), key=lambda d: (d.name, d.id))

        return await self._run(principal, work)

    async def edit_draft(
        self,
        principal: Principal,
        project_id: str,
        draft_id: str,
        *,
        expected_revision: int,
        name: str | None = None,
        description: str | None = None,
    ) -> DatasetDraft:
        def work(connection: Connection, space: str) -> DatasetDraft:
            draft = self._draft(connection, space, project_id, draft_id)
            self._editable(draft, expected_revision)
            changes: dict[str, Any] = {
                "revision": draft.revision + 1,
                "updated_by": principal.user_id,
                "updated_at": _now(),
            }
            if name is not None:
                changes["name"] = name
            if description is not None:
                changes["description"] = description
            updated = DatasetDraft.model_validate(_json(draft.model_copy(update=changes)))
            self._save_draft(connection, space, updated, expected_revision=expected_revision)
            return updated

        saved = await self._run(principal, work)
        self._emit(principal, "draft", project_id, draft_id)
        return saved

    async def delete_draft(self, principal: Principal, project_id: str, draft_id: str) -> None:
        def work(connection: Connection, space: str) -> None:
            self._draft(connection, space, project_id, draft_id)
            for table, key in (
                (dataset_draft_cases, dataset_draft_cases.c.draft_id),
                (dataset_drafts, dataset_drafts.c.id),
            ):
                connection.execute(
                    delete(table).where(table.c.space == space, table.c.project_id == project_id, key == draft_id)
                )

        await self._run(principal, work)
        self._emit(principal, "draft", project_id, draft_id)

    def _count_cases(self, connection: Connection, space: str, project_id: str, draft_id: str) -> int:
        return connection.execute(
            select(func.count())
            .select_from(dataset_draft_cases)
            .where(
                dataset_draft_cases.c.space == space,
                dataset_draft_cases.c.project_id == project_id,
                dataset_draft_cases.c.draft_id == draft_id,
            )
        ).scalar_one()

    async def put_draft_cases(
        self, principal: Principal, project_id: str, draft_id: str, cases: list[Case], *, expected_revision: int
    ) -> DatasetDraft:
        ids = [case.id for case in cases]
        if len(set(ids)) != len(ids):
            raise StoreError("invalid", "case ids must be unique")

        def work(connection: Connection, space: str) -> DatasetDraft:
            draft = self._draft(connection, space, project_id, draft_id)
            self._editable(draft, expected_revision)
            where = [
                dataset_draft_cases.c.space == space,
                dataset_draft_cases.c.project_id == project_id,
                dataset_draft_cases.c.draft_id == draft_id,
            ]
            existing: dict[str, int] = dict(
                connection.execute(
                    select(dataset_draft_cases.c.case_id, dataset_draft_cases.c.ordinal).where(*where)
                ).all()
            )
            following = (max(existing.values()) + 1) if existing else 0
            for case in cases:
                ordinal = existing.get(case.id)
                if ordinal is None:
                    ordinal, following = following, following + 1
                self._upsert(
                    connection,
                    dataset_draft_cases,
                    {
                        "space": space,
                        "project_id": project_id,
                        "draft_id": draft_id,
                        "case_id": case.id,
                        "ordinal": ordinal,
                        "record": _json(case),
                    },
                    ["space", "project_id", "draft_id", "case_id"],
                )
            updated = draft.model_copy(
                update={
                    "revision": draft.revision + 1,
                    "case_count": self._count_cases(connection, space, project_id, draft_id),
                    "updated_by": principal.user_id,
                    "updated_at": _now(),
                }
            )
            self._save_draft(connection, space, updated, expected_revision=expected_revision)
            return updated

        saved = await self._run(principal, work)
        self._emit(principal, "draft", project_id, draft_id)
        return saved

    async def delete_draft_cases(
        self, principal: Principal, project_id: str, draft_id: str, case_ids: list[str], *, expected_revision: int
    ) -> DatasetDraft:
        def work(connection: Connection, space: str) -> DatasetDraft:
            draft = self._draft(connection, space, project_id, draft_id)
            self._editable(draft, expected_revision)
            connection.execute(
                delete(dataset_draft_cases).where(
                    dataset_draft_cases.c.space == space,
                    dataset_draft_cases.c.project_id == project_id,
                    dataset_draft_cases.c.draft_id == draft_id,
                    dataset_draft_cases.c.case_id.in_(case_ids),
                )
            )
            updated = draft.model_copy(
                update={
                    "revision": draft.revision + 1,
                    "case_count": self._count_cases(connection, space, project_id, draft_id),
                    "updated_by": principal.user_id,
                    "updated_at": _now(),
                }
            )
            self._save_draft(connection, space, updated, expected_revision=expected_revision)
            return updated

        saved = await self._run(principal, work)
        self._emit(principal, "draft", project_id, draft_id)
        return saved

    async def draft_cases(
        self, principal: Principal, project_id: str, draft_id: str, *, offset: int = 0, limit: int = 100
    ) -> Page[Case]:
        _bounds(offset, limit)

        def work(connection: Connection, space: str) -> Page[Case]:
            self._draft(connection, space, project_id, draft_id)
            total = self._count_cases(connection, space, project_id, draft_id)
            rows: Sequence[Any] = (
                connection.execute(
                    select(dataset_draft_cases.c.record)
                    .where(
                        dataset_draft_cases.c.space == space,
                        dataset_draft_cases.c.project_id == project_id,
                        dataset_draft_cases.c.draft_id == draft_id,
                    )
                    .order_by(dataset_draft_cases.c.ordinal)
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return _page([Case.model_validate(r) for r in rows], offset, limit, total)

        return await self._run(principal, work)

    async def publish_draft(
        self, principal: Principal, project_id: str, draft_id: str, *, expected_revision: int, digest: str
    ) -> DatasetDraft:
        def work(connection: Connection, space: str) -> DatasetDraft:
            draft = self._draft(connection, space, project_id, draft_id)
            self._editable(draft, expected_revision)
            published = draft.model_copy(
                update={
                    "state": "published",
                    "published_digest": digest,
                    "revision": draft.revision + 1,
                    "updated_by": principal.user_id,
                    "updated_at": _now(),
                }
            )
            DatasetDraft.model_validate(_json(published))  # the digest must be a digest
            self._save_draft(connection, space, published, expected_revision=expected_revision)
            return published

        saved = await self._run(principal, work)
        self._emit(principal, "draft", project_id, draft_id)
        return saved

    # --- saved trace views ---------------------------------------------------------------------

    async def save_view(
        self, principal: Principal, project_id: str, view: SavedView, *, expected_revision: int | None = None
    ) -> SavedView:
        if view.project_id != project_id:
            raise StoreError("invalid", "the view belongs to another project")

        def work(connection: Connection, space: str) -> tuple[SavedView, bool]:
            stored = self._one(connection, saved_views, space, project_id, id=view.id)
            now = _now()
            if expected_revision is None:
                if stored is not None:
                    return SavedView.model_validate(stored), False
                saved = view.model_copy(update={"revision": 1, "created_at": view.created_at or now, "updated_at": now})
                connection.execute(
                    insert(saved_views).values(
                        space=space, project_id=project_id, id=view.id, revision=1, record=_json(saved)
                    )
                )
                return saved, True
            if stored is None:
                raise StoreError("not_found", "view not found")
            earlier = SavedView.model_validate(stored)
            saved = view.model_copy(
                update={"revision": expected_revision + 1, "created_at": earlier.created_at, "updated_at": now}
            )
            changed = connection.execute(
                update(saved_views)
                .where(
                    saved_views.c.space == space,
                    saved_views.c.project_id == project_id,
                    saved_views.c.id == view.id,
                    saved_views.c.revision == expected_revision,
                )
                .values(revision=saved.revision, record=_json(saved))
            ).rowcount
            if changed == 0:
                raise StoreError("conflict", "the view was changed by someone else; reload it")
            return saved, True

        saved, changed = await self._run(principal, work)
        if changed:
            self._emit(principal, "view", project_id, view.id)
        return saved

    async def get_view(self, principal: Principal, project_id: str, view_id: str) -> SavedView | None:
        def work(connection: Connection, space: str) -> SavedView | None:
            stored = self._one(connection, saved_views, space, project_id, id=view_id)
            return SavedView.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def list_views(self, principal: Principal, project_id: str) -> list[SavedView]:
        def work(connection: Connection, space: str) -> list[SavedView]:
            rows: Sequence[Any] = (
                connection.execute(
                    select(saved_views.c.record).where(
                        saved_views.c.space == space, saved_views.c.project_id == project_id
                    )
                )
                .scalars()
                .all()
            )
            return sorted((SavedView.model_validate(r) for r in rows), key=lambda v: (v.name, v.id))

        return await self._run(principal, work)

    async def delete_view(self, principal: Principal, project_id: str, view_id: str, *, expected_revision: int) -> None:
        def work(connection: Connection, space: str) -> None:
            if self._one(connection, saved_views, space, project_id, id=view_id) is None:
                raise StoreError("not_found", "view not found")
            removed = connection.execute(
                delete(saved_views).where(
                    saved_views.c.space == space,
                    saved_views.c.project_id == project_id,
                    saved_views.c.id == view_id,
                    saved_views.c.revision == expected_revision,
                )
            ).rowcount
            if removed == 0:
                raise StoreError("conflict", "the view was changed by someone else; reload it")

        await self._run(principal, work)
        self._emit(principal, "view", project_id, view_id)

    # --- review queues with blind double review ----------------------------------------------

    async def add_blind_queue(self, principal: Principal, project_id: str, queue: BlindQueue) -> BlindQueue:
        def work(connection: Connection, space: str) -> tuple[BlindQueue, bool]:
            if stored := self._one(connection, blind_queues, space, project_id, id=queue.id):
                return BlindQueue.model_validate(stored), False
            self._require_run(connection, space, project_id, queue.run_id)
            saved = queue.model_copy(update={"created_at": queue.created_at or _now()})
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(blind_queues).values(
                            space=space,
                            project_id=project_id,
                            id=saved.id,
                            run_id=saved.run_id,
                            created_at=saved.created_at,
                            record=_json(saved),
                        )
                    )
            except IntegrityError:
                # The same queue recorded at the same time: the one kept first is the queue's.
                stored = self._one(connection, blind_queues, space, project_id, id=queue.id)
                return BlindQueue.model_validate(stored), False
            return saved, True

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "blind_queue", project_id, saved.id)
        return saved

    async def list_blind_queues(
        self, principal: Principal, project_id: str, *, run_id: str | None = None
    ) -> list[BlindQueue]:
        def work(connection: Connection, space: str) -> list[BlindQueue]:
            query = select(blind_queues.c.record).where(
                blind_queues.c.space == space, blind_queues.c.project_id == project_id
            )
            if run_id is not None:
                query = query.where(blind_queues.c.run_id == run_id)
            rows: Sequence[Any] = (
                connection.execute(query.order_by(blind_queues.c.created_at, blind_queues.c.id)).scalars().all()
            )
            return [BlindQueue.model_validate(r) for r in rows]

        return await self._run(principal, work)

    # --- judge calibrations --------------------------------------------------------------------

    async def save_calibration(
        self, principal: Principal, project_id: str, calibration: JudgeCalibration
    ) -> JudgeCalibration:
        if calibration.project_id != project_id:
            raise StoreError("invalid", "the calibration belongs to another project")

        def work(connection: Connection, space: str) -> tuple[JudgeCalibration, bool]:
            if stored := self._one(connection, judge_calibrations, space, project_id, id=calibration.id):
                return JudgeCalibration.model_validate(stored), False
            saved = calibration.model_copy(update={"created_at": calibration.created_at or _now()})
            connection.execute(
                insert(judge_calibrations).values(
                    space=space,
                    project_id=project_id,
                    id=saved.id,
                    judge=saved.judge,
                    version=saved.version,
                    created_at=saved.created_at,
                    record=_json(saved),
                )
            )
            return saved, True

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "calibration", project_id, saved.id)
        return saved

    async def get_calibration(
        self, principal: Principal, project_id: str, calibration_id: str
    ) -> JudgeCalibration | None:
        def work(connection: Connection, space: str) -> JudgeCalibration | None:
            stored = self._one(connection, judge_calibrations, space, project_id, id=calibration_id)
            return JudgeCalibration.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def list_calibrations(
        self, principal: Principal, project_id: str, *, judge: str | None = None
    ) -> list[JudgeCalibration]:
        def work(connection: Connection, space: str) -> list[JudgeCalibration]:
            query = select(judge_calibrations.c.record).where(
                judge_calibrations.c.space == space, judge_calibrations.c.project_id == project_id
            )
            if judge is not None:
                query = query.where(judge_calibrations.c.judge == judge)
            order = (judge_calibrations.c.created_at.desc(), judge_calibrations.c.id.desc())
            rows: Sequence[Any] = connection.execute(query.order_by(*order)).scalars().all()
            return [JudgeCalibration.model_validate(r) for r in rows]

        return await self._run(principal, work)

    # --- calibration re-checks -----------------------------------------------------------------

    async def create_recheck(
        self, principal: Principal, project_id: str, recheck: CalibrationRecheck
    ) -> CalibrationRecheck:
        if recheck.project_id != project_id:
            raise StoreError("invalid", "the re-check belongs to another project")

        def work(connection: Connection, space: str) -> tuple[CalibrationRecheck, bool]:
            if stored := self._one(connection, calibration_rechecks, space, project_id, id=recheck.id):
                return CalibrationRecheck.model_validate(stored), False
            saved = recheck.model_copy(update={"created_at": recheck.created_at or _now()})
            connection.execute(
                insert(calibration_rechecks).values(
                    space=space,
                    project_id=project_id,
                    id=saved.id,
                    judge=saved.judge,
                    version=saved.version,
                    state=saved.state,
                    created_at=saved.created_at,
                    record=_json(saved),
                )
            )
            return saved, True

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "recheck", project_id, saved.id)
        return saved

    async def get_recheck(self, principal: Principal, project_id: str, recheck_id: str) -> CalibrationRecheck | None:
        def work(connection: Connection, space: str) -> CalibrationRecheck | None:
            stored = self._one(connection, calibration_rechecks, space, project_id, id=recheck_id)
            return CalibrationRecheck.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def update_recheck(
        self,
        principal: Principal,
        project_id: str,
        recheck: CalibrationRecheck,
        *,
        expected_state: RecheckState | None = None,
    ) -> CalibrationRecheck:
        def work(connection: Connection, space: str) -> CalibrationRecheck:
            stored = self._one(connection, calibration_rechecks, space, project_id, id=recheck.id)
            if stored is None:
                raise StoreError("not_found", "re-check not found")
            saved = recheck.model_copy(update={"created_at": CalibrationRecheck.model_validate(stored).created_at})
            table = calibration_rechecks
            statement = update(table).where(
                table.c.space == space, table.c.project_id == project_id, table.c.id == recheck.id
            )
            if expected_state is not None:
                statement = statement.where(table.c.state == expected_state)
            if connection.execute(statement.values(state=saved.state, record=_json(saved))).rowcount == 0:
                raise StoreError("conflict", "the re-check is no longer in the expected state")
            return saved

        saved = await self._run(principal, work)
        self._emit(principal, "recheck", project_id, saved.id)
        return saved

    async def list_rechecks(
        self, principal: Principal, project_id: str, *, judge: str | None = None
    ) -> list[CalibrationRecheck]:
        def work(connection: Connection, space: str) -> list[CalibrationRecheck]:
            table = calibration_rechecks
            query = select(table.c.record).where(table.c.space == space, table.c.project_id == project_id)
            if judge is not None:
                query = query.where(table.c.judge == judge)
            order = (table.c.created_at.desc(), table.c.id.desc())
            rows: Sequence[Any] = connection.execute(query.order_by(*order)).scalars().all()
            return [CalibrationRecheck.model_validate(r) for r in rows]

        return await self._run(principal, work)

    # --- dataset manifests -------------------------------------------------------------------

    async def begin_publication(
        self, principal: Principal, project_id: str, manifest: DatasetManifest
    ) -> DatasetManifest:
        if manifest.project_id != project_id:
            raise StoreError("invalid", "the manifest belongs to another project")

        def work(connection: Connection, space: str) -> DatasetManifest:
            if stored := self._one(connection, dataset_manifests, space, project_id, id=manifest.id):
                return DatasetManifest.model_validate(stored)
            saved = manifest.model_copy(
                update={"state": "publishing", "dataset_id": None, "started_at": manifest.started_at or _now()}
            )
            connection.execute(
                insert(dataset_manifests).values(
                    space=space,
                    project_id=project_id,
                    id=saved.id,
                    dataset_id=None,
                    state=saved.state,
                    started_at=saved.started_at,
                    record=_json(saved),
                )
            )
            return saved

        return await self._run(principal, work)

    async def finish_publication(
        self, principal: Principal, project_id: str, manifest_id: str, dataset_id: str
    ) -> DatasetManifest:
        def work(connection: Connection, space: str) -> DatasetManifest:
            stored = self._one(connection, dataset_manifests, space, project_id, id=manifest_id)
            if stored is None:
                raise StoreError("not_found", "publication not found")
            manifest = DatasetManifest.model_validate(stored)
            if manifest.state == "published":
                if manifest.dataset_id != dataset_id:
                    raise StoreError("conflict", "the publication was already finished as another dataset")
                return manifest
            saved = manifest.model_copy(update={"state": "published", "dataset_id": dataset_id, "published_at": _now()})
            connection.execute(
                update(dataset_manifests)
                .where(
                    dataset_manifests.c.space == space,
                    dataset_manifests.c.project_id == project_id,
                    dataset_manifests.c.id == manifest_id,
                )
                .values(dataset_id=dataset_id, state=saved.state, record=_json(saved))
            )
            return saved

        return await self._run(principal, work)

    async def manifest_of(self, principal: Principal, project_id: str, dataset_id: str) -> DatasetManifest | None:
        def work(connection: Connection, space: str) -> DatasetManifest | None:
            # Two drafts with the same name and cases publish the same version: the newest manifest.
            stored = connection.execute(
                select(dataset_manifests.c.record)
                .where(
                    dataset_manifests.c.space == space,
                    dataset_manifests.c.project_id == project_id,
                    dataset_manifests.c.dataset_id == dataset_id,
                    dataset_manifests.c.state == "published",
                )
                .order_by(dataset_manifests.c.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            return DatasetManifest.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def list_publications(self, principal: Principal, project_id: str) -> list[DatasetManifest]:
        def work(connection: Connection, space: str) -> list[DatasetManifest]:
            rows: Sequence[Any] = (
                connection.execute(
                    select(dataset_manifests.c.record)
                    .where(dataset_manifests.c.space == space, dataset_manifests.c.project_id == project_id)
                    .order_by(dataset_manifests.c.started_at.desc(), dataset_manifests.c.id)
                )
                .scalars()
                .all()
            )
            return [DatasetManifest.model_validate(r) for r in rows]

        return await self._run(principal, work)

    # --- the outbox --------------------------------------------------------------------------

    def _put_delivery(self, connection: Connection, space: str, project_id: str, delivery: Delivery) -> None:
        values = {
            "space": space,
            "project_id": project_id,
            "id": delivery.id,
            "kind": delivery.kind,
            "state": delivery.state,
            "next_attempt_at": delivery.next_attempt_at,
            "created_at": delivery.created_at,
            "record": _json(delivery),
        }
        self._upsert(connection, deliveries, values, ["space", "project_id", "id"])

    async def put_delivery(self, principal: Principal, project_id: str, delivery: Delivery) -> Delivery:
        if delivery.project_id != project_id:
            raise StoreError("invalid", "the delivery belongs to another project")
        now = _now()
        saved = delivery.model_copy(
            update={
                "state": "pending",
                "attempts": 0,
                "error": "",
                "next_attempt_at": now,
                "created_at": delivery.created_at or now,
                "delivered_at": None,
            }
        )

        def work(connection: Connection, space: str) -> Delivery:
            self._put_delivery(connection, space, project_id, saved)
            return saved

        return await self._run(principal, work)

    async def settle_delivery(self, principal: Principal, project_id: str, delivery: Delivery) -> Delivery:
        if delivery.next_attempt_at is None or delivery.created_at is None:
            raise StoreError("invalid", "a settled delivery has its times")

        def work(connection: Connection, space: str) -> Delivery:
            if self._one(connection, deliveries, space, project_id, id=delivery.id) is None:
                raise StoreError("not_found", "delivery not found")
            self._put_delivery(connection, space, project_id, delivery)
            return delivery

        return await self._run(principal, work)

    async def get_delivery(self, principal: Principal, project_id: str, delivery_id: str) -> Delivery | None:
        def work(connection: Connection, space: str) -> Delivery | None:
            stored = self._one(connection, deliveries, space, project_id, id=delivery_id)
            return Delivery.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def due_deliveries(self, principal: Principal, *, now: datetime, limit: int = 100) -> list[Delivery]:
        _bounds(0, limit)

        def work(connection: Connection, space: str) -> list[Delivery]:
            rows: Sequence[Any] = (
                connection.execute(
                    select(deliveries.c.record)
                    .where(
                        deliveries.c.space == space,
                        deliveries.c.state == "pending",
                        deliveries.c.next_attempt_at <= now,
                    )
                    .order_by(deliveries.c.created_at, deliveries.c.id)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [Delivery.model_validate(r) for r in rows]

        return await self._run(principal, work)

    # --- monitors ------------------------------------------------------------------------------

    async def create_monitor(self, principal: Principal, project_id: str, monitor: Monitor) -> Monitor:
        if monitor.project_id != project_id:
            raise StoreError("invalid", "the monitor belongs to another project")

        def work(connection: Connection, space: str) -> tuple[Monitor, bool]:
            if stored := self._one(connection, monitors, space, project_id, id=monitor.id):
                return Monitor.model_validate(stored), False
            now = _now()
            saved = monitor.model_copy(
                update={
                    "revision": 1,
                    "created_at": monitor.created_at or now,
                    "updated_at": now,
                    "active_since": monitor.active_since or monitor.created_at or now,
                    "created_by": monitor.created_by or principal.user_id,
                }
            )
            connection.execute(
                insert(monitors).values(
                    space=space,
                    project_id=project_id,
                    id=monitor.id,
                    state=saved.state,
                    revision=1,
                    record=_json(saved),
                    progress=_json(MonitorProgress()),
                )
            )
            return saved, True

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "monitor", project_id, monitor.id)
        return saved

    async def get_monitor(self, principal: Principal, project_id: str, monitor_id: str) -> Monitor | None:
        def work(connection: Connection, space: str) -> Monitor | None:
            stored = self._one(connection, monitors, space, project_id, id=monitor_id)
            return Monitor.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def list_monitors(self, principal: Principal, project_id: str) -> list[Monitor]:
        def work(connection: Connection, space: str) -> list[Monitor]:
            rows: Sequence[Any] = (
                connection.execute(
                    select(monitors.c.record).where(monitors.c.space == space, monitors.c.project_id == project_id)
                )
                .scalars()
                .all()
            )
            return sorted((Monitor.model_validate(r) for r in rows), key=lambda m: (m.name, m.id))

        return await self._run(principal, work)

    async def update_monitor(
        self, principal: Principal, project_id: str, monitor: Monitor, *, expected_revision: int
    ) -> Monitor:
        def work(connection: Connection, space: str) -> Monitor:
            stored = self._one(connection, monitors, space, project_id, id=monitor.id)
            if stored is None:
                raise StoreError("not_found", "monitor not found")
            earlier = Monitor.model_validate(stored)
            saved = monitor.model_copy(
                update={
                    "revision": expected_revision + 1,
                    "created_at": earlier.created_at,
                    "created_by": earlier.created_by,
                    "updated_at": _now(),
                }
            )
            changed = connection.execute(
                update(monitors)
                .where(
                    monitors.c.space == space,
                    monitors.c.project_id == project_id,
                    monitors.c.id == monitor.id,
                    monitors.c.revision == expected_revision,
                )
                .values(state=saved.state, revision=saved.revision, record=_json(saved))
            ).rowcount
            if changed == 0:
                raise StoreError("conflict", "the monitor was changed by someone else; reload it")
            return saved

        saved = await self._run(principal, work)
        self._emit(principal, "monitor", project_id, monitor.id)
        return saved

    async def delete_monitor(self, principal: Principal, project_id: str, monitor_id: str) -> None:
        def work(connection: Connection, space: str) -> list[str]:
            if self._one(connection, monitors, space, project_id, id=monitor_id) is None:
                raise StoreError("not_found", "monitor not found")
            gone = self._delete_thresholds_on(connection, space, project_id, "monitor", monitor_id)
            for table, key in ((monitored_traces, monitored_traces.c.monitor_id), (monitors, monitors.c.id)):
                connection.execute(
                    delete(table).where(table.c.space == space, table.c.project_id == project_id, key == monitor_id)
                )
            return gone

        for threshold_id in await self._run(principal, work):
            self._emit(principal, "threshold", project_id, threshold_id)
        self._emit(principal, "monitor", project_id, monitor_id)

    async def monitor_progress(self, principal: Principal, project_id: str, monitor_id: str) -> MonitorProgress | None:
        def work(connection: Connection, space: str) -> MonitorProgress | None:
            stored = connection.execute(
                select(monitors.c.progress).where(
                    monitors.c.space == space, monitors.c.project_id == project_id, monitors.c.id == monitor_id
                )
            ).scalar_one_or_none()
            return MonitorProgress.model_validate(stored) if stored is not None else None

        return await self._run(principal, work)

    async def set_monitor_progress(
        self, principal: Principal, project_id: str, monitor_id: str, progress: MonitorProgress
    ) -> None:
        def work(connection: Connection, space: str) -> int:
            return connection.execute(
                update(monitors)
                .where(monitors.c.space == space, monitors.c.project_id == project_id, monitors.c.id == monitor_id)
                .values(progress=_json(progress))
            ).rowcount

        if await self._run(principal, work):
            self._emit(principal, "monitor", project_id, monitor_id)

    async def claim_monitored_trace(
        self, principal: Principal, project_id: str, monitor_id: str, trace_id: str, *, pass_id: str
    ) -> bool:
        def work(connection: Connection, space: str) -> bool:
            state = connection.execute(
                select(monitors.c.state).where(
                    monitors.c.space == space, monitors.c.project_id == project_id, monitors.c.id == monitor_id
                )
            ).scalar_one_or_none()
            if state != "active":
                return False
            key = (
                monitored_traces.c.space == space,
                monitored_traces.c.project_id == project_id,
                monitored_traces.c.monitor_id == monitor_id,
                monitored_traces.c.trace_id == trace_id,
            )
            found = connection.execute(select(monitored_traces.c.state, monitored_traces.c.pass_id).where(*key)).first()
            if found is None:
                taken = MonitoredTrace(
                    monitor_id=monitor_id, trace_id=trace_id, state="scoring", pass_id=pass_id, created_at=_now()
                )
                try:
                    with connection.begin_nested():
                        connection.execute(
                            insert(monitored_traces).values(
                                space=space,
                                project_id=project_id,
                                monitor_id=monitor_id,
                                trace_id=trace_id,
                                state="scoring",
                                pass_id=pass_id,
                                created_at=taken.created_at,
                                record=_json(taken),
                            )
                        )
                except IntegrityError:
                    return False  # another pass took it at the same moment
                return True
            if found.state != "scoring":
                return False
            if found.pass_id == pass_id:
                return True
            # An earlier pass took it and stopped before storing its scores: this pass takes it over.
            return bool(
                connection.execute(
                    update(monitored_traces)
                    .where(*key, monitored_traces.c.state == "scoring", monitored_traces.c.pass_id == found.pass_id)
                    .values(pass_id=pass_id)
                ).rowcount
            )

        return await self._run(principal, work)

    async def finish_monitored_trace(self, principal: Principal, project_id: str, trace: MonitoredTrace) -> None:
        def work(connection: Connection, space: str) -> None:
            stored = connection.execute(
                select(monitored_traces.c.record).where(
                    monitored_traces.c.space == space,
                    monitored_traces.c.project_id == project_id,
                    monitored_traces.c.monitor_id == trace.monitor_id,
                    monitored_traces.c.trace_id == trace.trace_id,
                )
            ).scalar_one_or_none()
            taken = MonitoredTrace.model_validate(stored).created_at if stored else None
            created = taken or trace.created_at or _now()
            saved = trace.model_copy(update={"created_at": created})
            self._upsert(
                connection,
                monitored_traces,
                {
                    "space": space,
                    "project_id": project_id,
                    "monitor_id": trace.monitor_id,
                    "trace_id": trace.trace_id,
                    "state": trace.state,
                    "pass_id": trace.pass_id,
                    "created_at": created,
                    "record": _json(saved),
                },
                ["space", "project_id", "monitor_id", "trace_id"],
            )

        await self._run(principal, work)

    async def list_monitored_traces(
        self, principal: Principal, project_id: str, monitor_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> Page[MonitoredTrace]:
        offset = int(cursor) if cursor and ascii_digits(cursor) else 0
        _bounds(offset, limit)

        def work(connection: Connection, space: str) -> Page[MonitoredTrace]:
            where = [
                monitored_traces.c.space == space,
                monitored_traces.c.project_id == project_id,
                monitored_traces.c.monitor_id == monitor_id,
            ]
            total = connection.execute(select(func.count()).select_from(monitored_traces).where(*where)).scalar_one()
            rows: Sequence[Any] = (
                connection.execute(
                    select(monitored_traces.c.record)
                    .where(*where)
                    .order_by(monitored_traces.c.created_at.desc(), monitored_traces.c.trace_id.desc())
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return _page([MonitoredTrace.model_validate(r) for r in rows], offset, limit, total)

        return await self._run(principal, work)

    async def scored_monitored_traces(
        self, principal: Principal, project_id: str, monitor_id: str, *, since: datetime, until: datetime, limit: int
    ) -> list[MonitoredTrace]:
        if not 1 <= limit <= THRESHOLD_WINDOW_BOUND:
            raise StoreError("invalid", f"limit must be between 1 and {THRESHOLD_WINDOW_BOUND}")

        def work(connection: Connection, space: str) -> list[MonitoredTrace]:
            rows: Sequence[Any] = (
                connection.execute(
                    select(monitored_traces.c.record)
                    .where(
                        monitored_traces.c.space == space,
                        monitored_traces.c.project_id == project_id,
                        monitored_traces.c.monitor_id == monitor_id,
                        monitored_traces.c.state == "scored",
                        monitored_traces.c.created_at >= since.astimezone(UTC),
                        monitored_traces.c.created_at < until.astimezone(UTC),
                    )
                    .order_by(monitored_traces.c.created_at.desc(), monitored_traces.c.trace_id.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [MonitoredTrace.model_validate(r) for r in rows]

        return await self._run(principal, work)

    async def forget_traces(self, principal: Principal, project_id: str, trace_ids: list[str]) -> dict[str, int]:
        check_trace_deletion(trace_ids=trace_ids)
        ids = list(dict.fromkeys(trace_ids))

        def work(connection: Connection, space: str) -> tuple[dict[str, int], list[str]]:
            taken = (
                monitored_traces.c.space == space,
                monitored_traces.c.project_id == project_id,
                monitored_traces.c.trace_id.in_(ids),
            )
            monitor_ids: list[str] = list(
                connection.execute(select(monitored_traces.c.monitor_id).where(*taken).distinct()).scalars()
            )
            removed = {monitored_traces.name: connection.execute(delete(monitored_traces).where(*taken)).rowcount}
            # A score's delivery names its trace in the score, a review queue item's beside the queue.
            score_trace = deliveries.c.record[("payload", "score", "trace_id")].as_string()
            item_trace = deliveries.c.record[("payload", "trace_id")].as_string()
            to_them = ((deliveries.c.kind == "score") & score_trace.in_(ids)) | (
                (deliveries.c.kind == "queue_item") & item_trace.in_(ids)
            )
            removed[deliveries.name] = connection.execute(
                delete(deliveries).where(deliveries.c.space == space, deliveries.c.project_id == project_id, to_them)
            ).rowcount
            return removed, monitor_ids

        removed, monitor_ids = await self._run(principal, work)
        for monitor_id in monitor_ids:
            self._emit(principal, "monitor", project_id, monitor_id)
        return removed

    # --- thresholds and their alerts -------------------------------------------------------------

    async def create_threshold(
        self, principal: Principal, project_id: str, threshold: Threshold, *, webhook_secret: str | None
    ) -> Threshold:
        if threshold.project_id != project_id:
            raise StoreError("invalid", "the threshold belongs to another project")
        if threshold.webhook is None and webhook_secret is not None:
            raise StoreError("invalid", "a threshold without a webhook has no secret")

        def work(connection: Connection, space: str) -> tuple[Threshold, bool]:
            if stored := self._one(connection, thresholds, space, project_id, id=threshold.id):
                return Threshold.model_validate(stored), False
            saved = threshold.model_copy(
                update={
                    "created_at": threshold.created_at or _now(),
                    "created_by": threshold.created_by or principal.user_id,
                }
            )
            connection.execute(
                insert(thresholds).values(
                    space=space,
                    project_id=project_id,
                    id=threshold.id,
                    source_kind=threshold.source.kind,
                    source_id=threshold.source.source_id,
                    sequence=0,
                    record=_json(saved),
                    status=_json(ThresholdStatus()),
                    secret=webhook_secret,
                )
            )
            return saved, True

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "threshold", project_id, threshold.id)
        return saved

    async def get_threshold(self, principal: Principal, project_id: str, threshold_id: str) -> Threshold | None:
        def work(connection: Connection, space: str) -> Threshold | None:
            stored = self._one(connection, thresholds, space, project_id, id=threshold_id)
            return Threshold.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def list_thresholds(
        self, principal: Principal, project_id: str, *, source_kind: str | None = None, source_id: str | None = None
    ) -> list[Threshold]:
        if (source_kind is None) != (source_id is None):
            raise StoreError("invalid", "name a source by its kind and its id together")

        def work(connection: Connection, space: str) -> list[Threshold]:
            query = select(thresholds.c.record).where(
                thresholds.c.space == space, thresholds.c.project_id == project_id
            )
            if source_kind is not None:
                query = query.where(thresholds.c.source_kind == source_kind, thresholds.c.source_id == source_id)
            rows: Sequence[Any] = connection.execute(query).scalars().all()
            return sorted((Threshold.model_validate(r) for r in rows), key=lambda t: (t.name, t.id))

        return await self._run(principal, work)

    async def delete_threshold(self, principal: Principal, project_id: str, threshold_id: str) -> None:
        def work(connection: Connection, space: str) -> None:
            if self._one(connection, thresholds, space, project_id, id=threshold_id) is None:
                raise StoreError("not_found", "threshold not found")
            connection.execute(
                delete(threshold_alerts).where(
                    threshold_alerts.c.space == space,
                    threshold_alerts.c.project_id == project_id,
                    threshold_alerts.c.threshold_id == threshold_id,
                )
            )
            connection.execute(
                delete(thresholds).where(
                    thresholds.c.space == space, thresholds.c.project_id == project_id, thresholds.c.id == threshold_id
                )
            )

        await self._run(principal, work)
        self._emit(principal, "threshold", project_id, threshold_id)

    async def threshold_status(
        self, principal: Principal, project_id: str, threshold_id: str
    ) -> ThresholdStatus | None:
        def work(connection: Connection, space: str) -> ThresholdStatus | None:
            stored = connection.execute(
                select(thresholds.c.status).where(
                    thresholds.c.space == space, thresholds.c.project_id == project_id, thresholds.c.id == threshold_id
                )
            ).scalar_one_or_none()
            return ThresholdStatus.model_validate(stored) if stored is not None else None

        return await self._run(principal, work)

    async def set_threshold_status(
        self,
        principal: Principal,
        project_id: str,
        threshold_id: str,
        status: ThresholdStatus,
        *,
        expected_sequence: int,
        alerts: Sequence[Alert] = (),
    ) -> bool:
        if any(alert.threshold_id != threshold_id for alert in alerts):
            raise StoreError("invalid", "an alert belongs to another threshold")

        def work(connection: Connection, space: str) -> bool:
            changed = connection.execute(
                update(thresholds)
                .where(
                    thresholds.c.space == space,
                    thresholds.c.project_id == project_id,
                    thresholds.c.id == threshold_id,
                    thresholds.c.sequence == expected_sequence,
                )
                .values(sequence=status.sequence, status=_json(status))
            ).rowcount
            if not changed:
                return False
            now = _now()
            for alert in alerts:
                if self._one(connection, threshold_alerts, space, project_id, id=alert.id) is not None:
                    continue
                saved = alert.model_copy(
                    update={"created_at": alert.created_at or now, "next_attempt_at": alert.next_attempt_at or now}
                )
                connection.execute(
                    insert(threshold_alerts).values(
                        space=space,
                        project_id=project_id,
                        id=alert.id,
                        threshold_id=threshold_id,
                        state=saved.state,
                        next_attempt_at=saved.next_attempt_at if saved.state == "pending" else None,
                        created_at=saved.created_at,
                        record=_json(saved),
                    )
                )
            return True

        written = await self._run(principal, work)
        if written and status.sequence != expected_sequence:
            self._emit(principal, "threshold", project_id, threshold_id)
        return written

    async def threshold_secret(self, principal: Principal, project_id: str, threshold_id: str) -> str | None:
        def work(connection: Connection, space: str) -> str | None:
            found: str | None = connection.execute(
                select(thresholds.c.secret).where(
                    thresholds.c.space == space, thresholds.c.project_id == project_id, thresholds.c.id == threshold_id
                )
            ).scalar_one_or_none()
            return found

        return await self._run(principal, work)

    async def threshold_signed(self, principal: Principal, project_id: str, threshold_id: str) -> bool:
        def work(connection: Connection, space: str) -> bool:
            return (
                connection.execute(
                    select(thresholds.c.id).where(
                        thresholds.c.space == space,
                        thresholds.c.project_id == project_id,
                        thresholds.c.id == threshold_id,
                        thresholds.c.secret.is_not(None),
                    )
                ).first()
                is not None
            )

        return await self._run(principal, work)

    async def set_threshold_secret(self, principal: Principal, project_id: str, threshold_id: str, secret: str) -> bool:
        def work(connection: Connection, space: str) -> bool:
            stored = self._one(connection, thresholds, space, project_id, id=threshold_id)
            if stored is None or Threshold.model_validate(stored).webhook is None:
                return False
            return bool(
                connection.execute(
                    update(thresholds)
                    .where(
                        thresholds.c.space == space,
                        thresholds.c.project_id == project_id,
                        thresholds.c.id == threshold_id,
                        thresholds.c.secret.is_(None),
                    )
                    .values(secret=secret)
                ).rowcount
            )

        written = await self._run(principal, work)
        if written:
            self._emit(principal, "threshold", project_id, threshold_id)
        return written

    async def take_due_alerts(
        self,
        principal: Principal,
        project_id: str,
        threshold_ids: Sequence[str],
        *,
        now: datetime,
        hold_until: datetime,
        limit: int = 20,
    ) -> list[Alert]:
        _bounds(0, limit, 100)
        ids = list(threshold_ids)
        if not ids:
            return []

        def work(connection: Connection, space: str) -> list[Alert]:
            due = (
                threshold_alerts.c.space == space,
                threshold_alerts.c.project_id == project_id,
                threshold_alerts.c.state == "pending",
                threshold_alerts.c.next_attempt_at <= now.astimezone(UTC),
            )
            rows: Sequence[Any] = connection.execute(
                select(threshold_alerts.c.id, threshold_alerts.c.record)
                .where(*due, threshold_alerts.c.threshold_id.in_(ids))
                .order_by(threshold_alerts.c.created_at, threshold_alerts.c.id)
                .limit(limit)
            ).all()
            taken = []
            for alert_id, record in rows:
                held = Alert.model_validate(record).model_copy(update={"next_attempt_at": hold_until})
                # Still due: no other delivery took it since it was read.
                if connection.execute(
                    update(threshold_alerts)
                    .where(*due, threshold_alerts.c.id == alert_id)
                    .values(next_attempt_at=hold_until.astimezone(UTC), record=_json(held))
                ).rowcount:
                    taken.append(held)
            return taken

        return await self._run(principal, work)

    async def finish_alert(self, principal: Principal, project_id: str, alert: Alert) -> None:
        def work(connection: Connection, space: str) -> None:
            connection.execute(
                update(threshold_alerts)
                .where(
                    threshold_alerts.c.space == space,
                    threshold_alerts.c.project_id == project_id,
                    threshold_alerts.c.id == alert.id,
                )
                .values(
                    state=alert.state,
                    next_attempt_at=alert.next_attempt_at if alert.state == "pending" else None,
                    record=_json(alert),
                )
            )

        await self._run(principal, work)

    async def list_alerts(
        self, principal: Principal, project_id: str, threshold_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> Page[Alert]:
        offset = int(cursor) if cursor and ascii_digits(cursor) else 0
        _bounds(offset, limit)

        def work(connection: Connection, space: str) -> Page[Alert]:
            where = [
                threshold_alerts.c.space == space,
                threshold_alerts.c.project_id == project_id,
                threshold_alerts.c.threshold_id == threshold_id,
            ]
            total = connection.execute(select(func.count()).select_from(threshold_alerts).where(*where)).scalar_one()
            rows: Sequence[Any] = (
                connection.execute(
                    select(threshold_alerts.c.record)
                    .where(*where)
                    .order_by(threshold_alerts.c.created_at.desc(), threshold_alerts.c.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return _page([Alert.model_validate(r) for r in rows], offset, limit, total)

        return await self._run(principal, work)

    # --- schedules -----------------------------------------------------------------------------

    async def create_schedule(self, principal: Principal, project_id: str, schedule: Schedule) -> Schedule:
        if schedule.project_id != project_id:
            raise StoreError("invalid", "the schedule belongs to another project")

        def work(connection: Connection, space: str) -> tuple[Schedule, bool]:
            if stored := self._one(connection, schedules, space, project_id, id=schedule.id):
                return Schedule.model_validate(stored), False
            now = _now()
            saved = schedule.model_copy(
                update={
                    "revision": 1,
                    "created_at": schedule.created_at or now,
                    "updated_at": now,
                    "active_since": schedule.active_since or schedule.created_at or now,
                    "created_by": schedule.created_by or principal.user_id,
                }
            )
            connection.execute(
                insert(schedules).values(
                    space=space,
                    project_id=project_id,
                    id=schedule.id,
                    state=saved.state,
                    revision=1,
                    record=_json(saved),
                    progress=_json(ScheduleProgress()),
                )
            )
            return saved, True

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "schedule", project_id, schedule.id)
        return saved

    async def get_schedule(self, principal: Principal, project_id: str, schedule_id: str) -> Schedule | None:
        def work(connection: Connection, space: str) -> Schedule | None:
            stored = self._one(connection, schedules, space, project_id, id=schedule_id)
            return Schedule.model_validate(stored) if stored else None

        return await self._run(principal, work)

    async def list_schedules(self, principal: Principal, project_id: str) -> list[Schedule]:
        def work(connection: Connection, space: str) -> list[Schedule]:
            rows: Sequence[Any] = (
                connection.execute(
                    select(schedules.c.record).where(schedules.c.space == space, schedules.c.project_id == project_id)
                )
                .scalars()
                .all()
            )
            return sorted((Schedule.model_validate(r) for r in rows), key=lambda s: (s.name, s.id))

        return await self._run(principal, work)

    async def update_schedule(
        self, principal: Principal, project_id: str, schedule: Schedule, *, expected_revision: int
    ) -> Schedule:
        def work(connection: Connection, space: str) -> Schedule:
            stored = self._one(connection, schedules, space, project_id, id=schedule.id)
            if stored is None:
                raise StoreError("not_found", "schedule not found")
            earlier = Schedule.model_validate(stored)
            saved = schedule.model_copy(
                update={
                    "revision": expected_revision + 1,
                    "created_at": earlier.created_at,
                    "created_by": earlier.created_by,
                    "updated_at": _now(),
                }
            )
            changed = connection.execute(
                update(schedules)
                .where(
                    schedules.c.space == space,
                    schedules.c.project_id == project_id,
                    schedules.c.id == schedule.id,
                    schedules.c.revision == expected_revision,
                )
                .values(state=saved.state, revision=saved.revision, record=_json(saved))
            ).rowcount
            if changed == 0:
                raise StoreError("conflict", "the schedule was changed by someone else; reload it")
            return saved

        saved = await self._run(principal, work)
        self._emit(principal, "schedule", project_id, schedule.id)
        return saved

    async def delete_schedule(self, principal: Principal, project_id: str, schedule_id: str) -> None:
        def work(connection: Connection, space: str) -> list[str]:
            if self._one(connection, schedules, space, project_id, id=schedule_id) is None:
                raise StoreError("not_found", "schedule not found")
            gone = self._delete_thresholds_on(connection, space, project_id, "gate", schedule_id)
            for table, key in ((schedule_runs, schedule_runs.c.schedule_id), (schedules, schedules.c.id)):
                connection.execute(
                    delete(table).where(table.c.space == space, table.c.project_id == project_id, key == schedule_id)
                )
            return gone

        for threshold_id in await self._run(principal, work):
            self._emit(principal, "threshold", project_id, threshold_id)
        self._emit(principal, "schedule", project_id, schedule_id)

    @staticmethod
    def _delete_thresholds_on(
        connection: Connection, space: str, project_id: str, source_kind: str, source_id: str
    ) -> list[str]:
        """Remove the thresholds on one source, with their alerts; their ids."""
        on_it = (
            thresholds.c.space == space,
            thresholds.c.project_id == project_id,
            thresholds.c.source_kind == source_kind,
            thresholds.c.source_id == source_id,
        )
        gone: list[str] = list(connection.execute(select(thresholds.c.id).where(*on_it)).scalars().all())
        if gone:
            connection.execute(
                delete(threshold_alerts).where(
                    threshold_alerts.c.space == space,
                    threshold_alerts.c.project_id == project_id,
                    threshold_alerts.c.threshold_id.in_(gone),
                )
            )
            connection.execute(delete(thresholds).where(*on_it))
        return gone

    async def schedule_progress(
        self, principal: Principal, project_id: str, schedule_id: str
    ) -> ScheduleProgress | None:
        def work(connection: Connection, space: str) -> ScheduleProgress | None:
            stored = connection.execute(
                select(schedules.c.progress).where(
                    schedules.c.space == space, schedules.c.project_id == project_id, schedules.c.id == schedule_id
                )
            ).scalar_one_or_none()
            return ScheduleProgress.model_validate(stored) if stored is not None else None

        return await self._run(principal, work)

    async def set_schedule_progress(
        self, principal: Principal, project_id: str, schedule_id: str, progress: ScheduleProgress
    ) -> None:
        def work(connection: Connection, space: str) -> int:
            return connection.execute(
                update(schedules)
                .where(schedules.c.space == space, schedules.c.project_id == project_id, schedules.c.id == schedule_id)
                .values(progress=_json(progress))
            ).rowcount

        if await self._run(principal, work):
            self._emit(principal, "schedule", project_id, schedule_id)

    def _scheduled_run(
        self, connection: Connection, space: str, project_id: str, schedule_id: str, slot: datetime
    ) -> ScheduledRun | None:
        stored = connection.execute(
            select(schedule_runs.c.record).where(
                schedule_runs.c.space == space,
                schedule_runs.c.project_id == project_id,
                schedule_runs.c.schedule_id == schedule_id,
                schedule_runs.c.slot == slot,
            )
        ).scalar_one_or_none()
        return ScheduledRun.model_validate(stored) if stored is not None else None

    async def add_scheduled_run(self, principal: Principal, project_id: str, entry: ScheduledRun) -> ScheduledRun:
        def work(connection: Connection, space: str) -> tuple[ScheduledRun, bool]:
            if self._one(connection, schedules, space, project_id, id=entry.schedule_id) is None:
                raise StoreError("not_found", "schedule not found")
            if found := self._scheduled_run(connection, space, project_id, entry.schedule_id, entry.slot):
                return found, False
            saved = entry.model_copy(update={"created_at": entry.created_at or _now()})
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(schedule_runs).values(
                            space=space,
                            project_id=project_id,
                            schedule_id=entry.schedule_id,
                            slot=entry.slot,
                            state=entry.state,
                            run_id=entry.run_id,
                            record=_json(saved),
                        )
                    )
            except IntegrityError:  # another turn recorded the slot at the same moment
                found = self._scheduled_run(connection, space, project_id, entry.schedule_id, entry.slot)
                if found is None:
                    raise
                return found, False
            return saved, True

        saved, created = await self._run(principal, work)
        if created:
            self._emit(principal, "schedule", project_id, entry.schedule_id)
        return saved

    async def get_scheduled_run(
        self, principal: Principal, project_id: str, schedule_id: str, slot: datetime
    ) -> ScheduledRun | None:
        def work(connection: Connection, space: str) -> ScheduledRun | None:
            return self._scheduled_run(connection, space, project_id, schedule_id, slot)

        return await self._run(principal, work)

    async def list_scheduled_runs(
        self, principal: Principal, project_id: str, schedule_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> Page[ScheduledRun]:
        offset = int(cursor) if cursor and ascii_digits(cursor) else 0
        _bounds(offset, limit)

        def work(connection: Connection, space: str) -> Page[ScheduledRun]:
            where = [
                schedule_runs.c.space == space,
                schedule_runs.c.project_id == project_id,
                schedule_runs.c.schedule_id == schedule_id,
            ]
            total = connection.execute(select(func.count()).select_from(schedule_runs).where(*where)).scalar_one()
            rows: Sequence[Any] = (
                connection.execute(
                    select(schedule_runs.c.record)
                    .where(*where)
                    .order_by(schedule_runs.c.slot.desc())
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return _page([ScheduledRun.model_validate(r) for r in rows], offset, limit, total)

        return await self._run(principal, work)

    async def set_scheduled_run_gate(
        self, principal: Principal, project_id: str, schedule_id: str, slot: datetime, gate: str
    ) -> ScheduledRun | None:
        def work(connection: Connection, space: str) -> ScheduledRun | None:
            found = self._scheduled_run(connection, space, project_id, schedule_id, slot)
            if found is None:
                return None
            gated = ScheduledRun.model_validate({**found.model_dump(mode="json"), "gate": gate})
            connection.execute(
                update(schedule_runs)
                .where(
                    schedule_runs.c.space == space,
                    schedule_runs.c.project_id == project_id,
                    schedule_runs.c.schedule_id == schedule_id,
                    schedule_runs.c.slot == slot,
                )
                .values(record=_json(gated))
            )
            return gated

        gated = await self._run(principal, work)
        if gated is not None:
            self._emit(principal, "schedule", project_id, schedule_id)
        return gated
