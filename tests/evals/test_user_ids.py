"""Every route names a person by the id the bootstrap gives, and the routes that take a user accept
it (MorningHQ/martian-notes#335). The local user is ``local``, which the contract's user fields, UUIDs,
refuse; the API names it by a UUID derived from it. A user whose id is a UUID is named by it."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from martian.evals.local import create_local_app, open_stores
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import ENTERPRISE, SINGLE_USER, organisation_principal, result_id
from martian.evals.store import MemoryStore
from tests.evals.read_fixture import seed
from tests.evals.write_fixture import request_id, serve

LOCAL = "http://127.0.0.1:7600"
LOCAL_USER = "bf0808be-f788-5dd7-8fc2-2d28ed350045"
"""The local user ``local`` as the API names it."""
HOSTED = organisation_principal(str(uuid.UUID(int=42)), "org-a", frozenset({"member"}))
"""A member of an organisation, whose id is a UUID as the server's are."""


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)


@pytest.fixture
def ui(tmp_path) -> Path:
    folder = tmp_path / "dist-evals"
    folder.mkdir()
    (folder / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return folder


@pytest.fixture
def records(tmp_path):
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


def call(client: TestClient, method: str, path: str, status: int, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def test_a_review_saved_from_the_local_ui_with_the_default_reviewer_is_accepted(tmp_path, ui):
    home = tmp_path / "home"
    store, records = open_stores(home)
    try:
        run = asyncio.run(seed(store, records, SINGLE_USER)).candidate.id
    finally:
        records.close()
    with TestClient(create_local_app(home, ui), base_url=LOCAL) as client:
        # The review dialog's reviewer starts as the session's user, and lists the workbench's members.
        me = call(client, "POST", "/v1/auth/refresh", 200)["session"]["user"]["id"]

        item = str(result_id(run, "faq-2"))
        # The body the dialog sends when only the rating and the explanation are changed.
        body = {
            "request_id": request_id(),
            "previous_id": None,
            "assignee_id": me,
            "state": "complete",
            "value": True,
            "comment": "Right answer.",
        }
        saved = call(client, "POST", f"/v1/evals/{run}/items/{item}/reviews", 201, json=body)
        assert (saved["actor_id"], saved["assignee_id"], saved["value"]) == (me, me, True)
        assert me == LOCAL_USER
        assert call(client, "GET", "/v1/evals/bootstrap", 200)["principal"]["user"]["id"] == me
        assert call(client, "POST", f"/v1/evals/{run}/items/{item}/reviews", 201, json=body) == saved
        mine = call(client, "GET", "/v1/evals/reviews", 200, params={"filter": "mine"})["items"]
        assert [r["id"] for r in mine] == [saved["id"]]
        work = call(client, "GET", f"/v1/evals/{run}/workbench", 200, params={"item_id": item})
        assert [m["id"] for m in work["members"]] == [me]
        assert [r["actor_id"] for r in work["reviews"]] == [me]
        assert [(a["name"], a["source_id"]) for a in work["assessments"] if a["source"] == "HUMAN"] == [
            ("human_review", me)
        ]


@pytest.mark.parametrize("principal", [SINGLE_USER, HOSTED], ids=["local", "hosted"])
def test_every_route_names_the_principal_by_the_id_the_bootstrap_gives(records, principal):
    client, core = serve(MemoryStore(), records, entitlements=lambda _p: ENTERPRISE, principal=principal)
    with client:
        me = core.call("GET", "/v1/evals/bootstrap", 200)["principal"]["user"]["id"]
        assert me == (LOCAL_USER if principal is SINGLE_USER else principal.user_id)
        run = core.completed_run()
        cases = {item["dataset_item_id"]: item for item in run["items"]}
        reviews = f"/v1/evals/{run['id']}/items/{{}}/reviews"

        # A review queue assigned to the principal, and its item answered.
        queue = core.call(
            "POST",
            "/v1/evals/review-queues",
            201,
            json={
                "request_id": request_id(),
                "run_id": run["id"],
                "name": "Mine",
                "item_ids": [cases["faq-1"]["id"]],
                "question": {"name": "helpful", "kind": "boolean", "instruction": "Helpful?"},
                "assignee_id": me,
            },
        )
        assert queue["assignee_id"] == me
        (head,) = core.call("GET", "/v1/evals/reviews", 200, params={"queue_id": queue["id"]})["items"]
        assert (head["actor_id"], head["assignee_id"]) == (me, me)
        answer = {"queue_id": queue["id"], "previous_id": head["id"], "assignee_id": me, "comment": "Yes"}
        answered = core.call(
            "POST",
            reviews.format(cases["faq-1"]["id"]),
            201,
            json={"request_id": request_id(), "state": "complete", "value": True, **answer},
        )
        # A review of another result, assigned to the principal.
        review = core.call(
            "POST",
            reviews.format(cases["faq-2"]["id"]),
            201,
            json={"request_id": request_id(), "assignee_id": me, "state": "complete", "value": False, "comment": "No"},
        )
        for saved in (answered, review):
            assert (saved["actor_id"], saved["assignee_id"]) == (me, me)
        mine = core.call("GET", "/v1/evals/reviews", 200, params={"filter": "mine"})["items"]
        assert {r["id"] for r in mine} == {answered["id"], review["id"]}

        work = core.call("GET", f"/v1/evals/{run['id']}/workbench", 200)
        assert [m["id"] for m in work["members"]] == [me]
        assert {r["actor_id"] for r in work["reviews"]} == {me}
        assert {a["source_id"] for a in work["assessments"] if a["source"] == "HUMAN"} == {me}

        trace_id = cases["faq-1"]["evals_trace_id"]
        note = {"request_id": request_id(), "kind": "feedback", "name": "on_topic", "value": True}
        annotation = core.call("POST", f"/v1/evals/traces/{trace_id}/annotations", 201, json=note)
        assert annotation["source"] == {"source_type": "HUMAN", "source_id": me}
        assert core.call("POST", f"/v1/evals/traces/{trace_id}/annotations", 201, json=note) == annotation

        view = {"request_id": request_id(), "name": "Slow", "query": {"order": "slowest"}}
        assert core.call("POST", "/v1/evals/views", 201, json=view)["owner_id"] == me
        suite = {"request_id": request_id(), "name": "Checks", "scorers": ["exact_match"]}
        assert core.call("POST", "/v1/evals/suites", 201, json=suite)["actor_id"] == me
        draft = core.call("POST", "/v1/evals/dataset-drafts", 201, json={"name": "Refunds"})
        assert (draft["created_by"], draft["updated_by"]) == (me, me)
