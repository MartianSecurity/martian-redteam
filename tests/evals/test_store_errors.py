"""A write the database refuses is reported by the operation and the table, never by a value.

A canary in the refused write appears in no store or record store error, no API response and no
log line, MLflow's own included, on SQLite and on Postgres when MARTIAN_TEST_POSTGRES_URL names a
database the tests may create schemas in.

Postgres refuses two of these writes itself: a trace metadata value over 8,000 characters
(MLflow's ``varchar(8000)``) and a case position beyond its ``integer`` column. SQLite takes any
length and any integer, so there a trigger refuses the same writes. The trace's trigger fails with
an error of SQLite's own, the kind MLflow logs itself, as it logs "database is locked"; the case's
aborts, as a constraint does. On both, a trigger refuses a draft case holding the canary, and
another fails a score holding it with an error that is no refusal, as a lost connection is not.
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from martian.evals._sql import postgres_url
from martian.evals.model import Case, CaseResult, Project, Run, Score, Trace
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER
from martian.evals.store import StoreError
from martian.evals.store.mlflow_local import MlflowStore
from tests.evals.write_fixture import serve

POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")
CANARY = "CANARY42"
"""Short, so that any part of a refused value an error could quote holds it whole."""
LONG = " ".join([CANARY] * 1000)
"""A request of 8,999 characters: more than MLflow's trace metadata holds on Postgres."""
OUT_OF_RANGE = 2**31
"""A case position beyond Postgres's ``integer``."""
DATABASES = [
    "sqlite",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to run on Postgres"),
    ),
]

SQLITE_MLFLOW_TRIGGERS = [
    """CREATE TRIGGER refuse_long_metadata BEFORE INSERT ON trace_request_metadata
    WHEN length(NEW.value) > 8000 BEGIN SELECT json('refused: longer than 8000 characters'); END""",
]
SQLITE_RECORD_TRIGGERS = [
    """CREATE TRIGGER refuse_large_position BEFORE INSERT ON martian_case_results
    WHEN NEW.position > 2147483647 BEGIN SELECT RAISE(ABORT, 'integer out of range'); END""",
    f"""CREATE TRIGGER refuse_canary_case BEFORE INSERT ON martian_dataset_draft_cases
    WHEN instr(NEW.record, '{CANARY}') > 0 BEGIN SELECT RAISE(ABORT, 'refused'); END""",
    f"""CREATE TRIGGER fail_canary_score BEFORE INSERT ON martian_scores
    WHEN instr(NEW.record, '{CANARY}') > 0 BEGIN SELECT json('failed'); END""",
]
POSTGRES_RECORD_TRIGGERS = [
    f"""CREATE FUNCTION refuse_canary_case() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF position('{CANARY}' in NEW.record::text) > 0 THEN
            RAISE EXCEPTION 'refused' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER refuse_canary_case BEFORE INSERT ON martian_dataset_draft_cases
    FOR EACH ROW EXECUTE FUNCTION refuse_canary_case()""",
    f"""CREATE FUNCTION fail_canary_score() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF position('{CANARY}' in NEW.record::text) > 0 THEN
            RAISE EXCEPTION 'failed';
        END IF;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER fail_canary_score BEFORE INSERT ON martian_scores
    FOR EACH ROW EXECUTE FUNCTION fail_canary_score()""",
]


def _execute(url: str, statements: list[str]) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


@dataclass
class Databases:
    kind: str
    store: MlflowStore
    records: SqlRecordStore


