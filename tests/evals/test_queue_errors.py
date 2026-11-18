"""A queue write the database refuses is reported by the operation and the table, never by a value
(MartianSecurity/martian-notes#354).

A canary in the refused write, in the queued entry's id, its project, its kind, its work and the
principal's organisation, appears in no queue error, no API answer and no log line, on SQLite and
on Postgres when MARTIAN_TEST_POSTGRES_URL names a database the tests may create schemas in.

Two writes are refused: a ``hold`` of an entry that is in the queue already (a duplicate key,
which Postgres reports with the key's values), and a kind longer than its ``varchar(16)``. SQLite
takes any length, so there a trigger refuses the same write.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from martian.evals.records.queue import RECORDING, AlreadyHeld, SqlWorkQueue
from martian.evals.server import _store_error
from martian.evals.server.principal import Principal
from martian.evals.store import StoreError
from tests.evals import test_records
from tests.evals.test_store_errors import Logs

needs_postgres = test_records.needs_postgres
schema, owner_url = test_records.schema, test_records.owner_url  # the record tests' fixtures
CANARY = "CANARY42"
PRINCIPAL = Principal(user_id="u1", organisation_id=f"org-{CANARY}")
PROJECT, ENTRY, WORK = f"p-{CANARY}", f"run:{CANARY}", {"prompt": CANARY}
LONG_KIND = f"{CANARY}-{CANARY}-{CANARY}"
"""More than the 16 characters of the kind column."""
SQLITE_TRIGGER = """CREATE TRIGGER refuse_long_kind BEFORE INSERT ON martian_work_queue
    WHEN length(NEW.kind) > 16 BEGIN SELECT RAISE(ABORT, 'refused'); END"""


class Contract:
    @pytest.fixture
    def logs(self) -> Iterator[Logs]:
        handler = Logs()
        core, root = logging.getLogger("martian"), logging.getLogger()
        level = core.level
        core.setLevel(logging.DEBUG)
        root.addHandler(handler)
        yield handler
        root.removeHandler(handler)
        core.setLevel(level)

    @staticmethod
    def quotes_nothing(error: StoreError, logs: Logs) -> None:
        assert error.__cause__ is None and error.__context__ is None, "the error keeps the driver's"
        assert CANARY not in "".join(traceback.format_exception(error))
        assert CANARY not in logs.text

    async def test_holding_an_entry_in_the_queue_raises_a_specific_error_without_its_values(
        self, queue: SqlWorkQueue, logs: Logs
    ) -> None:
        claim = await queue.hold(PRINCIPAL, PROJECT, ENTRY, RECORDING, WORK, 60)
        with pytest.raises(AlreadyHeld) as refused:
            await queue.hold(PRINCIPAL, PROJECT, ENTRY, RECORDING, WORK, 60)
        assert refused.value.kind == "conflict"
        assert refused.value.detail.startswith("hold: ") and "martian_work_queue" in refused.value.detail
        assert CANARY not in refused.value.detail
        self.quotes_nothing(refused.value, logs)
        assert await queue.held(PRINCIPAL, PROJECT, ENTRY), "the first hold is untouched"
        await queue.done(claim)

    async def test_a_value_the_database_refuses_is_reported_without_it(self, queue: SqlWorkQueue, logs: Logs) -> None:
        with pytest.raises(StoreError) as refused:
            await queue.put(PRINCIPAL, PROJECT, ENTRY, LONG_KIND, WORK)
        assert not isinstance(refused.value, AlreadyHeld) and refused.value.kind == "invalid"
        assert refused.value.detail.startswith("put: ") and "martian_work_queue" in refused.value.detail
        assert CANARY not in refused.value.detail
        self.quotes_nothing(refused.value, logs)
        with pytest.raises(StoreError) as other:
            await queue.hold(PRINCIPAL, PROJECT, ENTRY, LONG_KIND, WORK, 60)
        assert other.value.kind == "invalid" and other.value.detail.startswith("hold: ")
        self.quotes_nothing(other.value, logs)

    async def test_an_answer_that_carries_the_error_holds_no_value(self, queue: SqlWorkQueue, logs: Logs) -> None:
        app = FastAPI()
        app.add_exception_handler(StoreError, _store_error)

        @app.post("/queue")
        async def put() -> None:
            await queue.put(PRINCIPAL, PROJECT, ENTRY, LONG_KIND, WORK)

        @app.post("/hold")
        async def hold() -> None:
            await queue.hold(PRINCIPAL, PROJECT, ENTRY, RECORDING, WORK, 60)

        with TestClient(app) as client:
            assert client.post("/hold").status_code == 200
            for route, status in (("/queue", 422), ("/hold", 409)):
                answer = client.post(route)
                assert answer.status_code == status and CANARY not in answer.text, answer.text
        assert CANARY not in logs.text


class TestQueueErrorsOnSqlite(Contract):
    @pytest.fixture
    def queue(self, tmp_path: Path) -> Iterator[SqlWorkQueue]:
        url = f"sqlite:///{tmp_path / 'martian.db'}"
        found = SqlWorkQueue(url)
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(text(SQLITE_TRIGGER))
        engine.dispose()
        yield found
        found.close()


@needs_postgres
class TestQueueErrorsOnPostgres(Contract):
    @pytest.fixture
    def queue(self, schema: str, owner_url: str) -> Iterator[SqlWorkQueue]:
        found = SqlWorkQueue(owner_url, schema=schema)
        yield found
        found.close()
