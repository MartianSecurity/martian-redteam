"""The evals core runs on its own, with the principal, store and entitlements an edition injects."""

from typing import Annotated

import pytest
from fastapi import Depends, Header, HTTPException
from fastapi.testclient import TestClient

from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import (
    COMMUNITY,
    SINGLE_USER,
    Entitlements,
    Principal,
    create_app,
    current_entitlements,
    current_principal,
    organisation_principal,
)
from martian.evals.store import EvalStore, MemoryStore, StoreStatus

# These tests read no records; an in-memory record store satisfies create_app.
RECORDS = SqlRecordStore("sqlite://")


def test_the_local_edition_serves_status_for_its_single_user():
    store = MemoryStore()
    client = TestClient(create_app(records=RECORDS, store=store, principal=lambda: SINGLE_USER))
    response = client.get("/v1/evals/status")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "ready": True,
        "error_code": None,
        "updated_at": None,
        "provider": None,
        "admission_paused": False,
        "routing_epoch": 0,
    }
    assert store.callers == [SINGLE_USER]
    assert SINGLE_USER.kind == "single_user" and SINGLE_USER.organisation_id is None


def test_a_server_injects_an_organisation_principal_from_its_own_session():
    sessions = {"session-a": organisation_principal("user-1", "org-a", frozenset({"member"}))}

    def from_session(x_session: Annotated[str | None, Header()] = None) -> Principal:
        if x_session not in sessions:
            raise HTTPException(401, "Sign in")
        return sessions[x_session]

    store = MemoryStore(StoreStatus(status="provisioning", ready=False, provider="mlflow"))
    client = TestClient(create_app(records=RECORDS, store=store, principal=from_session))
    assert client.get("/v1/evals/status").status_code == 401
    response = client.get("/v1/evals/status", headers={"x-session": "session-a"})
    assert response.json()["status"] == "provisioning" and response.json()["provider"] == "mlflow"
    (caller,) = store.callers
    assert (caller.kind, caller.organisation_id, caller.roles) == ("organisation", "org-a", frozenset({"member"}))


def test_entitlements_come_from_the_edition_for_each_principal():
    enterprise = Entitlements(edition="enterprise", features=frozenset({"review_queues"}))
    app = create_app(
        records=RECORDS,
        store=MemoryStore(),
        principal=lambda: organisation_principal("user-1", "org-a", frozenset({"admin"})),
        entitlements=lambda principal: enterprise if principal.organisation_id == "org-a" else COMMUNITY,
    )

    @app.get("/probe")
    def probe(
        principal: Annotated[Principal, Depends(current_principal)],
        entitlements: Annotated[Entitlements, Depends(current_entitlements)],
    ) -> dict[str, object]:
        return {"edition": entitlements.edition, "queues": entitlements.allows("review_queues")}

    assert TestClient(app).get("/probe").json() == {"edition": "enterprise", "queues": True}
    assert COMMUNITY.allows("review_queues") is False


def test_the_default_entitlements_are_community():
    app = create_app(records=RECORDS, store=MemoryStore(), principal=lambda: SINGLE_USER)

    @app.get("/probe")
    def probe(entitlements: Annotated[Entitlements, Depends(current_entitlements)]) -> str:
        return entitlements.edition

    assert TestClient(app).get("/probe").json() == "community"


def test_a_core_without_a_principal_provider_refuses_to_answer():
    from fastapi import FastAPI

    from martian.evals.server import router

    app = FastAPI()
    app.state.martian_evals_store = MemoryStore()
    app.include_router(router)
    with pytest.raises(RuntimeError, match="principal provider"):
        TestClient(app).get("/v1/evals/status")


def test_a_store_must_implement_the_interface():
    class NotAStore:
        pass

    with pytest.raises(TypeError, match="EvalStore"):
        create_app(records=RECORDS, store=NotAStore(), principal=lambda: SINGLE_USER)  # type: ignore[arg-type]
    assert isinstance(MemoryStore(), EvalStore)


def test_principals_are_well_formed():
    with pytest.raises(ValueError, match="user id"):
        Principal(user_id="")
    with pytest.raises(ValueError, match="organisation id"):
        organisation_principal("user-1", "", frozenset())
    with pytest.raises(ValueError, match="None for the single local user"):
        Principal(user_id="u", organisation_id="")


def test_the_status_route_is_in_the_shape_of_the_frozen_contract():
    # The server's EvalsStatusOut, from contracts/evals-api.json in MartianSecurity/MartianBackend.
    contract_fields = {"status", "ready", "error_code", "updated_at", "provider", "admission_paused", "routing_epoch"}
    schema = create_app(records=RECORDS, store=MemoryStore(), principal=lambda: SINGLE_USER).openapi()
    assert set(schema["components"]["schemas"]["StoreStatus"]["properties"]) == contract_fields
