"""The Overview and Quality issues pages' routes: ``POST /v1/evals/overview``,
``POST /v1/evals/quality-issues/search`` and ``POST /v1/evals/quality-issues/triage``, on the store's
traces, with the answers the server's ``app/evals_explorer.py`` gives (MartianSecurity/MartianBackend)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from martian.evals.model import Project, Score, Trace
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER, create_app, organisation_principal
from martian.evals.store import MemoryStore
from martian.evals.store.mlflow_local import MlflowStore

T0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
HOUR = 3_600_000
LOCAL_USER = "bf0808be-f788-5dd7-8fc2-2d28ed350045"
OTHER = str(uuid.UUID(int=7))
METHOD = (
    "Groups persisted execution errors and explicit Boolean failed assessments. Numeric/categorical scores "
    "require their own evaluation policy. These are quality signals, not verified security findings."
)


def trace_id(number: int) -> str:
    return f"tr-{number:032x}"


FIRST, ERRORED, LATEST, ELSEWHERE = trace_id(1), trace_id(2), trace_id(3), trace_id(4)


def score(trace: str, scorer: str, value, error: str = "") -> Score:
    return Score(
        trace_id=trace,
        scorer=scorer,
        source="human" if error == "" and isinstance(value, bool) else "llm_judge",
        source_id="reviewer",
        value=value,
        passed=value if isinstance(value, bool) else None,
        error=error,
        created_at=T0,
    )


async def seed(store, principal) -> None:
    await store.create_project(principal, Project(id=DEFAULT_PROJECT, name="Default", created_at=T0))
    await store.create_project(principal, Project(id=OTHER, name="Checkout", created_at=T0))
    traces = [
        (FIRST, DEFAULT_PROJECT, "ok", T0, 100.0, 10, 0.01, [score(FIRST, "helpful", True)]),
        (
            ERRORED,
            DEFAULT_PROJECT,
            "error",
            T0 + timedelta(hours=1),
            300.0,
            None,
            None,
            [score(ERRORED, "helpful", False)],
        ),
        (
            LATEST,
            DEFAULT_PROJECT,
            "ok",
            T0 + timedelta(hours=3),
            200.0,
            30,
            None,
            [
                score(LATEST, "tone", "warm"),
                score(LATEST, "quality", 0.5),
                score(LATEST, "helpful", None, "judge failed"),
            ],
        ),
        (ELSEWHERE, OTHER, "error", T0, 50.0, None, None, []),
    ]
    for identifier, project, state, at, latency, tokens, cost, scores in traces:
        await store.record_trace(
            principal,
            Trace(
                id=identifier,
                project_id=project,
                name="answer",
                state=state,
                request_time=at,
                latency_ms=latency,
                request="Where is my order?",
                response="On its way.",
                tokens=tokens,
                cost=cost,
            ),
        )
        for each in scores:
            await store.log_score(principal, project, identifier, each)


@pytest.fixture
def records(tmp_path):
    found = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield found
    found.close()


@pytest.fixture(params=["memory", "mlflow"])
def store(request, tmp_path):
    return MemoryStore() if request.param == "memory" else MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path)


def client_for(store, records, principal=SINGLE_USER) -> TestClient:
    return TestClient(create_app(store=store, records=records, principal=lambda: principal))


@pytest.fixture
def client(store, records):
    asyncio.run(seed(store, SINGLE_USER))
    return client_for(store, records)


def test_the_overview_summarises_the_projects_traces(client):
    overview = client.post("/v1/evals/overview", json={}).json()
    assert overview == {
        "requests": 3,
        "errors": 1,
        "latency": {"count": 3, "p50": 200.0, "p95": 300.0},
        "tokens": {"reported": 2, "total": 40},
        "cost": {"reported": 1, "total": 0.01},
        "feedback": {
            "helpful": {"kind": "boolean", "count": 2, "pass_rate": 0.5},
            "tone": {"kind": "categorical", "count": 1, "values": {"warm": 1}},
            "quality": {"kind": "numeric", "count": 1, "mean": 0.5},
        },
        "has_more": False,
        "limit": 1000,
        "bucket_ms": HOUR,
        "series": [
            {"timestamp_ms": int(T0.timestamp() * 1000), "requests": 1, "errors": 0, "latency_ms": 100.0},
            {"timestamp_ms": int(T0.timestamp() * 1000) + HOUR, "requests": 1, "errors": 1, "latency_ms": 300.0},
            {"timestamp_ms": int(T0.timestamp() * 1000) + 2 * HOUR, "requests": 0, "errors": 0, "latency_ms": None},
            {"timestamp_ms": int(T0.timestamp() * 1000) + 3 * HOUR, "requests": 1, "errors": 0, "latency_ms": 200.0},
        ],
    }


def test_the_overview_takes_the_trace_searchs_filters_and_project(client):
    errors = client.post("/v1/evals/overview", json={"state": "ERROR"}).json()
    assert (errors["requests"], errors["errors"], errors["feedback"]) == (
        1,
        1,
        {"helpful": {"kind": "boolean", "count": 1, "pass_rate": 0.0}},
    )
    other = client.post("/v1/evals/overview", json={"project_id": OTHER}).json()
    assert (other["requests"], other["errors"], other["latency"]["p50"]) == (1, 1, 50.0)
    assert client.post("/v1/evals/overview", json={"project_id": str(uuid.UUID(int=99))}).status_code == 404


def test_an_aggregate_starts_from_the_first_trace(client):
    for path in ("/v1/evals/overview", "/v1/evals/quality-issues/search"):
        refused = client.post(path, json={"cursor": "page-2"})
        assert refused.status_code == 422
        assert refused.json() == {"detail": "Start a fresh search for an aggregate or export"}


def test_quality_issues_group_execution_errors_and_failed_boolean_assessments(client):
    issues = client.post("/v1/evals/quality-issues/search", json={}).json()
    assert {key: issues[key] for key in ("has_more", "traces_examined", "limit", "method")} == {
        "has_more": False,
        "traces_examined": 3,
        "limit": 1000,
        "method": METHOD,
    }
    # The errored judge's verdict on the latest trace is not a failure, and its categorical and
    # numeric scores are no issue.
    assert [(g["id"], g["name"], g["count"], g["open"], [t["id"] for t in g["traces"]]) for g in issues["items"]] == [
        ("execution_error", "Execution errors", 1, 1, [ERRORED]),
        ("failed:helpful", "Failed helpful", 1, 1, [ERRORED]),
    ]
    assert issues["items"][0]["traces"][0]["triage"] is None


def test_triage_records_the_decision_on_each_trace_and_closes_its_issues(client):
    body = {"trace_ids": [ERRORED], "state": "resolved", "rationale": "  Known outage  "}
    response = client.post("/v1/evals/quality-issues/triage", json=body)
    assert response.status_code == 200
    assert response.json() == {"persisted": 1, "state": "resolved"}
    issues = client.post("/v1/evals/quality-issues/search", json={}).json()
    assert [(g["id"], g["count"], g["open"]) for g in issues["items"]] == [
        ("execution_error", 1, 0),
        ("failed:helpful", 1, 0),
    ]
    triage = issues["items"][0]["traces"][0]["triage"]
    assert {key: triage[key] for key in ("state", "rationale", "actor_id")} == {
        "state": "resolved",
        "rationale": "Known outage",
        "actor_id": LOCAL_USER,
    }
    assert datetime.fromisoformat(triage["at"]).tzinfo is not None
    # The trace search shows the same decision.
    (row,) = client.post("/v1/evals/traces/search", json={"state": "ERROR"}).json()["items"]
    assert row["triage"] == triage
    # Investigating keeps an issue open.
    client.post("/v1/evals/quality-issues/triage", json={**body, "state": "investigating", "rationale": "Looking"})
    issues = client.post("/v1/evals/quality-issues/search", json={}).json()
    assert [g["open"] for g in issues["items"]] == [1, 1]


def test_triage_of_a_trace_the_project_lacks_changes_nothing(client):
    body = {"trace_ids": [ERRORED, ELSEWHERE], "state": "dismissed", "rationale": "Noise"}
    refused = client.post("/v1/evals/quality-issues/triage", json=body)
    assert (refused.status_code, refused.json()) == (404, {"detail": "Trace not found"})
    issues = client.post("/v1/evals/quality-issues/search", json={}).json()
    assert issues["items"][0]["traces"][0]["triage"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"trace_ids": [], "state": "resolved", "rationale": "Done"},
        {"trace_ids": ["not-a-trace"], "state": "resolved", "rationale": "Done"},
        {"trace_ids": [ERRORED, ERRORED], "state": "resolved", "rationale": "Done"},
        {"trace_ids": [ERRORED], "state": "fixed", "rationale": "Done"},
        {"trace_ids": [ERRORED], "state": "resolved", "rationale": "   "},
        {"trace_ids": [ERRORED], "state": "resolved", "rationale": "Done", "note": "extra"},
    ],
)
def test_a_triage_request_the_contract_refuses_is_422(client, body):
    assert client.post("/v1/evals/quality-issues/triage", json=body).status_code == 422


def test_a_viewer_reads_the_issues_but_cannot_triage_them(store, records):
    viewer = organisation_principal("viewer-1", "org-a", frozenset({"viewer"}))
    asyncio.run(seed(store, viewer))
    client = client_for(store, records, viewer)
    assert client.post("/v1/evals/quality-issues/search", json={}).json()["traces_examined"] == 3
    assert client.post("/v1/evals/overview", json={}).json()["requests"] == 3
    refused = client.post(
        "/v1/evals/quality-issues/triage", json={"trace_ids": [ERRORED], "state": "resolved", "rationale": "x"}
    )
    assert (refused.status_code, refused.json()) == (403, {"detail": "Requires member role"})


def test_an_archived_project_takes_no_triage(store, records):
    asyncio.run(seed(store, SINGLE_USER))
    asyncio.run(store.archive_project(SINGLE_USER, OTHER))
    client = client_for(store, records)
    body = {"project_id": OTHER, "trace_ids": [ELSEWHERE], "state": "resolved", "rationale": "Done"}
    refused = client.post("/v1/evals/quality-issues/triage", json=body)
    assert (refused.status_code, refused.json()) == (
        409,
        {"detail": "This evaluation project is archived or unavailable"},
    )


def test_before_the_default_project_exists_there_is_nothing_to_summarise(records):
    client = client_for(MemoryStore(), records)
    overview = client.post("/v1/evals/overview", json={}).json()
    assert (overview["requests"], overview["series"], overview["bucket_ms"], overview["has_more"]) == (
        0,
        [],
        HOUR,
        False,
    )
    issues = client.post("/v1/evals/quality-issues/search", json={}).json()
    assert (issues["items"], issues["traces_examined"], issues["has_more"]) == ([], 0, False)


def test_an_aggregate_reads_the_newest_thousand_traces_and_says_there_are_more(records):
    store = MemoryStore()

    async def many() -> None:
        await store.create_project(SINGLE_USER, Project(id=DEFAULT_PROJECT, name="Default", created_at=T0))
        for number in range(1001):
            at = T0 + timedelta(minutes=number)
            trace = Trace(id=trace_id(100 + number), project_id=DEFAULT_PROJECT, state="error", request_time=at)
            await store.record_trace(SINGLE_USER, trace)

    asyncio.run(many())
    client = client_for(store, records)
    overview = client.post("/v1/evals/overview", json={}).json()
    assert (overview["requests"], overview["errors"], overview["has_more"]) == (1000, 1000, True)
    issues = client.post("/v1/evals/quality-issues/search", json={}).json()
    (group,) = issues["items"]
    assert (issues["traces_examined"], issues["has_more"], group["count"], len(group["traces"])) == (
        1000,
        True,
        1000,
        100,
    )
    # The newest first: the oldest trace is the one left out.
    assert trace_id(100) not in {t["id"] for t in group["traces"]} and group["traces"][0]["id"] == trace_id(1100)
    exactly = client.post(
        "/v1/evals/overview", json={"start_time_ms": int((T0 + timedelta(minutes=1)).timestamp() * 1000)}
    )
    assert (exactly.json()["requests"], exactly.json()["has_more"]) == (1000, False)
