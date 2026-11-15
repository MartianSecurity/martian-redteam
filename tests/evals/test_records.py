"""The record store passes its contract on SQLite, and on Postgres when MARTIAN_TEST_POSTGRES_URL
names a database the tests may create schemas and roles in. The migrations run both ways on
both databases and create exactly the tables the store uses."""

import inspect
import json
import os
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, create_engine, insert, select, text

from martian.evals.model import Score
from martian.evals.records import RECORD_METHODS, RecordStore
from martian.evals.records.queue import SqlWorkQueue
from martian.evals.records.sql import SqlRecordStore, downgrade, engine_for, upgrade
from martian.evals.records.tables import TABLES, metadata, scores
from martian.evals.store import StoreError
from tests.evals.record_contract import ORG_A, T0, RecordContract, run
from tests.evals.record_contract import test_a_listener_that_falls_behind_is_told_to_read_again as _listener_test

POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to run on Postgres")
test_a_listener_that_falls_behind_is_told_to_read_again = _listener_test


@pytest.fixture
def schema():
    name = f"martian_test_{uuid.uuid4().hex[:10]}"
    yield name
    if POSTGRES_URL:
        engine = create_engine(POSTGRES_URL)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        engine.dispose()


class TestRecordsOnSqlite(RecordContract):
    @pytest.fixture
    def records(self, tmp_path):
        store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
        yield store
        store.close()


@pytest.fixture
def owner_url(schema):
    """A URL for an ordinary role that owns the test schema, as an installation would run: the
    tables' owner is held by row-level security, a superuser is not."""
    from sqlalchemy.engine import make_url

    role, password = f"martian_owner_{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    admin = create_engine(POSTGRES_URL)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        connection.execute(text(f'CREATE SCHEMA "{schema}" AUTHORIZATION {role}'))
    yield make_url(POSTGRES_URL).set(username=role, password=password).render_as_string(hide_password=False)
    with admin.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        connection.execute(text(f"DROP ROLE {role}"))
    admin.dispose()


@needs_postgres
class TestRecordsOnPostgres(RecordContract):
    @pytest.fixture
    def records(self, schema, owner_url):
        store = SqlRecordStore(owner_url, schema=schema)
        yield store
        store.close()


def test_the_sql_store_implements_every_method():
    for name in RECORD_METHODS:
        implementation = getattr(SqlRecordStore, name)
        assert inspect.iscoroutinefunction(implementation), name
        assert list(inspect.signature(implementation).parameters) == list(
            inspect.signature(getattr(RecordStore, name)).parameters
        ), name


def _migrations_match_the_tables(engine) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"version_table": "martian_alembic_version"})
        assert compare_metadata(context, metadata) == []


def _round_trip(engine) -> None:
    upgrade(engine)
    _migrations_match_the_tables(engine)
    downgrade(engine)
    with engine.connect() as connection:
        names = set(connection.dialect.get_table_names(connection))
    assert not names & {table.name for table in TABLES}
    upgrade(engine)
    _migrations_match_the_tables(engine)


def test_migrations_run_both_ways_on_sqlite(tmp_path):
    engine = engine_for(f"sqlite:///{tmp_path / 'martian.db'}")
    try:
        _round_trip(engine)
    finally:
        engine.dispose()


@needs_postgres
def test_migrations_run_both_ways_on_postgres_in_their_own_schema(schema):
    engine = engine_for(POSTGRES_URL, schema=schema)
    try:
        _round_trip(engine)
        with engine.connect() as connection:
            placed = (
                connection.execute(
                    text("SELECT table_schema FROM information_schema.tables WHERE table_name = 'martian_runs'")
                )
                .scalars()
                .all()
            )
        # Tests running alongside, in this run or another on the same database, have their own.
        assert schema in placed
        assert [name for name in placed if not name.startswith("martian_test_")] == []
    finally:
        engine.dispose()


# The scores table as migration 0011 leaves it: a score keyed by run, case, scorer and scoring job.
SCORES_AT_0011 = Table(
    "martian_scores",
    MetaData(),
    *(Column(name, String(200)) for name in ("space", "project_id", "run_id", "case_id", "scorer", "scoring_job_id")),
    Column("created_at", DateTime(timezone=True)),
    Column("record", JSON),
)
SPACES = ("org-a", "~local")


def _in_space(connection, space: str) -> None:
    # On Postgres row-level security shows a transaction only its space's rows, the owner's too.
    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT set_config('martian.space', :space, true)"), {"space": space})


def _score_row(space: str, case_id: str, source: str) -> dict[str, Any]:
    score = Score(run_id="r1", case_id=case_id, scorer="politeness", source=source, value=source != "human")
    return {
        "space": space,
        "project_id": "p1",
        "run_id": "r1",
        "case_id": case_id,
        "scorer": "politeness",
        "scoring_job_id": "",
        "created_at": T0,
        "record": score.model_dump(mode="json"),
    }


