"""Run limits and resume in the evals contract (MartianSecurity/martian-notes#346): the launch body takes
optional ``limits``, ``POST /v1/evals/{run_id}/resume`` goes on with a stopped run, and both are the
contract's own (version 4). A fake agent stands in for the target; nothing calls a model or a network
target."""

from __future__ import annotations

import time
from typing import Any

import pytest

from martian.evals.records.sql import SqlRecordStore
from martian.evals.store import MemoryStore
from tests.evals.evals_contract import VERSION, operation, validate_request, validate_response
from tests.evals.write_fixture import AGENT, Served, agent, request_id, serve


@pytest.fixture
def records(tmp_path):
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


def kept(run: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {item["dataset_item_id"]: (item["status"], item["error"]) for item in run["items"]}


def stopped_after_first_case(records, monkeypatch, called: list[str]) -> tuple[Any, Served, dict[str, Any]]:
    """A served core whose run clock runs an hour on once the first case is in flight, and a run
    launched through the contract with a wall-time limit of a minute, which stops there."""
    import martian.evals.limits

    real = time.monotonic
    monkeypatch.setattr(martian.evals.limits, "monotonic", lambda: real() + 3600 * (len(called) == 1))

    async def counted(case):
        called.append(case.id)
        return await agent(case)

    client, served = serve(MemoryStore(), records, targets={AGENT: counted})
    return client, served, {"limits": {"max_seconds": 60}}


def test_the_contract_is_version_4_and_names_limits_and_resume():
    assert VERSION == "4"
    launch = operation("post", "/v1/evals")["requestBody"]["content"]["application/json"]["schema"]
    assert launch == {"$ref": "#/components/schemas/EvalRunRequest"}
    resume = operation("post", "/v1/evals/{run_id}/resume")
    assert resume["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EvalRunOut"
    }
    # Optional fields only: a version 3 client's bodies are still valid.
    validate_request("EvalRunRequest", {"agent_id": AGENT, "dataset_name": "support-faq"})
    validate_request(
        "EvalRunRequest", {"agent_id": AGENT, "dataset_name": "support-faq", "limits": {"max_seconds": 60}}
    )


def test_a_run_launched_with_a_time_limit_stops_at_it_and_resumes_without_repeating_finished_cases(
    records, monkeypatch
):
    called: list[str] = []
    client, served, limits = stopped_after_first_case(records, monkeypatch, called)
    with client:
        body = {"request_id": request_id(), **limits}
        validate_request("EvalRunRequest", {"agent_id": AGENT, "dataset_name": "support-faq", **body})
        launched = served.launch(**body)
        validate_response("POST", "/v1/evals", launched, 202)
        assert launched["provenance"]["limits"] == {"max_seconds": 60.0, "max_judge_tokens": None}
        served.drain()
        stopped = served.call("GET", f"/v1/evals/{launched['id']}", 200)
        # It stopped before its second case: the first is kept, the others have no result.
        assert stopped["state"] == "stopped"
        assert stopped["error"].startswith("Stopped at its time limit (max_seconds 60) after ")
        assert stopped["provenance"]["stopped"]["limit"] == "max_seconds"
        assert kept(stopped) == {"faq-1": ("passed", "")}
        assert called == ["faq-1"]

        # Resumed through the contract's route, with a higher limit, it calls only the rest.
        resumed = served.call("POST", f"/v1/evals/{launched['id']}/resume", 202, json={"limits": {"max_seconds": 120}})
        validate_response("POST", "/v1/evals/{run_id}/resume", resumed, 202)
        assert resumed["state"] == "queued"
        assert resumed["provenance"]["limits"] == {"max_seconds": 120.0, "max_judge_tokens": None}
        served.drain()
        done = served.call("GET", f"/v1/evals/{launched['id']}", 200)
    assert (done["state"], done["passed"], done["failed"], done["skipped"]) == ("completed", 1, 1, 1)
    assert called == ["faq-1", "faq-2"]  # faq-1 was not called again
    assert "stopped" not in done["provenance"] and done["error"] == ""


def test_a_resume_without_a_body_keeps_the_runs_limits(records, monkeypatch):
    called: list[str] = []
    client, served, limits = stopped_after_first_case(records, monkeypatch, called)
    with client:
        run = served.launch(**limits)
        served.drain()
        resumed = served.call("POST", f"/v1/evals/{run['id']}/resume", 202)
    assert resumed["provenance"]["limits"] == {"max_seconds": 60.0, "max_judge_tokens": None}


def test_only_a_stopped_run_resumes_and_a_bad_limit_is_refused(records, monkeypatch):
    called: list[str] = []
    client, served, limits = stopped_after_first_case(records, monkeypatch, called)
    with client:
        run = served.launch(**limits)
        served.drain()
        served.call("POST", f"/v1/evals/{run['id']}/resume", 202, json={})
        served.drain()
        done = served.call("GET", f"/v1/evals/{run['id']}", 200)
        assert done["state"] == "completed"
        # A completed run does not resume, nor does one that is not there.
        refused = served.call("POST", f"/v1/evals/{run['id']}/resume", 409, json={})
        assert refused["detail"] == "Only a run stopped at one of its limits can be resumed"
        served.call("POST", f"/v1/evals/{request_id()}/resume", 404, json={})
        # Limits are above zero, and the body has no other field.
        served.call("POST", f"/v1/evals/{run['id']}/resume", 422, json={"limits": {"max_seconds": 0}})
        served.call("POST", f"/v1/evals/{run['id']}/resume", 422, json={"limits": {"max_judge_tokens": 0}})
        served.call("POST", f"/v1/evals/{run['id']}/resume", 422, json={"limit": {"max_seconds": 5}})
        served.call(
            "POST", "/v1/evals", 422, json={"agent_id": AGENT, "dataset_name": "support-faq", "limits": {"x": 1}}
        )


def test_a_repeated_launch_with_other_limits_is_a_different_request(records):
    client, served = serve(MemoryStore(), records)
    with client:
        body = {"request_id": request_id(), "limits": {"max_seconds": 600}}
        first = served.launch(**body)
        assert served.launch(**body)["id"] == first["id"]
        # Without limits of its own, the repeat is the same request; with other limits it is not.
        assert served.launch(request_id=body["request_id"])["id"] == first["id"]
        refused = served.call(
            "POST",
            "/v1/evals",
            409,
            json={"agent_id": AGENT, "dataset_name": "support-faq", **body, "limits": {"max_seconds": 900}},
        )
    assert refused["detail"] == "request_id was already used for a different eval"
