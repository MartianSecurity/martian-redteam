"""Each judge runs on its own model, or on the default judge model when it names none, and every
judge score records the model and version that produced it. The default is an organisation's on
the server and a local setting in the local edition. A fake judge answers; nothing calls a model."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from martian.evals.judge_settings import FileJudgeSettings, JudgeModel, MemoryJudgeSettings
from martian.evals.local import SETTINGS_FILE, create_local_app
from martian.evals.model import ScorerVersion, Suite, digest, suite_digest
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import ENTERPRISE, SINGLE_USER, create_app, organisation_principal
from martian.evals.store import MemoryStore
from martian.evals.worker import JudgeRequest, JudgeResult, Verdict
from tests.evals.evals_contract import validate_request, validate_response
from tests.evals.write_fixture import Served, judge, request_id, serve

ADMIN = organisation_principal("admin-1", "org-a", frozenset({"admin"}))
MEMBER = organisation_principal("member-1", "org-a", frozenset({"member"}))
OTHER_ORG = organisation_principal("admin-2", "org-b", frozenset({"admin"}))
GATEWAY = "http://localhost:4000/v1"


@pytest.fixture
def records(tmp_path):
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


class Recording:
    """The write fixture's judge, keeping every request it was given."""

    def __init__(self) -> None:
        self.requests: list[JudgeRequest] = []

    async def __call__(self, request: JudgeRequest) -> JudgeResult:
        self.requests.append(request)
        return await judge(request)


@pytest.fixture
def recording() -> Recording:
    return Recording()


@pytest.fixture
def org(records, recording):
    """An organisation's core, its admin making the requests, with a judge that records them."""
    client, served = serve(
        MemoryStore(), records, entitlements=lambda _p: ENTERPRISE, principal=ADMIN, judge_with=recording
    )
    with client:
        yield served


def register(core: Served, name: str, **model: Any) -> dict[str, Any]:
    body = {"request_id": request_id(), "name": name, "instructions": f"Is {{{{ outputs }}}} {name}?", **model}
    validate_request("JudgeVersionRequest", body)
    return core.call("POST", "/v1/evals/judges", 201, json=body)


def score(core: Served, run: dict[str, Any], **criteria: Any) -> list[dict[str, Any]]:
    core.call(
        "POST",
        f"/v1/evals/{run['id']}/scores",
        202,
        json={"request_id": request_id(), "acknowledge_model_cost": True, **criteria},
    )
    core.drain()
    return core.call("GET", f"/v1/evals/{run['id']}/workbench", 200)["assessments"]


def test_two_judges_in_one_organisation_run_on_their_own_models(org: Served, recording: Recording):
    tone = register(org, "tone", model="openai:/alpha", endpoint=GATEWAY)
    brevity = register(org, "brevity", model="anthropic:/beta")
    assert (tone["definition"]["model"], tone["definition"]["endpoint"]) == ("openai:/alpha", GATEWAY)
    assert (brevity["definition"]["model"], brevity["definition"]["endpoint"]) == ("anthropic:/beta", None)
    listed = {j["name"]: j["definition"] for j in org.call("GET", "/v1/evals/judges", 200)["items"]}
    assert (listed["tone"]["model"], listed["brevity"]["model"]) == ("openai:/alpha", "anthropic:/beta")
    # The built-in correctness judge names no model, so it runs on the organisation's default.
    org.call("PUT", "/v1/evals/judge-model", 200, json={"model": "openai:/gamma"})

    run = org.completed_run()
    assessments = score(
        org,
        run,
        scorers=["correctness", "exact_match"],
        judges=[{"name": "tone", "version": 1}, {"name": "brevity", "version": 1}],
    )

    # One request per model for each case with an output, each with the judges pinned to it.
    asked = {(r.model, r.endpoint, tuple(s.name for s in r.scorers)) for r in recording.requests}
    assert asked == {
        ("anthropic:/beta", None, ("brevity",)),
        ("openai:/gamma", None, ("correctness",)),
        ("openai:/alpha", GATEWAY, ("tone",)),
    }
    assert len(recording.requests) == 3 * 2  # faq-3 has no expected text, so its case was skipped
    expected = {"tone": "openai:/alpha", "brevity": "anthropic:/beta", "correctness": "openai:/gamma"}
    judged = [a for a in assessments if a["source"] == "LLM_JUDGE"]
    assert {a["name"] for a in judged} == set(expected)
    for found in judged:
        model = expected[found["name"]]
        if found["error"]:  # the skipped case has no output to judge, and names the model it was pinned to
            assert (found["error"], found["provenance"]["martian.judge.model"]) == (
                "No successful saved target output",
                model,
            )
            assert "martian.judge.model_version" not in found["provenance"]
            continue
        assert found["source_id"] == f"{model}@2026-11"
        assert found["provenance"]["martian.judge.model"] == model
        assert found["provenance"]["martian.judge.model_version"] == "2026-11"
        assert found["provenance"].get("martian.judge.endpoint") == (GATEWAY if found["name"] == "tone" else None)
    code = [a for a in assessments if a["name"] == "exact_match"]
    assert code and not any(key.startswith("martian.judge.") for a in code for key in a["provenance"])

    # Each score keeps its model on its trace too.
    trace = next(item["evals_trace_id"] for item in run["items"] if item["dataset_item_id"] == "faq-1")
    on_trace = org.call("GET", f"/v1/evals/traces/{trace}", 200)["info"]["assessments"]
    sources = {
        a["assessment_name"]: a["source"]["source_id"] for a in on_trace if a["source"]["source_type"] == "LLM_JUDGE"
    }
    assert sources == {name: f"{model}@2026-11" for name, model in expected.items()}


