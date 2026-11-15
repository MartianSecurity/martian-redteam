"""A judge version's calibration goes stale when the model it runs on or what it means changes, and a
re-check grades exactly the labelled items it rests on again and refreshes it. A fake judge
answers for the core's worker and a loopback OpenAI-compatible endpoint for the SDK judge; nothing
calls a real model."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import mlflow
import pytest

from martian.evals.agreement import MINIMUM_LABELLED
from martian.evals.calibration import NOT_CALIBRATED, recheck_calibration, save_calibration
from martian.evals.gate import STATE
from martian.evals.judge_settings import FileJudgeSettings, JudgeModel
from martian.evals.local import SETTINGS_FILE, open_stores
from martian.evals.model import CaseResult, Review, Run, Score, ScorerVersion, Trace
from martian.evals.records import CalibrationRecheck
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER
from martian.evals.store import MemoryStore, StoreError
from martian.evals.worker import JudgeError, JudgeRequest, JudgeResult, Verdict, Work
from martian.evals.worker.execute import fail_unfinished, take_over
from martian.evals.worker.judges import RUBRICS
from martian.evals.worker.queue import entry_id
from tests.evals.test_worker_judges import KEY, Loopback
from tests.evals.write_fixture import Served, request_id, serve

T0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
CASES = MINIMUM_LABELLED - 2
"""Labelled case results; two labelled traces of live traffic make up the rest."""


def polite(i: int) -> bool:
    return i % 3 != 0


def output(i: int) -> str:
    return f"A polite answer {i}" if polite(i) else f"A curt answer {i}"


LABELLED = [output(i) for i in range(CASES)] + ["A polite live answer", "A curt live answer"]
"""The text of every labelled item: what a re-check grades again, and nothing else."""


def live(n: int) -> str:
    return "tr-" + f"{n + 900:032x}"


async def labelled(store, records, judge: str, version: str, *, model: str) -> str:
    """A completed run whose cases the judge version passed on ``model``, and two traces of live
    traffic it passed too. People pass a polite output and fail a curt one: they labelled every
    case but the last three, by review, and both traces, by annotation. Returns the run's id."""
    principal, run_id = SINGLE_USER, str(uuid.uuid4())
    run = Run(id=run_id, project_id=DEFAULT_PROJECT, code="EV-7", source="console", state="completed", created_at=T0)
    await records.create_run(principal, run)
    cases = [f"case-{i:02d}" for i in range(CASES + 3)]
    results = [
        CaseResult(
            run_id=run_id,
            case_id=case,
            position=i,
            status="passed",
            inputs={"message": f"Question {i}"},
            output=output(i) if i < CASES else f"An unlabelled answer {i}",
        )  # fmt: skip
        for i, case in enumerate(cases)
    ]
    await records.put_results(principal, DEFAULT_PROJECT, results)
    verdict = {"scorer": judge, "scorer_version": version, "source": "llm_judge", "source_id": model, "value": True,
               "passed": True, "created_at": T0}  # fmt: skip
    scores = [Score(run_id=run_id, case_id=case, scoring_job_id="job-0", **verdict) for case in cases]
    await records.put_scores(principal, DEFAULT_PROJECT, scores)
    for i, case in enumerate(cases[:CASES]):
        review = Review(id=f"review-{run_id}-{i}", run_id=run_id, case_id=case, criterion=judge, state="complete",
                        value=polite(i), created_at=T0 + timedelta(minutes=5))  # fmt: skip
        await records.add_review(principal, DEFAULT_PROJECT, review)
    for n, (response, label) in enumerate([("A polite live answer", True), ("A curt live answer", False)]):
        trace = Trace(id=live(n), project_id=DEFAULT_PROJECT, name="live", state="ok", request_time=T0,
                      request="A live question", response=response)  # fmt: skip
        await store.record_trace(principal, trace)
        await store.log_score(principal, DEFAULT_PROJECT, live(n), Score(trace_id=live(n), **verdict))
        feedback = {"martian.annotation": "feedback"}
        annotation = Score(trace_id=live(n), scorer=judge, source="human", value=label, metadata=feedback,
                           created_at=T0 + timedelta(minutes=5))  # fmt: skip
        await store.log_score(principal, DEFAULT_PROJECT, live(n), annotation)
    return run_id


# --- the core and its worker ----------------------------------------------------------------------


