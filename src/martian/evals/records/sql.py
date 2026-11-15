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

from martian.evals._sql import check_schema, create_schema, database_refusal, is_postgres, operation_name, postgres_url
from martian.evals.model import (
    Case,
    CaseResult,
    DatasetDraft,
    Page,
    Review,
    Run,
    RunState,
    Score,
    ScoringJob,
    Suite,
)
from martian.evals.records import DatasetManifest, Delivery, SavedView
from martian.evals.records.events import Event, EventBus, InProcessEvents, space_of
from martian.evals.records.tables import (
    case_results,
    dataset_draft_cases,
    dataset_drafts,
    dataset_manifests,
    deliveries,
    reviews,
    runs,
    saved_views,
    scoring_jobs,
    suites,
)
from martian.evals.records.tables import scores as score_rows
from martian.evals.store import StoreError, cursor_offset

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
    ) -> None:
        self._postgres = is_postgres(database_url)
        self._engine = engine_for(database_url, schema=schema)
        if migrate:
            upgrade(self._engine)
        self.events: EventBus = events or InProcessEvents()

    def close(self) -> None:
        self._engine.dispose()

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
    ) -> Page[Run]:
        offset = cursor_offset(cursor)
        _bounds(offset, limit, 500)

        def work(connection: Connection, space: str) -> Page[Run]:
            where = [runs.c.space == space, runs.c.project_id == project_id]
            if states:
                where.append(runs.c.state.in_(states))
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

    async def update_run(self, principal: Principal, run: Run, *, expected_state: RunState | None = None) -> Run:
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
            changed = connection.execute(statement.values(state=run.state, record=_json(saved))).rowcount
            if changed == 0:
                raise StoreError("conflict", "the run is no longer in the expected state")
            return saved

        saved = await self._run(principal, work)
        self._emit(principal, "run", run.project_id, run.id)
        return saved

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
                    "created_at": saved.created_at,
                    "record": _json(saved),
                },
                ["space", "project_id", "run_id", "case_id", "scorer", "scoring_job_id"],
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
            query = query.order_by(score_rows.c.case_id, score_rows.c.scorer, score_rows.c.scoring_job_id)
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
