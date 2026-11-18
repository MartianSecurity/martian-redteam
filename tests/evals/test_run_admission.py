"""A server may give the record store a ``RunAdmission``: its own rule for a run the launch records.
It refuses the run, or chooses its code, before the store keeps it, and the core names nothing of
why. Without one the store keeps the run as it was given. The record store tests run on SQLite, and
on Postgres when MARTIAN_TEST_POSTGRES_URL is set; the launch route is the core's own."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text

from martian.evals.model import Run
from martian.evals.records import LAUNCHED, RunAdmission
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, ENTERPRISE, SINGLE_USER, organisation_principal
from martian.evals.store import MemoryStore, StoreError
from tests.evals.write_fixture import AGENT, serve

ORG_A = organisation_principal("user-a", "org-a", frozenset({"admin"}))
ORG_B = organisation_principal("user-b", "org-b", frozenset({"admin"}))
POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")


class Numbered:
    """An admission that numbers each organisation's runs from its own counter, ``N-<n>`` from 7,
    and pauses the organisations named in ``paused``."""

    def __init__(self, *, paused: tuple[str, ...] = (), refuse_with: Exception | None = None) -> None:
        self.paused, self.taken, self.asked, self.resumed = set(paused), {}, [], []
        self.refuse_with = refuse_with

    async def admit(self, principal, run):
        self.asked.append((principal.organisation_id, run.id))
        if principal.organisation_id in self.paused:
            raise self.refuse_with or HTTPException(409, "New runs are paused")
        number = self.taken.get(principal.organisation_id, 6) + 1
        self.taken[principal.organisation_id] = number
        return run.model_copy(update={"code": f"N-{number}"})

    async def resume(self, principal, run):
        self.resumed.append((principal.organisation_id, run.id))
        if principal.organisation_id in self.paused:
            raise self.refuse_with or HTTPException(409, "New runs are paused")


def run(source="console", *, code="EV-1001") -> Run:
    return Run(id=str(uuid.uuid4()), project_id=DEFAULT_PROJECT, code=code, source=source, state="queued")


@pytest.fixture(params=["sqlite", "postgres"])
def opened(request, tmp_path):
    """A way to open the record store on each database."""
    stores: list[SqlRecordStore] = []
    schema = f"martian_admission_{uuid.uuid4().hex[:10]}"
    if request.param == "postgres" and not POSTGRES_URL:
        pytest.skip("set MARTIAN_TEST_POSTGRES_URL to run on Postgres")

    def open_store(admission: RunAdmission | None = None) -> SqlRecordStore:
        url = f"sqlite:///{tmp_path / 'martian.db'}" if request.param == "sqlite" else POSTGRES_URL
        assert url is not None
        stores.append(SqlRecordStore(url, schema=schema, admission=admission))
        return stores[-1]

    yield open_store
    for store in stores:
        store.close()
    if request.param == "postgres":
        engine = create_engine(POSTGRES_URL)  # type: ignore[arg-type]
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


async def test_a_store_without_an_admission_keeps_the_run_as_it_was_given(opened):
    records = opened()
    assert records.admission is None
    assert (await records.create_run(ORG_A, run())).code == "EV-1001"


async def test_a_launched_run_takes_the_code_its_admission_chooses_and_a_launch_again_keeps_it(opened):
    admission = Numbered()
    records = opened(admission)
    first, second = run(), run()
    stored = await records.create_run(ORG_A, first)
    assert stored.code == "N-7"
    assert (await records.get_run(ORG_A, DEFAULT_PROJECT, first.id)).code == "N-7"
    # The same launch again is the stored run; it is not asked about and takes no number.
    assert (await records.create_run(ORG_A, first)).code == "N-7"
    assert admission.asked == [("org-a", first.id)]
    assert (await records.create_run(ORG_A, second)).code == "N-8"
    # Another organisation counts on its own: the admission is told whose run it is.
    assert (await records.create_run(ORG_B, run())).code == "N-7"


async def test_a_refusal_keeps_the_run_out_and_is_what_the_store_raises(opened):
    admission = Numbered(paused=("org-a",))
    records = opened(admission)
    refused = run()
    with pytest.raises(HTTPException) as caught:
        await records.create_run(ORG_A, refused)
    assert (caught.value.status_code, caught.value.detail) == (409, "New runs are paused")
    assert await records.get_run(ORG_A, DEFAULT_PROJECT, refused.id) is None
    assert (await records.create_run(ORG_B, refused.model_copy(update={"id": str(uuid.uuid4())}))).code == "N-7"
    # A store error, which the core's own app answers, passes through as it is.
    paused = Numbered(paused=("org-a",), refuse_with=StoreError("conflict", "Paused"))
    with pytest.raises(StoreError, match="Paused"):
        await opened(paused).create_run(ORG_A, run())


async def test_only_a_run_the_launch_records_is_asked_about(opened):
    admission = Numbered(paused=("org-a",))
    records = opened(admission)
    for source in ("ci", "local"):
        stored = await records.create_run(ORG_A, run(source, code="EV-1043"))
        assert (stored.source, stored.code) == (source, "EV-1043")
    assert admission.asked == []
    assert LAUNCHED == "console"


async def test_the_single_local_user_has_an_organisationless_admission_like_any_other(opened):
    records = opened(Numbered())
    assert (await records.create_run(SINGLE_USER, run())).code == "N-7"


def test_the_launch_route_records_the_run_through_the_admission(tmp_path):
    admission = Numbered()
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}", admission=admission)
    try:
        client, served = serve(MemoryStore(), records, entitlements=lambda _p: ENTERPRISE, principal=ORG_A)
        with client:
            launched = served.launch()
            assert (launched["state"], launched["run_code"], launched["agent_id"]) == ("queued", "N-7", AGENT)
            assert served.launch()["run_code"] == "N-8"
            admission.paused.add("org-a")
            response = client.post(
                "/v1/evals", json={"agent_id": AGENT, "dataset_name": "support-faq", "request_id": str(uuid.uuid4())}
            )
            assert (response.status_code, response.json()) == (409, {"detail": "New runs are paused"})
            served.drain()
            runs = served.call("GET", "/v1/evals", 200)
            assert sorted(r["run_code"] for r in runs) == ["N-7", "N-8"]
    finally:
        records.close()


def test_a_launch_sent_again_while_paused_is_the_run_it_already_made(tmp_path):
    admission = Numbered()
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}", admission=admission)
    try:
        client, served = serve(MemoryStore(), records, entitlements=lambda _p: ENTERPRISE, principal=ORG_A)
        with client:
            body = {"request_id": str(uuid.uuid4())}
            first = served.launch(**body)
            admission.paused.add("org-a")
            again = served.launch(**body)
            assert (again["id"], again["run_code"]) == (first["id"], "N-7")
    finally:
        records.close()


def test_a_resume_is_refused_while_paused_as_a_launch_is_and_takes_no_second_code(tmp_path):
    """A stopped run resumes through ``update_run``, which no admission is asked about, so the
    route asks ``RunAdmission.resume``: a paused organisation's resume is refused with what the
    admission raises, and the admission numbers nothing, so the run keeps its code and the next
    launch takes the next number."""
    admission = Numbered()
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}", admission=admission)
    try:
        client, served = serve(MemoryStore(), records, entitlements=lambda _p: ENTERPRISE, principal=ORG_A)
        with client:
            launched = served.launch()
            served.drain()
            assert launched["run_code"] == "N-7"
            stored = client.portal.call(records.get_run, ORG_A, DEFAULT_PROJECT, launched["id"])
            stopped = stored.model_copy(update={"state": "stopped", "error": "Stopped at its time limit"})
            client.portal.call(lambda: records.update_run(ORG_A, stopped, expected_state="completed"))
            admission.paused.add("org-a")
            refused = client.post(f"/v1/evals/{launched['id']}/resume", json={})
            assert (refused.status_code, refused.json()) == (409, {"detail": "New runs are paused"})
            assert served.call("GET", f"/v1/evals/{launched['id']}", 200)["state"] == "stopped"
            admission.paused.discard("org-a")
            resumed = served.call("POST", f"/v1/evals/{launched['id']}/resume", 202, json={})
            assert (resumed["state"], resumed["run_code"]) == ("queued", "N-7")
            served.drain()
            assert served.call("GET", f"/v1/evals/{launched['id']}", 200)["run_code"] == "N-7"
            assert served.launch()["run_code"] == "N-8"
        # The resumes were asked of the admission, the launches only numbered; neither took a code more.
        assert admission.resumed == [("org-a", launched["id"])] * 2
        assert [number for _, number in admission.taken.items()] == [8]
    finally:
        records.close()
