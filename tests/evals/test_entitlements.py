"""One map from edition to feature keys; the edition comes from a licence, a plan or nothing."""

from typing import Annotated

import pytest
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient

from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import (
    COMMUNITY,
    EDITIONS,
    ENTERPRISE,
    FEATURES,
    LIMITS,
    ON_PREMISES,
    SINGLE_USER,
    Entitlements,
    Principal,
    create_app,
    ensure_entitled,
    entitlements_for,
    organisation_principal,
    require_entitlement,
    resolve_edition,
)
from martian.evals.store import MemoryStore
from tests.evals.evals_contract import operation


@pytest.fixture
def records(tmp_path):
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


def test_the_key_list_and_the_edition_sets():
    assert dict(FEATURES) == {"review_queues": "review queues", "monitoring": "monitoring"}
    assert dict(LIMITS) == {}
    assert COMMUNITY.features == frozenset()
    assert ENTERPRISE.features == ON_PREMISES.features == frozenset(FEATURES)
    assert {edition: e.edition for edition, e in EDITIONS.items()} == {
        "community": "community",
        "enterprise": "enterprise",
        "on_premises": "on_premises",
    }
    assert all(not e.limits and e.limit("anything") is None for e in EDITIONS.values())


def test_the_edition_comes_from_a_licence_then_a_plan_then_nothing():
    assert resolve_edition() == "community"
    assert resolve_edition(plan="community") == "community"
    assert resolve_edition(plan="enterprise") == "enterprise"
    assert resolve_edition(licence="on_premises") == "on_premises"
    assert resolve_edition(plan="community", licence="on_premises") == "on_premises"
    assert entitlements_for(resolve_edition(plan="enterprise")) is ENTERPRISE
    with pytest.raises(ValueError, match="organisation plan"):
        resolve_edition(plan="on_premises")
    with pytest.raises(ValueError, match="organisation plan"):
        resolve_edition(plan="")
    with pytest.raises(ValueError, match="licence"):
        resolve_edition(licence="gold")


def test_entitlements_hold_only_known_keys():
    with pytest.raises(ValueError, match="unknown feature keys"):
        Entitlements(edition="enterprise", features=frozenset({"sso"}))
    with pytest.raises(ValueError, match="unknown limit keys"):
        Entitlements(edition="enterprise", limits={"seats": 5})
    with pytest.raises(ValueError, match="unknown feature key"):
        require_entitlement("sso")


def _app_with_paid_route(records, entitlements):
    """The core's own review queue route, which the edition map gates."""
    plans = {"org-community": "community", "org-enterprise": "enterprise"}

    def from_header(x_org: Annotated[str | None, Header()] = None) -> Principal:
        if x_org is None:
            raise HTTPException(401, "Sign in")
        return organisation_principal("user-1", x_org, frozenset({"member"}))

    return create_app(
        records=records,
        store=MemoryStore(),
        principal=from_header,
        entitlements=entitlements or (lambda p: entitlements_for(resolve_edition(plan=plans[p.organisation_id]))),
    )


NO_QUEUES = {"items": [], "total": 0, "has_more": False, "page": 1, "limit": 25}


def test_a_paid_route_is_refused_in_community_with_a_clear_error(records):
    client = TestClient(_app_with_paid_route(records, None))
    refused = client.get("/v1/evals/review-queues", headers={"x-org": "org-community"})
    assert refused.status_code == 403
    assert refused.json() == {"detail": "The Community edition does not include review queues"}
    allowed = client.get("/v1/evals/review-queues", headers={"x-org": "org-enterprise"})
    assert (allowed.status_code, allowed.json()) == (200, NO_QUEUES)
    assert client.get("/v1/evals/review-queues").status_code == 401  # sign-in is checked first


def test_the_local_edition_is_community_by_default_and_refuses_paid_routes(records):
    app = create_app(records=records, store=MemoryStore(), principal=lambda: SINGLE_USER)
    response = TestClient(app).get("/v1/evals/monitoring")
    assert (response.status_code, response.json()) == (
        403,
        {"detail": "The Community edition does not include monitoring"},
    )


def test_part_of_a_request_can_need_a_feature():
    ensure_entitled(ENTERPRISE, "review_queues")
    with pytest.raises(HTTPException) as refused:
        ensure_entitled(COMMUNITY, "review_queues")
    assert (refused.value.status_code, refused.value.detail) == (
        403,
        "The Community edition does not include review queues",
    )


def test_an_on_premises_licence_turns_the_paid_routes_on(records):
    licence = _app_with_paid_route(records, lambda _p: entitlements_for(resolve_edition(licence="on_premises")))
    assert TestClient(licence).get("/v1/evals/review-queues", headers={"x-org": "org-community"}).status_code == 200


def test_the_refusal_adds_nothing_to_the_route_contract(records):
    openapi = _app_with_paid_route(records, None).openapi()["paths"]
    for path in ("/v1/evals/review-queues", "/v1/evals/monitoring"):
        assert openapi[path]["get"] == operation("get", path)