@pytest.fixture(params=DATABASES)
def databases(request, tmp_path, monkeypatch) -> Iterator[Databases]:
    """The store and the record store, each with its own database, which refuse the writes above."""
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)
    # MLflow keeps each database URL's engine for the life of the process; unpooled, a test's
    # connections close as it finishes.
    monkeypatch.setenv("MLFLOW_SQLALCHEMYSTORE_POOLCLASS", "NullPool")
    if request.param == "sqlite":
        mlflow_url, records_url = f"sqlite:///{tmp_path / 'mlflow.db'}", f"sqlite:///{tmp_path / 'martian.db'}"
        found = Databases("sqlite", MlflowStore(mlflow_url, tmp_path / "artifacts"), SqlRecordStore(records_url))
        _execute(mlflow_url, SQLITE_MLFLOW_TRIGGERS)
        _execute(records_url, SQLITE_RECORD_TRIGGERS)
        yield found
        found.records.close()
        return
    assert POSTGRES_URL is not None
    suffix = uuid.uuid4().hex[:10]
    schemas = (f"mlflow_test_{suffix}", f"martian_test_{suffix}")
    try:
        found = Databases(
            "postgres",
            MlflowStore(POSTGRES_URL, tmp_path / "artifacts", schema=schemas[0]),
            SqlRecordStore(POSTGRES_URL, schema=schemas[1]),
        )
        _execute(postgres_url(POSTGRES_URL, schemas[1]), POSTGRES_RECORD_TRIGGERS)
        yield found
        found.records.close()
    finally:
        _execute(POSTGRES_URL, [f'DROP SCHEMA IF EXISTS "{schema}" CASCADE' for schema in schemas])


