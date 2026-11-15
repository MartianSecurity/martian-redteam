"""Prompt versions: the core's own prompt routes, `martian evals prompts push` from a suite file's
matrix, and the prompt version a run records it used, matched by its text's digest. No model call:
the matrix example's function target stands in for the model."""

import shutil
from pathlib import Path

import mlflow
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.evals.local import create_local_app
from martian.evals.model import Run, prompt_digest
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import SINGLE_USER, create_app, organisation_principal
from martian.evals.state import STATE
from martian.evals.store import MemoryStore
from tests.evals.evals_contract import validate_response

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "evals" / "matrix"
TERSE = "Answer in one sentence."
FRIENDLY = "Greet the customer, then answer in one sentence."
LOCAL_USER = "bf0808be-f788-5dd7-8fc2-2d28ed350045"  # the API's id of the single local user


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    # Runs and prompts are kept in the local edition's data folder; never the real one in a test.
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


@pytest.fixture
def core(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    store = MemoryStore()
    principal = {"current": SINGLE_USER}
    with TestClient(create_app(store=store, records=records, principal=lambda: principal["current"])) as client:
        yield client, store, records, principal
    records.close()


def example(folder: Path) -> Path:
    """A copy of examples/evals/matrix: two models and the prompts terse and friendly."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("evals.yaml", "cases.jsonl", "agent.py"):
        shutil.copy(EXAMPLE / name, folder / name)
    return folder / "evals.yaml"


def cli(*args: str):
    STATE.reset()
    return CliRunner().invoke(app, ["evals", *args], env={"COLUMNS": "200"})


def local_ui(folder: Path) -> TestClient:
    ui = folder / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return TestClient(create_local_app(folder / "evals-home", ui), base_url="http://127.0.0.1:7600")


def test_a_prompt_keeps_each_version_with_its_text_message_author_and_time(core):
    client, _, _, _ = core
    created = client.post("/v1/evals/prompts", json={"name": "support", "description": "Support replies"})
    assert created.status_code == 201
    assert created.json() | {"created_at": None} == {
        "name": "support",
        "description": "Support replies",
        "created_at": None,
        "created_by": LOCAL_USER,
        "version_count": 0,
        "latest": None,
        "run_count": 0,
    }
    assert (
        client.post("/v1/evals/prompts", json={"name": "support", "description": "Support replies"}).status_code == 201
    )
    assert client.post("/v1/evals/prompts", json={"name": "support", "description": "Other"}).status_code == 409

    first = client.post(
        "/v1/evals/prompts/support/versions", json={"text": TERSE, "message": "First", "base_version": 0}
    )
    assert first.status_code == 201 and first.json()["added"] is True
    version = first.json()["version"]
    assert (version["version"], version["text"], version["message"], version["created_by"]) == (
        1,
        TERSE,
        "First",
        LOCAL_USER,
    )
    assert version["digest"] == prompt_digest(TERSE) and version["created_at"]
    # The same text again adds nothing: the latest version is answered, whatever it was based on.
    same = client.post("/v1/evals/prompts/support/versions", json={"text": TERSE, "base_version": 0})
    assert (same.status_code, same.json()["added"], same.json()["version"]["version"]) == (200, False, 1)
    second = client.post("/v1/evals/prompts/support/versions", json={"text": FRIENDLY, "base_version": 1})
    assert (second.status_code, second.json()["version"]["version"], second.json()["version"]["message"]) == (
        201,
        2,
        "",
    )
    # A change made from an older version than the latest is refused rather than added blindly.
    stale = client.post("/v1/evals/prompts/support/versions", json={"text": "Be brief.", "base_version": 1})
    assert stale.status_code == 409 and "version 2" in stale.json()["detail"]

    listed = client.get("/v1/evals/prompts").json()["items"]
    assert [(p["name"], p["version_count"], p["latest"]["version"]) for p in listed] == [("support", 2, 2)]
    assert "text" not in listed[0]["latest"]
    detail = client.get("/v1/evals/prompts/support").json()
    assert [(v["version"], v["text"], v["runs"]) for v in detail["versions"]] == [(1, TERSE, []), (2, FRIENDLY, [])]

    assert client.get("/v1/evals/prompts/missing").status_code == 404
    assert client.post("/v1/evals/prompts/missing/versions", json={"text": TERSE}).status_code == 404
    assert client.post("/v1/evals/prompts", json={"name": "two words"}).status_code == 422
    assert client.post("/v1/evals/prompts/support/versions", json={"text": ""}).status_code == 422
    assert client.post("/v1/evals/prompts/support/versions", json={"text": TERSE, "author": "x"}).status_code == 422


def test_a_viewer_reads_prompts_and_cannot_change_them(core):
    client, store, _, principal = core
    admin = organisation_principal("admin-1", "org-a", frozenset({"admin"}))
    principal["current"] = admin
    client.post("/v1/evals/prompts", json={"name": "support"})
    client.post("/v1/evals/prompts/support/versions", json={"text": TERSE})
    principal["current"] = organisation_principal("viewer-1", "org-a", frozenset({"viewer"}))
    assert [p["name"] for p in client.get("/v1/evals/prompts").json()["items"]] == ["support"]
    assert client.post("/v1/evals/prompts", json={"name": "other"}).status_code == 403
    assert client.post("/v1/evals/prompts/support/versions", json={"text": FRIENDLY}).status_code == 403
    # The local user is not the organisation, and sees none of its prompts.
    principal["current"] = SINGLE_USER
    assert client.get("/v1/evals/prompts").json() == {"items": []}


async def test_a_version_lists_the_runs_whose_provenance_names_it(core):
    client, store, records, _ = core
    client.post("/v1/evals/prompts", json={"name": "terse"})
    client.post("/v1/evals/prompts/terse/versions", json={"text": TERSE})
    client.post("/v1/evals/prompts/terse/versions", json={"text": "Answer briefly."})
    used = {"axis": "prompt", "value": "terse", "name": "terse", "digest": prompt_digest(TERSE)}
    for number, (version, code) in enumerate([(1, "evals-1"), (2, "evals-2"), (1, "evals-3"), (None, "other-1")]):
        provenance = {"prompts": [{**used, "version": version}]} if version else {}
        run = Run(
            id=f"00000000-0000-4000-8000-00000000000{number}",
            project_id="default",
            code=code,
            source="local",
            state="completed",
            provenance=provenance,
            created_at=f"2026-11-15T10:0{number}:00Z",
        )
        await records.create_run(SINGLE_USER, run)
    detail = client.get("/v1/evals/prompts/terse").json()
    runs = {v["version"]: [run["run_code"] for run in v["runs"]] for v in detail["versions"]}
    assert runs == {1: ["evals-3", "evals-1"], 2: ["evals-2"]}  # newest first
    assert detail["versions"][0]["runs"][0]["prompt"] == {"axis": "prompt", "value": "terse"}
    validate_response(
        "get", "/v1/evals", [{k: v for k, v in run.items() if k != "prompt"} for run in detail["versions"][0]["runs"]]
    )
    assert detail["run_count"] == 3
    assert client.get("/v1/evals/prompts").json()["items"][0]["run_count"] == 3


def test_a_matrix_run_records_the_prompt_version_each_combination_used(tmp_path):
    suite = example(tmp_path / "suite")
    # Before the push, no run names a version: a run is matched when it is recorded.
    assert cli("run", str(suite), "--output", str(tmp_path / "before")).exit_code == 1
    pushed = cli("prompts", "push", str(suite), "-m", "The example's prompts")
    assert pushed.exit_code == 0, pushed.output
    assert pushed.output.splitlines() == ["terse: version 1 added", "friendly: version 1 added"]
    again = cli("prompts", "push", str(suite))
    assert again.output.splitlines() == ["terse: version 1 unchanged", "friendly: version 1 unchanged"]

    # A new text of the terse prompt is its version 2; the friendly prompt stays at version 1.
    text = suite.read_text(encoding="utf-8").replace(f'terse: "{TERSE}"', 'terse: "Answer in one short sentence."')
    suite.write_text(text, encoding="utf-8")
    edited = cli("prompts", "push", str(suite), "--value", "terse", "-m", "Shorter")
    assert edited.output.splitlines() == ["terse: version 2 added"]
    assert cli("run", str(suite), "--output", str(tmp_path / "after")).exit_code == 1

    client = local_ui(tmp_path)
    runs = client.get("/v1/evals").json()
    validate_response("get", "/v1/evals", runs)
    by_code = {run["run_code"]: run for run in runs}
    assert "prompts" not in by_code["evals[model=small-model,prompt=terse]-1"]["provenance"]
    detail = client.get(f"/v1/evals/{by_code['evals[model=small-model,prompt=terse]-2']['id']}").json()
    validate_response("get", "/v1/evals/{run_id}", detail)
    assert detail["provenance"]["prompts"] == [
        {
            "axis": "prompt",
            "value": "terse",
            "name": "terse",
            "version": 2,
            "digest": prompt_digest("Answer in one short sentence."),
        }
    ]
    friendly = by_code["evals[model=large-model,prompt=friendly]-2"]["provenance"]["prompts"]
    assert [(p["name"], p["version"]) for p in friendly] == [("friendly", 1)]

    # The Prompts routes of `martian ui` show the versions, their messages and the runs of each.
    terse = client.get("/v1/evals/prompts/terse").json()
    assert [(v["version"], v["message"]) for v in terse["versions"]] == [(1, "The example's prompts"), (2, "Shorter")]
    assert terse["versions"][0]["runs"] == []
    assert sorted(run["run_code"] for run in terse["versions"][1]["runs"]) == [
        "evals[model=large-model,prompt=terse]-2",
        "evals[model=small-model,prompt=terse]-2",
    ]


@pytest.mark.parametrize(
    ("args", "edit", "problem"),
    [
        (["--axis", "tone"], None, "the suite file's matrix has no axis 'tone' (its axes: model, prompt)"),
        (["--value", "formal"], None, "matrix.prompt has no value named 'formal'"),
        (
            ["--axis", "model"],
            ("[small-model, large-model]", "[1, 2]"),
            "matrix.model.1 is not text, so it is not a prompt",
        ),
        ([], ("    terse:", "    v1:terse:"), "matrix.prompt: 'v1:terse' cannot name a prompt"),
    ],
)
def test_push_refuses_what_is_not_a_prompt_of_the_suite_file(tmp_path, args, edit, problem):
    suite = example(tmp_path / "suite")
    if edit is not None:
        suite.write_text(suite.read_text(encoding="utf-8").replace(*edit), encoding="utf-8")
    result = cli("prompts", "push", str(suite), *args)
    assert result.exit_code == 2
    assert problem in result.output
    # Nothing was pushed.
    assert local_ui(tmp_path).get("/v1/evals/prompts").json() == {"items": []}


def test_a_run_s_readers_see_its_matrix_prompt_versions_and_cache_and_nothing_else_of_them():
    from martian.evals.server.projection import reader_provenance

    used = {"axis": "prompt", "value": "terse", "name": "support.terse", "version": 2, "digest": "d" * 64}
    matrix = {"id": "prompt=terse", "values": {"prompt": "terse"}, "axes": [], "invocation": "i-1"}
    cache = {"folder": "/data/cache", "ttl_seconds": 60.0, "functions": False, "target": None, "judge": None}
    kept = {
        "matrix": {**matrix, "pinned": "kept for the server"},
        "prompts": [{**used, "text": "Answer in one sentence."}, "not an object"],
        "cache": {**cache, "entries": ["kept for the server"]},
    }
    # Each prompt version of the list shows the keys the allowlist gives, as an object's keys do.
    assert reader_provenance(kept) == {"matrix": matrix, "prompts": [used], "cache": cache}