def _sources(engine, space: str, table: Table) -> list[tuple[str, str]]:
    """Each score of the space as (case, source), the source read from the score's record and, once
    the table has one, checked against its column."""
    with engine.begin() as connection:
        _in_space(connection, space)
        rows = connection.execute(select(table).where(table.c.space == space)).mappings().all()
    for row in rows:
        assert row.get("source", row["record"]["source"]) == row["record"]["source"]
    return sorted((row["case_id"], row["record"]["source"]) for row in rows)


def _sources_join_the_score_key(engine) -> None:
    """Migration 0012 fills each score's source from its record in every space, so a check's score
    and a person's of the same name are two rows; going back keeps the check's (the review still
    holds the person's answer)."""
    upgrade(engine, "0011")
    with engine.begin() as connection:
        for space in SPACES:
            _in_space(connection, space)
            # A review's answer that replaced the judge's score on c1, and a judge's score on c2.
            connection.execute(
                insert(SCORES_AT_0011), [_score_row(space, "c1", "human"), _score_row(space, "c2", "llm_judge")]
            )
    upgrade(engine)
    _migrations_match_the_tables(engine)
    for space in SPACES:
        assert _sources(engine, space, scores) == [("c1", "human"), ("c2", "llm_judge")]
    with engine.begin() as connection:
        _in_space(connection, "org-a")
        connection.execute(insert(scores), [{**_score_row("org-a", "c1", "llm_judge"), "source": "llm_judge"}])
    assert _sources(engine, "org-a", scores) == [("c1", "human"), ("c1", "llm_judge"), ("c2", "llm_judge")]
    downgrade(engine, "0011")
    assert _sources(engine, "org-a", SCORES_AT_0011) == [("c1", "llm_judge"), ("c2", "llm_judge")]
    assert _sources(engine, "~local", SCORES_AT_0011) == [("c1", "human"), ("c2", "llm_judge")]
    upgrade(engine)
    _migrations_match_the_tables(engine)


def test_a_score_keeps_its_source_through_the_migrations_on_sqlite(tmp_path):
    engine = engine_for(f"sqlite:///{tmp_path / 'martian.db'}")
    try:
        _sources_join_the_score_key(engine)
    finally:
        engine.dispose()


@needs_postgres
def test_a_score_keeps_its_source_through_the_migrations_on_postgres_as_the_tables_owner(schema, owner_url):
    engine = engine_for(owner_url, schema=schema)
    try:
        _sources_join_the_score_key(engine)
    finally:
        engine.dispose()


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=needs_postgres)])
def database(request, tmp_path, schema) -> tuple[str, str]:
    """The URL and schema of a fresh record store database: SQLite, or Postgres as the tables'
    owner, held by row-level security."""
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'martian.db'}", schema
    return request.getfixturevalue("owner_url"), schema


GONE, KEPT = "run-gone-5c1e", "run-kept-8d2f"
KEYED_TO_A_RUN = {
    "martian_runs",
    "martian_case_results",
    "martian_scores",
    "martian_scoring_jobs",
    "martian_reviews",
    "martian_work_queue",
    "martian_deliveries",
    "martian_schedule_runs",
}


async def _everything_of_a_run(records: SqlRecordStore, queue: SqlWorkQueue, run_id: str, day: int) -> None:
    """A finished run with a row in every table keyed to a run: results, its own scores, a
    cancelled scoring job's scores, a person's answer, a review chain, the answer's delivery, the
    schedule slot that started it, and work for it a worker has not yet removed from the queue."""
    from martian.evals.model import CaseResult, Review, ScoringJob
    from martian.evals.outbox import score_delivery
    from martian.evals.records import ScheduledRun
    from martian.evals.worker import Work
    from martian.evals.worker.process import dump_work
    from martian.evals.worker.queue import entry_id
    from tests.evals.record_contract import JUDGE

    await records.create_run(ORG_A, run(run_id, minutes=day, state="completed"))
    own = Score(run_id=run_id, case_id="c1", scorer="exact_match", source="code", value=True, passed=True)
    await records.put_results(ORG_A, "p1", [CaseResult(run_id=run_id, case_id="c1", status="passed")], scores=[own])
    job = ScoringJob(id=f"{run_id}-job", run_id=run_id, scorers=[JUDGE], state="cancelled")
    await records.create_scoring_job(ORG_A, "p1", job)
    answer = own.model_copy(update={"scorer": "politeness", "source": "human", "trace_id": f"tr-{run_id}"})
    await records.put_scores(ORG_A, "p1", [own.model_copy(update={"scoring_job_id": job.id}), answer])
    first = await records.add_review(
        ORG_A, "p1", Review(id=f"{run_id}-v1", run_id=run_id, case_id="c1", state="pending")
    )
    await records.add_review(ORG_A, "p1", first.model_copy(update={"id": f"{run_id}-v2", "previous_id": first.id}))
    await records.put_delivery(ORG_A, "p1", score_delivery("p1", answer))
    slot = ScheduledRun(schedule_id="sc1", slot=T0 + timedelta(days=day), state="admitted", run_id=run_id)
    await records.add_scheduled_run(ORG_A, "p1", slot)
    for work in (
        Work(kind="run", principal=ORG_A, project_id="p1", run_id=run_id),
        Work(kind="scoring", principal=ORG_A, project_id="p1", run_id=run_id, job_id=job.id),
    ):
        await queue.put(ORG_A, "p1", entry_id(work), work.kind, dump_work(work))


