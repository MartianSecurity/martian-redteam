"""Admitting work reads the record store a constant number of times, whatever the number of runs
the organisation has (MartianSecurity/martian-notes#350)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from martian.evals.model import Run, ScoringJob
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import SINGLE_USER, Entitlements
from martian.evals.server.admission import ensure_run_place, ensure_scoring_place
from martian.evals.store import MemoryStore
from tests.evals import test_records
from tests.evals.record_contract import JUDGE, T0

needs_postgres = test_records.needs_postgres
schema, owner_url = test_records.schema, test_records.owner_url  # the record tests' fixtures
LIMIT = 3
EDITION = Entitlements(edition="enterprise", limits={"concurrent_runs": LIMIT, "concurrent_scoring_jobs": LIMIT})


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=needs_postgres)])
def records(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SqlRecordStore]:
    if request.param == "sqlite":
        found = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    else:
        found = SqlRecordStore(request.getfixturevalue("owner_url"), schema=request.getfixturevalue("schema"))
    yield found
    found.close()


def _run(run_id: str, state: str) -> Run:
    return Run.model_validate({"id": run_id, "project_id": "p1", "source": "local", "state": state, "created_at": T0})


async def _statements_of_admission(records: SqlRecordStore, statements: list[str]) -> int:
    """How many statements admitting a run and a scoring job takes, and the refusals it gives."""
    before = len(statements)
    await ensure_run_place(MemoryStore(), records, SINGLE_USER, EDITION)
    await ensure_scoring_place(MemoryStore(), records, SINGLE_USER, EDITION)
    return len(statements) - before


async def test_admission_takes_the_same_statements_for_one_run_as_for_fifty(records: SqlRecordStore) -> None:
    statements: list[str] = []

    @event.listens_for(records._engine, "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    await records.create_run(SINGLE_USER, _run("r0", "completed"))
    await records.create_scoring_job(
        SINGLE_USER, "p1", ScoringJob(id="j0", run_id="r0", scorers=[JUDGE], state="queued")
    )
    with_one = await _statements_of_admission(records, statements)
    for number in range(1, 50):
        await records.create_run(SINGLE_USER, _run(f"r{number}", "completed"))
    with_fifty = await _statements_of_admission(records, statements)
    assert with_one == with_fifty
    assert with_fifty <= 4  # a count each, and on Postgres the space each transaction sets


async def test_the_limits_still_refuse_at_the_limit_and_not_before(records: SqlRecordStore) -> None:
    await records.create_run(SINGLE_USER, _run("done", "completed"))
    for number in range(LIMIT):
        await ensure_run_place(MemoryStore(), records, SINGLE_USER, EDITION)
        await ensure_scoring_place(MemoryStore(), records, SINGLE_USER, EDITION)
        await records.create_run(SINGLE_USER, _run(f"active-{number}", "queued"))
        await records.create_scoring_job(
            SINGLE_USER, "p1", ScoringJob(id=f"job-{number}", run_id="done", scorers=[JUDGE], state="running")
        )
    for ensure in (ensure_run_place, ensure_scoring_place):
        with pytest.raises(HTTPException) as refused:
            await ensure(MemoryStore(), records, SINGLE_USER, EDITION)
        assert refused.value.status_code == 409
