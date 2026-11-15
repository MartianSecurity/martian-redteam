"""One bootstrap response tells the UI who is signed in, the edition, and what to show."""

import uuid

import pytest
from fastapi.testclient import TestClient

from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import (
    CAPABILITIES,
    COMMUNITY,
    EDITIONS,
    ENTERPRISE,
    SINGLE_USER,
    Principal,
    capabilities_for,
    create_app,
    entitlements_for,
    organisation_principal,
    resolve_edition,
)
from martian.evals.server.bootstrap import EvalCapabilities
from martian.evals.store import MemoryStore, StoreStatus

# These tests read no records; an in-memory record store satisfies create_app.
RECORDS = SqlRecordStore("sqlite://")

# The local user `local`, as the API names it: a UUID derived from it.
LOCAL_USER = "bf0808be-f788-5dd7-8fc2-2d28ed350045"
MEMBER_ID = str(uuid.UUID(int=1))

WRITERS = {"run", "score", "review", "createReviewQueues", "saveSuites", "saveViews", "editDrafts"}
ADMINS = WRITERS | {
    "publishDatasets",
    "assignReviews",
    "manageViews",
    "manageApiKeys",
    "retryCiExport",
    "manageProjects",
    "manageJudges",
}
PAID = {"createReviewQueues"}  # needs the review_queues feature

EXPECTED = {
    ("community", "owner"): ADMINS - PAID,
    ("community", "admin"): ADMINS - PAID,
    ("community", "member"): WRITERS - PAID,
    ("community", "viewer"): set(),
    ("enterprise", "owner"): ADMINS,
    ("enterprise", "admin"): ADMINS,
    ("enterprise", "member"): WRITERS,
    ("enterprise", "viewer"): set(),
    ("on_premises", "owner"): ADMINS,
    ("on_premises", "admin"): ADMINS,
    ("on_premises", "member"): WRITERS,
    ("on_premises", "viewer"): set(),
}

# The shape of EvalsBootstrapOut in contracts/evals-api.json in MartianSecurity/MartianBackend: each
# object's properties. The server's test pins the same shape.
SHAPE = {
    "Bootstrap": {"principal", "edition", "entitlements", "capabilities", "status"},
    "BootstrapPrincipal": {"kind", "user", "organisation", "role", "platform_admin"},
    "BootstrapUser": {"id", "email", "display_name"},
    "BootstrapOrganisation": {"id", "name"},
    "BootstrapEdition": {"key", "name"},
    "BootstrapEntitlements": {"features", "limits"},
    "EvalCapabilities": ADMINS,
    "StoreStatus": {"status", "ready", "error_code", "updated_at", "provider", "admission_paused", "routing_epoch"},
}


def test_the_local_edition_answers_with_its_fixed_principal_in_community():
    client = TestClient(create_app(records=RECORDS, store=MemoryStore(), principal=lambda: SINGLE_USER))
    response = client.get("/v1/evals/bootstrap")
    assert response.status_code == 200
    assert response.json() == {
        "principal": {
            "kind": "single_user",
            "user": {"id": LOCAL_USER, "email": None, "display_name": None},
            "organisation": None,
            "role": "owner",
            "platform_admin": False,
        },
        "edition": {"key": "community", "name": "Community"},
        "entitlements": {"features": [], "limits": {"concurrent_runs": 1, "concurrent_scoring_jobs": 1}},
        "capabilities": {name: name in ADMINS - PAID for name in ADMINS},
        "status": {
            "status": "ready",
            "ready": True,
            "error_code": None,
            "updated_at": None,
            "provider": None,
            "admission_paused": False,
            "routing_epoch": 0,
        },
    }


@pytest.mark.parametrize(("edition", "role"), sorted(EXPECTED))
def test_each_edition_and_role_gets_its_capabilities(edition, role):
    member = organisation_principal(
        MEMBER_ID,
        "org-a",
        frozenset({role}),
        email="ada@example.test",
        display_name="Ada",
        organisation_name="Org A",
    )
    store = MemoryStore(StoreStatus(status="provisioning", ready=False, provider="mlflow"))
    app = create_app(
        records=RECORDS, store=store, principal=lambda: member, entitlements=lambda _p: entitlements_for(edition)
    )
    body = TestClient(app).get("/v1/evals/bootstrap").json()
    assert body["principal"] == {
        "kind": "organisation",
        "user": {"id": MEMBER_ID, "email": "ada@example.test", "display_name": "Ada"},
        "organisation": {"id": "org-a", "name": "Org A"},
        "role": role,
        "platform_admin": False,
    }
    assert body["edition"]["key"] == edition
    assert body["entitlements"] == {
        "features": sorted(EDITIONS[edition].features),
        "limits": dict(EDITIONS[edition].limits),
    }
    assert {name for name, allowed in body["capabilities"].items() if allowed} == EXPECTED[(edition, role)]
    assert set(body["capabilities"]) == ADMINS
    assert (body["status"]["status"], body["status"]["ready"]) == ("provisioning", False)


def test_the_edition_names_come_from_the_map():
    names = {
        edition: TestClient(
            create_app(
                records=RECORDS,
                store=MemoryStore(),
                principal=lambda: SINGLE_USER,
                entitlements=lambda _p, e=edition: e,
            )
        )
        .get("/v1/evals/bootstrap")
        .json()["edition"]
        for edition in (COMMUNITY, ENTERPRISE, entitlements_for(resolve_edition(licence="on_premises")))
    }
    assert sorted(names.values(), key=lambda e: e["key"]) == [
        {"key": "community", "name": "Community"},
        {"key": "enterprise", "name": "Enterprise"},
        {"key": "on_premises", "name": "On-Premises"},
    ]


def test_a_principal_with_no_role_may_do_nothing():
    nobody = Principal(user_id="user-2", organisation_id="org-a")
    body = (
        TestClient(create_app(records=RECORDS, store=MemoryStore(), principal=lambda: nobody))
        .get("/v1/evals/bootstrap")
        .json()
    )
    assert body["principal"]["role"] is None
    assert not any(body["capabilities"].values())


def test_the_highest_role_decides():
    both = Principal(user_id="user-3", organisation_id="org-a", roles=frozenset({"viewer", "admin"}))
    body = (
        TestClient(create_app(records=RECORDS, store=MemoryStore(), principal=lambda: both))
        .get("/v1/evals/bootstrap")
        .json()
    )
    assert body["principal"]["role"] == "admin"
    assert body["capabilities"]["manageProjects"] is True


def test_the_table_and_the_response_name_the_same_capabilities():
    assert set(CAPABILITIES) == set(EvalCapabilities.model_fields) == ADMINS
    assert capabilities_for("owner", ENTERPRISE) == dict.fromkeys(ADMINS, True)


def test_the_shape_is_the_frozen_contracts():
    schemas = create_app(records=RECORDS, store=MemoryStore(), principal=lambda: SINGLE_USER).openapi()["components"][
        "schemas"
    ]
    assert {name: set(schemas[name]["properties"]) for name in SHAPE} == SHAPE
    assert all(set(schemas[name].get("required", [])) == SHAPE[name] for name in SHAPE if name != "StoreStatus")


def test_the_single_local_user_names_no_organisation():
    with pytest.raises(ValueError, match="no organisation to name"):
        Principal(user_id="local", organisation_name="Org A")
