"""The worker's limits (MorningHQ/martian-notes#144): every target call has a time limit, a function
that blocks or never returns included, and a run or scoring job stops at its time or judge token
limit, keeps what it finished, and resumes after it. A fake agent and a deterministic judge stand
in for the target and the model; nothing calls a model or a network target."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
import time
from typing import Any

import pytest
from fastapi import HTTPException

from martian.evals.model import RunLimits
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import SINGLE_USER
from martian.evals.server.launch import resume_run, resume_scoring
from martian.evals.server.projection import DEFAULT_PROJECT
from martian.evals.store import MemoryStore
from martian.evals.worker import JudgeRequest, JudgeResult
from tests.evals.write_fixture import AGENT, Served, agent, judge, request_id, serve


@pytest.fixture
def records(tmp_path):
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


def items(run: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {item["dataset_item_id"]: (item["status"], item["error"]) for item in run["items"]}


def stored_run(served: Served, run_id: str):
    assert served.client.portal is not None
    return served.client.portal.call(served.records.get_run, SINGLE_USER, DEFAULT_PROJECT, run_id)


def test_a_function_that_blocks_or_never_returns_is_a_timed_out_case_and_the_run_finishes(records):
    async def hung(case):
        if case.id == "faq-1":
            time.sleep(6)  # blocks whichever event loop runs it
        if case.id == "faq-2":  # ignores being cancelled, for long after its deadline
            for _ in range(16):
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.5)
        return await agent(case)

    client, served = serve(MemoryStore(), records, targets={AGENT: hung}, target_timeout_seconds=0.3)
    with client:
        started = time.monotonic()
        run = served.launch()
        served.drain()
        elapsed = time.monotonic() - started
        run = served.call("GET", f"/v1/evals/{run['id']}", 200)
    # Each hung case ends at its deadline as an error, and the run goes on to its end.
    assert (run["state"], run["errored"], run["skipped"]) == ("completed", 2, 1)
    assert items(run)["faq-1"] == ("error", "item timed out")
    assert items(run)["faq-2"] == ("error", "item timed out")
    assert elapsed < 5, elapsed


def test_a_run_stops_at_its_time_limit_and_resumes_after_its_finished_cases(records, monkeypatch):
    import martian.evals.limits

    called: list[str] = []
    # The run's clock runs an hour on once its first case is in flight, so its time is up then.
    real = time.monotonic
    monkeypatch.setattr(martian.evals.limits, "monotonic", lambda: real() + 3600 * (len(called) == 1))

    async def counted(case):
        called.append(case.id)
        return await agent(case)

    client, served = serve(MemoryStore(), records, targets={AGENT: counted}, limits=RunLimits(max_seconds=60))
    with client:
        run = served.launch()
        served.drain()
        stopped = served.call("GET", f"/v1/evals/{run['id']}", 200)
        # It stopped before its second case: the first is kept, the others have no result.
        assert stopped["state"] == "stopped"
        assert re.fullmatch(r"Stopped at its time limit \(max_seconds 60\) after 36\d\d seconds", stopped["error"])
        assert items(stopped) == {"faq-1": ("passed", "")}
        assert (stopped["planned_count"], stopped["passed"], stopped["skipped"]) == (3, 1, 0)
        assert stopped["provenance"]["limits"] == {"max_seconds": 60.0, "max_judge_tokens": None}
        assert stopped["provenance"]["stopped"]["limit"] == "max_seconds"
        assert served.call("POST", f"/v1/evals/{run['id']}/cancel", 409)["detail"].startswith("Execution has finished")

        # Resumed with a higher limit, it calls only the cases without a result.
        found = stored_run(served, run["id"])
        assert client.portal is not None
        client.portal.call(
            lambda: resume_run(
                served.store, records, served.worker, SINGLE_USER, found, limits=RunLimits(max_seconds=120)
            )
        )
        served.drain()
        resumed = served.call("GET", f"/v1/evals/{run['id']}", 200)
        # Only a stopped run resumes.
        again = stored_run(served, run["id"])
        with pytest.raises(HTTPException) as refused:
            client.portal.call(
                lambda: resume_run(served.store, records, served.worker, SINGLE_USER, again, limits=None)
            )
    assert (resumed["state"], resumed["passed"], resumed["failed"], resumed["skipped"]) == ("completed", 1, 1, 1)
    assert called == ["faq-1", "faq-2"]
    assert resumed["provenance"]["limits"] == {"max_seconds": 120.0, "max_judge_tokens": None}
    assert "stopped" not in resumed["provenance"] and resumed["error"] == ""
    assert refused.value.status_code == 409


def test_a_scoring_job_stops_at_its_judge_token_limit_and_resumes_after_its_scored_cases(records):
    judged: list[str] = []

    async def counted(request: JudgeRequest) -> JudgeResult:
        judged.append(request.prompt)
        return dataclasses.replace(await judge(request), tokens=15)

    limits = RunLimits(max_judge_tokens=10)
    client, served = serve(MemoryStore(), records, judge_with=counted, limits=limits)
    with client:
        run = served.completed_run()
        assert (run["state"], run["provenance"]["limits"]) == ("completed", limits.model_dump(mode="json"))
        body = {"request_id": request_id(), "scorers": ["correctness"], "acknowledge_model_cost": True}
        served.call("POST", f"/v1/evals/{run['id']}/scores", 202, json=body)
        served.drain()
        [job] = served.call("GET", f"/v1/evals/{run['id']}/workbench", 200)["jobs"]
        # The first case's judgment used 15 tokens, past the limit of 10: the job stopped there.
        assert job["state"] == "stopped"
        assert job["error"] == "Stopped at its judge token limit (max_judge_tokens 10) after 15 tokens"
        assert (job["succeeded"], job["errors"]) == (1, 0)
        assert judged == ["How do I reset my password?"]

        found = stored_run(served, run["id"])
        assert client.portal is not None
        jobs = client.portal.call(served.records.list_scoring_jobs, SINGLE_USER, DEFAULT_PROJECT, run["id"])
        client.portal.call(
            lambda: resume_scoring(
                records, served.worker, SINGLE_USER, found, jobs[0], limits=RunLimits(max_judge_tokens=1000)
            )
        )
        served.drain()
        [resumed] = served.call("GET", f"/v1/evals/{run['id']}/workbench", 200)["jobs"]
        after = served.call("GET", f"/v1/evals/{run['id']}", 200)
    # Resumed, the job judges only the case it had not scored; the third case has no output.
    assert judged == ["How do I reset my password?", "Can I change my username?"]
    assert (resumed["state"], resumed["succeeded"], resumed["errors"]) == ("partial", 2, 1)
    assert after["provenance"]["limits"] == {"max_seconds": None, "max_judge_tokens": 1000}


def test_a_judge_that_reports_no_tokens_stops_a_job_that_has_a_token_limit(records):
    client, served = serve(MemoryStore(), records, limits=RunLimits(max_judge_tokens=1000))
    with client:
        run = served.completed_run()
        body = {"request_id": request_id(), "scorers": ["correctness"], "acknowledge_model_cost": True}
        served.call("POST", f"/v1/evals/{run['id']}/scores", 202, json=body)
        served.drain()
        [job] = served.call("GET", f"/v1/evals/{run['id']}/workbench", 200)["jobs"]
    assert (job["state"], job["succeeded"]) == ("stopped", 1)
    assert job["error"] == (
        "Stopped: the judge reported no token usage, so its token limit (max_judge_tokens) cannot be kept"
    )