def test_a_job_keeps_the_default_it_was_admitted_with(org: Served, recording: Recording):
    run = org.completed_run()
    first = score(org, run, scorers=["safety"])
    org.call("PUT", "/v1/evals/judge-model", 200, json={"model": "openai:/alpha", "endpoint": GATEWAY})
    second = score(org, run, scorers=["safety"])

    # With no default, the judge used its own model, which reports no version.
    before = {a["source_id"] for a in first if not a["error"]}
    after = {a["source_id"] for a in second if not a["error"] and a not in first}
    assert before == {"fake-judge@1"} and after == {"openai:/alpha@2026-11"}
    jobs = org.call("GET", f"/v1/evals/{run['id']}/workbench", 200)["jobs"]
    pinned = {job["config"]["definition_digest"] for job in jobs}
    assert len(pinned) == 2  # different models never compare as the same scoring
    assert [r.model for r in recording.requests] == [None, None, "openai:/alpha", "openai:/alpha"]


def test_a_judge_that_answers_from_another_model_fails_its_scores(records):
    async def stubborn(request: JudgeRequest) -> JudgeResult:
        return JudgeResult(model="openai:/other", verdicts={s.name: Verdict(True) for s in request.scorers})

    client, core = serve(
        MemoryStore(), records, entitlements=lambda _p: ENTERPRISE, principal=ADMIN, judge_with=stubborn
    )
    with client:
        register(core, "tone", model="openai:/alpha")
        run = core.completed_run()
        found = [a for a in score(core, run, judges=[{"name": "tone", "version": 1}]) if a["name"] == "tone"]
    answered = [a for a in found if a["error"] != "No successful saved target output"]
    assert answered and all(a["value"] is None for a in answered)
    assert {a["error"] for a in answered} == {
        "The judge answered with openai:/other, not openai:/alpha, the model this judge is pinned to"
    }
    assert {a["provenance"]["martian.judge.model"] for a in answered} == {"openai:/alpha"}


@pytest.mark.parametrize(
    "model",
    [
        {"model": "gpt-4o-mini"},  # not a model URI
        {"endpoint": GATEWAY},  # an endpoint serves a model
        {"model": "anthropic:/beta", "endpoint": GATEWAY},  # an endpoint is OpenAI-compatible
        {"model": "openai:/alpha", "endpoint": "http://user:secret@localhost:4000/v1"},
        {"model": "openai:/alpha", "endpoint": "http://localhost:4000/v1?api_key=secret"},
        {"model": "openai:/alpha", "endpoint": "http://localhost:4000/v1#secret"},
        {"model": "openai:/alpha", "endpoint": "http://localhost:4000/v1/"},
        {"model": "openai:/alpha", "endpoint": "ftp://localhost/v1"},
        {"model": "openai:/alpha", "api_key": "secret"},
    ],
)
def test_a_judge_model_never_carries_a_key_and_is_one_form(org: Served, model: dict[str, Any]):
    body = {"request_id": request_id(), "name": "tone", "instructions": "Is {{ outputs }} polite?", **model}
    org.call("POST", "/v1/evals/judges", 422, json=body)
    org.call("PUT", "/v1/evals/judge-model", 422, json=model)
    assert org.call("GET", "/v1/evals/judges", 200)["items"] == []