@pytest.fixture
def core(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    asked: list[tuple[str | None, str, tuple[str, ...]]] = []
    failing: set[str] = set()

    async def judge(request: JudgeRequest) -> JudgeResult:
        """Passes a polite output and fails any other, from the model the request names; it cannot
        grade an output it is told to fail on."""
        asked.append((request.model, request.output, tuple(s.name for s in request.scorers)))
        if any(word in request.output for word in failing):
            raise JudgeError("Judge draft failed [judge_provider_auth]. Check the judge's endpoint.")
        passed = "polite" in request.output
        verdicts = {s.name: Verdict(passed, "Polite." if passed else "Curt.") for s in request.scorers}
        return JudgeResult(model=request.model or "fake", model_version="2026-11", verdicts=verdicts)

    client, served = serve(MemoryStore(), records, judge_with=judge)
    served.asked = asked  # type: ignore[attr-defined]
    served.failing = failing  # type: ignore[attr-defined]
    with client:
        yield served
    records.close()


def status(core: Served, name: str) -> dict[str, tuple[str, str | None, str | None]]:
    items = core.call("GET", "/v1/evals/judge-calibrations", 200, params={"name": name})["items"]
    return {i["version"]: (i["status"], i["reason"], (i["calibration"] or {}).get("id")) for i in items}


def calibrate(core: Served, name: str, version: str, status_code: int = 201):
    body = {"request_id": request_id(), "name": name, "version": version}
    return core.call("POST", "/v1/evals/judge-calibrations", status_code, json=body)


def tone_on_alpha(core: Served) -> dict:
    """A registered judge, ``tone``, that names no model, calibrated on the default judge model
    ``openai:/alpha``."""
    registered = {"request_id": request_id(), "name": "tone", "instructions": "Is {{ outputs }} polite?"}
    core.call("POST", "/v1/evals/judges", 201, json=registered)
    core.call("PUT", "/v1/evals/judge-model", 200, json={"model": "openai:/alpha"})
    asyncio.run(labelled(core.store, core.records, "tone", "1", model="openai:/alpha"))
    return calibrate(core, "tone", "1")


def where(item: dict) -> tuple[str | None, str | None, str | None]:
    return item["run_id"], item["case_id"], item["trace_id"]


def test_changing_a_judges_model_marks_it_unchecked_and_a_recheck_refreshes_the_record(core):
    saved = tone_on_alpha(core)
    # The calibration records exactly the labelled items it rests on, with their labels and verdicts.
    assert (saved["labelled"], saved["model"], saved["recheck_of"]) == (MINIMUM_LABELLED, "openai:/alpha", None)
    assert [i["case_id"] for i in saved["items"][:CASES]] == [f"case-{i:02d}" for i in range(CASES)]
    assert [i["trace_id"] for i in saved["items"][CASES:]] == [live(0), live(1)]
    labels = [*map(polite, range(CASES)), True, False]
    assert [(i["label"], i["verdict"]) for i in saved["items"]] == [(label, True) for label in labels]
    # The first model passed every item; people passed the 19 polite ones.
    assert saved["report"]["agreement"] == pytest.approx(19 / 30)
    assert status(core, "tone") == {"1": ("checked", None, saved["id"])}

    # The judge names no model, so a new default judge model is a change of its model.
    core.call("PUT", "/v1/evals/judge-model", 200, json={"model": "openai:/beta"})
    assert status(core, "tone") == {"1": ("unchecked", "model_changed", saved["id"])}

    # A re-check calls a model for each item, so it needs the cost acknowledged.
    body = {"request_id": request_id(), "name": "tone", "version": "1"}
    refused = core.call("POST", "/v1/evals/judge-rechecks", 422, json=body)
    assert refused["detail"] == "Model scoring requires cost acknowledgement" and core.asked == []
    body["acknowledge_model_cost"] = True
    queued = core.call("POST", "/v1/evals/judge-rechecks", 202, json=body)
    assert (queued["state"], queued["calibration_id"], queued["model"], queued["total"]) == (
        "queued",
        saved["id"],
        "openai:/beta",
        MINIMUM_LABELLED,
    )
    core.drain()

    done = core.call("GET", f"/v1/evals/judge-rechecks/{queued['id']}", 200)
    assert (done["state"], done["graded"], done["errors"], done["error"]) == ("completed", MINIMUM_LABELLED, 0, "")
    # Exactly the labelled items were graded again, each once, on the model the judge runs on now.
    assert sorted(core.asked) == sorted(("openai:/beta", text, ("tone",)) for text in LABELLED)

    refreshed = core.call("GET", f"/v1/evals/judge-calibrations/{done['result_id']}", 200)
    assert (refreshed["recheck_of"], refreshed["model"], refreshed["labelled"]) == (
        saved["id"],
        "openai:/beta",
        MINIMUM_LABELLED,
    )
    assert refreshed["definition"] == saved["definition"]
    assert [where(i) for i in refreshed["items"]] == [where(i) for i in saved["items"]]
    assert all(i["verdict"] == i["label"] for i in refreshed["items"])
    assert (refreshed["report"]["agreement"], refreshed["report"]["models"]) == (1.0, ["openai:/beta@2026-11"])
    assert status(core, "tone") == {"1": ("checked", None, refreshed["id"])}
    # The calibration it re-checked stays readable for the scores that name it.
    assert core.call("GET", f"/v1/evals/judge-calibrations/{saved['id']}", 200) == saved

    # The same request again is the same re-check, and calls no model again.
    assert core.call("POST", "/v1/evals/judge-rechecks", 202, json=body)["id"] == queued["id"]
    core.drain()
    assert len(core.asked) == MINIMUM_LABELLED


def test_a_recheck_with_too_few_verdicts_leaves_the_calibration_as_it_was(core):
    saved = tone_on_alpha(core)
    core.call("PUT", "/v1/evals/judge-model", 200, json={"model": "openai:/beta"})
    core.failing.add("curt")
    body = {"request_id": request_id(), "name": "tone", "version": "1", "acknowledge_model_cost": True}
    queued = core.call("POST", "/v1/evals/judge-rechecks", 202, json=body)
    core.drain()
    done = core.call("GET", f"/v1/evals/judge-rechecks/{queued['id']}", 200)
    # The judge cannot grade the 11 curt items, so only the 19 polite ones have a verdict.
    assert (done["state"], done["graded"], done["errors"], done["result_id"]) == ("failed", 19, 11, None)
    assert done["error"] == (
        f"Only 19 of the 30 labelled items got a verdict to compare, and {MINIMUM_LABELLED} are needed; "
        "the calibration is unchanged"
    )
    assert status(core, "tone") == {"1": ("unchecked", "model_changed", saved["id"])}


def test_a_recheck_is_refused_before_any_model_call(core):
    body = {"request_id": request_id(), "name": "tone", "version": "1", "acknowledge_model_cost": True}
    registered = {"request_id": request_id(), "name": "tone", "instructions": "Is {{ outputs }} polite?"}
    core.call("POST", "/v1/evals/judges", 201, json=registered)
    missing = core.call("POST", "/v1/evals/judge-rechecks", 404, json=body)
    assert missing["detail"] == "This judge version has no calibration to re-check"

    core.call("PUT", "/v1/evals/judge-model", 200, json={"model": "openai:/alpha"})
    asyncio.run(labelled(core.store, core.records, "tone", "1", model="openai:/alpha"))
    saved = calibrate(core, "tone", "1")
    scorer = ScorerVersion(name="tone", kind="llm_judge", type="instructions", version="1")
    running = CalibrationRecheck(id=str(uuid.uuid4()), project_id=DEFAULT_PROJECT, judge="tone", version="1",
                                 calibration_id=saved["id"], scorer=scorer, definition="d",
                                 state="running")  # fmt: skip
    asyncio.run(core.records.create_recheck(SINGLE_USER, DEFAULT_PROJECT, running))
    busy = core.call("POST", "/v1/evals/judge-rechecks", 409, json={**body, "request_id": request_id()})
    assert busy["detail"] == "A re-check of this judge version is already queued or running"
    assert core.asked == []
    core.call("GET", f"/v1/evals/judge-rechecks/{uuid.uuid4()}", 404)


def test_a_recheck_whose_worker_stopped_is_queued_again_or_failed(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    scorer = ScorerVersion(name="tone", kind="llm_judge", type="instructions", version="1")
    for n, state in enumerate(["running", "running", "completed"]):
        recheck = CalibrationRecheck(id=f"q{n}", project_id=DEFAULT_PROJECT, judge="tone", version="1",
                                     calibration_id="k1", scorer=scorer, definition="d", state=state,
                                     total=30, graded=12)  # fmt: skip
        asyncio.run(records.create_recheck(SINGLE_USER, DEFAULT_PROJECT, recheck))
    work = [Work(kind="recheck", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id="", job_id=f"q{n}")
            for n in range(3)]  # fmt: skip
    assert [entry_id(each) for each in work] == ["recheck:q0", "recheck:q1", "recheck:q2"]
    # The queue's next worker starts a re-check that keeps no checkpoint again from its first item.
    asyncio.run(take_over(records, work[0]))
    asyncio.run(fail_unfinished(records, work[1], "The worker stopped"))
    asyncio.run(take_over(records, work[2]))
    found = [asyncio.run(records.get_recheck(SINGLE_USER, DEFAULT_PROJECT, f"q{n}")) for n in range(3)]
    assert [(r.state, r.graded, r.error) for r in found] == [
        ("queued", 0, ""),
        ("failed", 12, "The worker stopped"),
        ("completed", 12, ""),
    ]
    records.close()


def test_editing_what_a_built_in_judge_means_marks_its_calibration_unchecked(core, monkeypatch):
    core.call("PUT", "/v1/evals/judge-model", 200, json={"model": "openai:/alpha"})
    asyncio.run(labelled(core.store, core.records, "correctness", "v1", model="openai:/alpha"))
    saved = calibrate(core, "correctness", "v1")
    assert status(core, "correctness") == {"v1": ("checked", None, saved["id"])}
    # A release that changes the built-in judge's rubric changes what correctness v1 means.
    monkeypatch.setitem(RUBRICS, "correctness", "Pass if the response is correct, whatever the expected response says.")
    assert status(core, "correctness") == {"v1": ("unchecked", "definition_changed", saved["id"])}

    # The policy judge takes its guidelines from each scoring request: it has no one definition.
    assert calibrate(core, "policy", "v1", 422)["detail"] == NOT_CALIBRATED
    body = {"request_id": request_id(), "name": "policy", "version": "v1", "acknowledge_model_cost": True}
    assert core.call("POST", "/v1/evals/judge-rechecks", 422, json=body)["detail"] == NOT_CALIBRATED


# --- the SDK, with the SDK judge ------------------------------------------------------------------


def polite_provider(path, headers, body):
    """An OpenAI-compatible endpoint whose every model passes a polite response and fails any other."""
    if path != "/v1/chat/completions" or headers.get("authorization") != f"Bearer {KEY}":
        return 404, {"detail": "Not Found"}
    graded = json.loads(body["messages"][1]["content"])
    verdict = "pass" if "polite" in json.dumps(graded) else "fail"
    result = [{"rubric_index": 1, "reason": "Checked.", "verdict": verdict, "evidence_ids": []}]
    content = json.dumps({"result": result})
    return 200, {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    # The process's own address goes nowhere: the judge must call the default judge model's endpoint.
    monkeypatch.setenv("OPENAI_API_BASE", "http://127.0.0.1:9/v1")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    server = Loopback(polite_provider)
    yield server
    server.close()
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


def test_the_sdk_rechecks_a_calibration_in_the_local_data_folder(tmp_path, provider):
    settings = FileJudgeSettings(tmp_path / SETTINGS_FILE)
    endpoint = provider.origin + "/v1"
    asyncio.run(settings.set_default_model(SINGLE_USER, JudgeModel(model="openai:/alpha", endpoint=endpoint)))
    store, records = open_stores(tmp_path)
    tone = ScorerVersion(name="tone", kind="llm_judge", type="instructions",
                         criteria={"name": "tone", "description": "", "instructions": "Is {{ outputs }} polite?",
                                   "kind": "boolean", "options": [], "minimum": 0, "maximum": 1,
                                   "operation_id": request_id()})  # fmt: skip
    asyncio.run(store.register_scorer(SINGLE_USER, DEFAULT_PROJECT, tone))
    asyncio.run(labelled(store, records, "tone", "1", model="openai:/alpha"))
    records.close()
    saved = save_calibration("tone", 1, data_dir=tmp_path)
    asyncio.run(settings.set_default_model(SINGLE_USER, JudgeModel(model="openai:/beta", endpoint=endpoint)))

    with pytest.raises(StoreError) as caught:
        recheck_calibration("tone", 1, data_dir=tmp_path)
    assert caught.value.kind == "invalid" and provider.requests == []

    recheck = recheck_calibration("tone", 1, data_dir=tmp_path, acknowledge_model_cost=True, request_id="sdk-1")
    assert (recheck.state, recheck.graded, recheck.errors, recheck.calibration_id) == (
        "completed",
        MINIMUM_LABELLED,
        0,
        saved.id,
    )
    # One call of the SDK judge per labelled item, on the model the judge runs on now.
    assert len(provider.requests) == MINIMUM_LABELLED
    assert {sent["body"]["model"] for sent in provider.requests} == {"beta"}
    assert recheck_calibration("tone", 1, data_dir=tmp_path, acknowledge_model_cost=True, request_id="sdk-1") == recheck
    assert len(provider.requests) == MINIMUM_LABELLED

    _, records = open_stores(tmp_path)
    newest = asyncio.run(records.list_calibrations(SINGLE_USER, DEFAULT_PROJECT))[0]
    records.close()
    assert (newest.id, newest.recheck_of, newest.model, newest.endpoint) == (
        recheck.result_id,
        saved.id,
        "openai:/beta",
        endpoint,
    )
    assert newest.report["agreement"] == 1.0