class Logs(logging.Handler):
    """Every line the core and MLflow log, each with its traceback."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def logs() -> Iterator[Logs]:
    """The core's loggers at every level; MLflow's, which do not propagate, at their own. The root
    logger keeps its level, so SQLAlchemy does not start logging every statement."""
    handler = Logs()
    core, mlflow, root = logging.getLogger("martian"), logging.getLogger("mlflow"), logging.getLogger()
    level = core.level
    core.setLevel(logging.DEBUG)
    for logger in (root, mlflow):
        logger.addHandler(handler)
    yield handler
    for logger in (root, mlflow):
        logger.removeHandler(handler)
    core.setLevel(level)


def assert_names(detail: str, operation: str, table: str) -> None:
    assert detail.startswith(f"{operation}: ") and table in detail, detail


def assert_quotes_nothing(error: StoreError, logs: Logs) -> None:
    assert error.__cause__ is None and error.__context__ is None, "the error keeps the driver's, which holds the values"
    assert CANARY not in "".join(traceback.format_exception(error))
    assert CANARY not in logs.text


async def test_a_trace_the_database_refuses_is_reported_without_its_text(databases: Databases, logs: Logs) -> None:
    store = databases.store
    await store.create_project(SINGLE_USER, Project(id="p1", name="Agent"))
    with pytest.raises(StoreError) as refused:
        await store.record_trace(SINGLE_USER, Trace(id="t1", project_id="p1", name="chat", request=LONG, response=LONG))
    assert refused.value.kind == "invalid"
    assert_names(refused.value.detail, "record_trace", "trace_request_metadata")
    assert_quotes_nothing(refused.value, logs)
    if databases.kind == "sqlite":
        assert "SQLAlchemy database error" in logs.text, "MLflow logged the failure itself"
    assert await store.get_trace(SINGLE_USER, "p1", "t1") is None


async def test_mlflows_own_refusals_are_reported_without_the_values(databases: Databases, logs: Logs) -> None:
    """MLflow quotes a tag key it refuses; its length refusal names only the field and the limit."""
    store = databases.store
    await store.create_project(SINGLE_USER, Project(id="p1", name="Agent"))
    await store.record_trace(SINGLE_USER, Trace(id="t1", project_id="p1", name="chat", request="hello"))
    with pytest.raises(StoreError) as refused:
        await store.set_trace_tag(SINGLE_USER, "p1", "t1", f"{CANARY}!", "yes")
    assert (refused.value.kind, refused.value.detail) == (
        "invalid",
        "set_trace_tag: MLflow refused the request (INVALID_PARAMETER_VALUE)",
    )
    assert_quotes_nothing(refused.value, logs)

    with pytest.raises(StoreError) as long:
        await store.create_project(SINGLE_USER, Project(id="p2", name="Agent", description=LONG * 3))
    assert (long.value.kind, long.value.detail) == (
        "invalid",
        "create_project: 'value' exceeds the maximum length of 20000 characters",
    )
    assert_quotes_nothing(long.value, logs)


async def test_a_case_result_the_database_refuses_is_reported_without_its_values(
    databases: Databases, logs: Logs
) -> None:
    records = databases.records
    await records.create_run(SINGLE_USER, Run(id="r1", project_id="p1", source="local", state="running"))
    result = CaseResult(
        run_id="r1",
        case_id=f"{CANARY}-case",
        position=OUT_OF_RANGE,
        status="failed",
        inputs={"message": CANARY},
        output=CANARY,
    )
    with pytest.raises(StoreError) as refused:
        await records.put_results(SINGLE_USER, "p1", [result])
    assert refused.value.kind == "invalid"
    assert_names(refused.value.detail, "put_results", "martian_case_results")
    assert_quotes_nothing(refused.value, logs)
    assert (await records.list_results(SINGLE_USER, "p1", "r1")).items == []


async def test_a_record_two_writers_create_at_once_is_a_conflict_without_its_values(
    databases: Databases, logs: Logs, monkeypatch
) -> None:
    """Both writers read that the run does not exist yet, so the second one's insert is refused.
    On Postgres the database's own message repeats the duplicate key's values."""
    records = databases.records
    run = Run(id=f"{CANARY}-run", project_id="p1", code=CANARY, source="local", state="queued")
    await records.create_run(SINGLE_USER, run)
    monkeypatch.setattr(SqlRecordStore, "_one", staticmethod(lambda *_args, **_keys: None))
    with pytest.raises(StoreError) as refused:
        await records.create_run(SINGLE_USER, run)
    assert refused.value.kind == "conflict"
    assert_names(refused.value.detail, "create_run", "martian_runs")
    assert_quotes_nothing(refused.value, logs)


async def test_a_database_failure_that_is_no_refusal_is_raised_without_the_values(databases: Databases) -> None:
    records = databases.records
    await records.create_run(SINGLE_USER, Run(id="r1", project_id="p1", source="local", state="running"))
    score = Score(run_id="r1", case_id=f"{CANARY}-case", scorer="helpful", source="llm_judge", rationale=CANARY)
    with pytest.raises(DBAPIError) as failed:
        await records.put_scores(SINGLE_USER, "p1", [score])
    assert "martian_scores" in str(failed.value)
    assert CANARY not in "".join(traceback.format_exception(failed.value))


def test_a_draft_case_the_database_refuses_is_answered_without_its_values(databases: Databases, logs: Logs) -> None:
    client, served = serve(databases.store, databases.records)
    with client:
        draft = served.call("POST", "/v1/evals/dataset-drafts", 201, json={"name": "Refunds"})
        record = {"id": f"{CANARY}-case", "input": CANARY, "expected_output": CANARY}
        response = client.post(
            f"/v1/evals/dataset-drafts/{draft['id']}/records", json={"base_revision": 1, "record": record}
        )
        cases = served.call("GET", f"/v1/evals/dataset-drafts/{draft['id']}/records", 200)
    assert response.status_code == 422, response.text
    assert_names(response.json()["detail"], "put_draft_cases", "martian_dataset_draft_cases")
    assert CANARY not in response.text
    assert CANARY not in logs.text
    assert cases["items"] == []


def test_a_run_whose_trace_is_refused_fails_without_quoting_it(databases: Databases, logs: Logs) -> None:
    client, served = serve(databases.store, databases.records)
    case = Case(id="long", inputs={"message": LONG}, expectations={"expected_response": "Yes."})
    asyncio.run(databases.store.create_dataset(SINGLE_USER, DEFAULT_PROJECT, "long-prompts", [case]))
    with client:
        launched = served.launch(dataset_name="long-prompts")
        served.drain()
        response = client.get(f"/v1/evals/{launched['id']}")
    run = response.json()
    assert run["state"] == "failed", run
    assert_names(run["error"], "record_trace", "trace_request_metadata")
    assert CANARY not in response.text
    assert "Eval run" in logs.text, "the worker logged the failure, with its traceback"
    assert CANARY not in logs.text