def test_registering_the_same_judge_again_keeps_its_model(org: Served):
    body = {"request_id": request_id(), "name": "tone", "instructions": "Is {{ outputs }} polite?"}
    first = org.call("POST", "/v1/evals/judges", 201, json={**body, "model": "openai:/alpha"})
    assert org.call("POST", "/v1/evals/judges", 201, json={**body, "model": "openai:/alpha"}) == first
    org.call("POST", "/v1/evals/judges", 409, json={**body, "model": "openai:/beta"})
    revised = org.call(
        "POST", "/v1/evals/judges", 201, json={**body, "request_id": request_id(), "expected_version": 1}
    )
    assert (revised["version"], revised["definition"]["model"]) == (2, None)


def test_the_default_judge_model_is_each_organisations_and_set_by_an_admin(org: Served):
    nothing = org.call("GET", "/v1/evals/judge-model", 200)
    validate_response("get", "/v1/evals/judge-model", nothing)
    assert nothing == {"model": None, "endpoint": None, "selectable": True}
    body = {"model": "openai:/alpha", "endpoint": GATEWAY}
    validate_request("JudgeModelRequest", body)
    saved = org.call("PUT", "/v1/evals/judge-model", 200, json=body)
    validate_response("put", "/v1/evals/judge-model", saved)
    assert saved == {**body, "selectable": True}

    org.act_as(MEMBER)
    assert org.call("GET", "/v1/evals/judge-model", 200) == saved
    org.call("PUT", "/v1/evals/judge-model", 403, json={})
    org.act_as(OTHER_ORG)
    assert org.call("GET", "/v1/evals/judge-model", 200)["model"] is None
    org.act_as(ADMIN)
    assert org.call("PUT", "/v1/evals/judge-model", 200, json={}) == nothing


def test_without_judge_settings_there_is_no_default_to_set(records):
    client = TestClient(create_app(store=MemoryStore(), records=records, principal=lambda: SINGLE_USER))
    assert client.get("/v1/evals/judge-model").json() == {"model": None, "endpoint": None, "selectable": True}
    refused = client.put("/v1/evals/judge-model", json={"model": "openai:/alpha"})
    assert (refused.status_code, refused.json()) == (503, {"detail": "This installation keeps no default judge model"})


def test_the_local_edition_keeps_its_default_in_the_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    home, ui = tmp_path / "home", tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    local = "http://127.0.0.1:7600"
    first = TestClient(create_local_app(home, ui), base_url=local)
    assert first.put("/v1/evals/judge-model", json={"model": "openai:/alpha", "endpoint": GATEWAY}).status_code == 200
    settings = json.loads((home / SETTINGS_FILE).read_text("utf-8"))
    assert settings == {"judge_model": {"model": "openai:/alpha", "endpoint": GATEWAY}}

    again = TestClient(create_local_app(home, ui), base_url=local)
    assert again.get("/v1/evals/judge-model").json() == {
        "model": "openai:/alpha",
        "endpoint": GATEWAY,
        "selectable": True,
    }
    assert again.put("/v1/evals/judge-model", json={}).status_code == 200
    assert json.loads((home / SETTINGS_FILE).read_text("utf-8")) == {}


async def test_file_settings_belong_to_the_single_local_user(tmp_path):
    settings = FileJudgeSettings(tmp_path / SETTINGS_FILE)
    assert (await settings.default_model(SINGLE_USER)) == JudgeModel()
    with pytest.raises(ValueError, match="single local user"):
        await settings.default_model(ADMIN)
    memory = MemoryJudgeSettings()
    await memory.set_default_model(ADMIN, JudgeModel(model="openai:/alpha"))
    assert (await memory.default_model(OTHER_ORG), await memory.default_model(SINGLE_USER)) == (
        JudgeModel(),
        JudgeModel(),
    )


def test_a_scorer_without_an_endpoint_keeps_the_digest_it_had():
    scorer = ScorerVersion(name="correctness", kind="llm_judge", type="correctness", version="v1")
    assert "endpoint" not in scorer.model_dump(mode="json")
    before = {"name": "correctness", "kind": "llm_judge", "type": "correctness", "version": "v1", "model": None}
    suite = Suite(id="s", name="Support", scorers=[scorer], digest="0" * 64)
    written = suite.model_dump(mode="json", exclude={"id", "version", "digest", "created_at", "created_by"})
    assert suite_digest(suite) == digest({**written, "scorers": [{**before, "parameters": {}, "criteria": {}}]})
    pinned = scorer.model_copy(update={"model": "openai:/alpha", "endpoint": GATEWAY})
    assert ScorerVersion.model_validate(pinned.model_dump(mode="json")) == pinned