def _rows_naming(engine, space: str, text_: str) -> dict[str, int]:
    """How many rows of the space each table holds with ``text_`` in any column, its JSON included."""
    found = {}
    with engine.begin() as connection:
        _in_space(connection, space)
        for table in TABLES:
            rows = connection.execute(select(table).where(table.c.space == space)).mappings().all()
            found[table.name] = sum(text_ in json.dumps(dict(row), default=str) for row in rows)
    return found


async def test_a_deleted_run_leaves_no_row_naming_it_in_any_table(database):
    from tests.evals.record_contract import schedule

    url, schema = database
    records = SqlRecordStore(url, schema=schema)
    queue = SqlWorkQueue(url, schema=schema, migrate=False)
    engine = engine_for(url, schema=schema)
    try:
        await records.create_schedule(ORG_A, "p1", schedule("sc1"))
        for day, run_id in enumerate((GONE, KEPT)):
            await _everything_of_a_run(records, queue, run_id, day)
        before, kept = _rows_naming(engine, "org-a", GONE), _rows_naming(engine, "org-a", KEPT)
        assert {name for name, count in before.items() if count} == KEYED_TO_A_RUN

        removed = await records.delete_run(ORG_A, "p1", GONE)
        assert removed == {name: count for name, count in before.items() if count}
        assert _rows_naming(engine, "org-a", GONE) == dict.fromkeys(before, 0)
        assert _rows_naming(engine, "org-a", KEPT) == kept
    finally:
        records.close()
        queue.close()
        engine.dispose()


async def test_a_run_a_worker_holds_is_not_deleted_until_its_lease_runs_out(database):
    from martian.evals.worker import Work
    from martian.evals.worker.process import dump_work
    from martian.evals.worker.queue import entry_id

    url, schema = database
    records = SqlRecordStore(url, schema=schema)
    queue = SqlWorkQueue(url, schema=schema, migrate=False)
    try:
        # The run has ended, and its worker is still finishing its turn, as it does for a run a
        # schedule started before it removes the work from the queue.
        await records.create_run(ORG_A, run("r1", state="completed"))
        work = Work(kind="run", principal=ORG_A, project_id="p1", run_id="r1")
        await queue.put(ORG_A, "p1", entry_id(work), work.kind, dump_work(work))
        claim = await queue.claim(lease_seconds=60)
        assert claim is not None and claim.id == entry_id(work)
        with pytest.raises(StoreError) as caught:
            await records.delete_run(ORG_A, "p1", "r1")
        assert caught.value.kind == "conflict"
        assert await records.get_run(ORG_A, "p1", "r1") is not None
        # A claim whose lease ran out holds nothing: its worker is gone.
        await queue.release(claim)
        assert (await queue.claim(lease_seconds=-1)) is not None
        removed = await records.delete_run(ORG_A, "p1", "r1")
        assert (removed["martian_runs"], removed["martian_work_queue"]) == (1, 1)
        assert not await queue.contains(ORG_A, "p1", entry_id(work))
    finally:
        records.close()
        queue.close()


@needs_postgres
async def test_row_level_security_hides_other_organisations_even_from_unfiltered_sql(schema):
    """The second guard: a query that forgets the space filter still sees only its own space."""
    store = SqlRecordStore(POSTGRES_URL, schema=schema)
    await store.create_run(ORG_A, run("r1"))
    store.close()
    role = f"martian_app_{uuid.uuid4().hex[:8]}"
    admin = create_engine(POSTGRES_URL)
    password = uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {role}'))
        connection.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO {role}'))
    from sqlalchemy.engine import make_url

    url = make_url(POSTGRES_URL).set(username=role, password=password)
    app = create_engine(url.render_as_string(hide_password=False), connect_args={"options": f"-csearch_path={schema}"})
    try:
        with app.begin() as connection:
            assert connection.execute(text("SELECT count(*) FROM martian_runs")).scalar_one() == 0
            connection.execute(text("SELECT set_config('martian.space', 'org-b', true)"))
            assert connection.execute(text("SELECT count(*) FROM martian_runs")).scalar_one() == 0
        with app.begin() as connection:
            connection.execute(text("SELECT set_config('martian.space', 'org-a', true)"))
            assert connection.execute(text("SELECT count(*) FROM martian_runs")).scalar_one() == 1
    finally:
        app.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema}" FROM {role}'))
            connection.execute(text(f'REVOKE USAGE ON SCHEMA "{schema}" FROM {role}'))
            connection.execute(text(f"DROP ROLE {role}"))
        admin.dispose()
