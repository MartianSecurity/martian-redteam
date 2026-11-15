"""The record store passes its contract on SQLite, and on Postgres when MARTIAN_TEST_POSTGRES_URL
names a database the tests may create schemas and roles in. The migrations run both ways on
both databases and create exactly the tables the store uses."""

import inspect
import os
import uuid
from typing import Any

import pytest
from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, create_engine, insert, select, text

from martian.evals.model import Score
from martian.evals.records import RECORD_METHODS, RecordStore
from martian.evals.records.sql import SqlRecordStore, downgrade, engine_for, upgrade
from martian.evals.records.tables import TABLES, metadata, scores
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


# The scores table as migration 0009 left it: a score keyed by run, case, scorer and scoring job.
SCORES_AT_0009 = Table(
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
    """Migration 0010 fills each score's source from its record in every space, so a check's score
    and a person's of the same name are two rows; going back keeps the check's (the review still
    holds the person's answer)."""
    upgrade(engine, "0009")
    with engine.begin() as connection:
        for space in SPACES:
            _in_space(connection, space)
            # A review's answer that replaced the judge's score on c1, and a judge's score on c2.
            connection.execute(
                insert(SCORES_AT_0009), [_score_row(space, "c1", "human"), _score_row(space, "c2", "llm_judge")]
            )
    upgrade(engine)
    _migrations_match_the_tables(engine)
    for space in SPACES:
        assert _sources(engine, space, scores) == [("c1", "human"), ("c2", "llm_judge")]
    with engine.begin() as connection:
        _in_space(connection, "org-a")
        connection.execute(insert(scores), [{**_score_row("org-a", "c1", "llm_judge"), "source": "llm_judge"}])
    assert _sources(engine, "org-a", scores) == [("c1", "human"), ("c1", "llm_judge"), ("c2", "llm_judge")]
    downgrade(engine, "0009")
    assert _sources(engine, "org-a", SCORES_AT_0009) == [("c1", "llm_judge"), ("c2", "llm_judge")]
    assert _sources(engine, "~local", SCORES_AT_0009) == [("c1", "human"), ("c2", "llm_judge")]
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
