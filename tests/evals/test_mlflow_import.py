"""An MLflow tracking store copied into the local edition: its traces, sessions, datasets,
assessments and evaluation runs read back through the local edition's routes, and a store of
another schema revision refused before anything is written.

The source is a real tracking store that MLflow itself writes, with the mlflow-skinny the SDK
pins, in a process of its own: ``tests/fixtures/mlflow/write_store.py`` says what it holds.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.evals._mlflow import schema_head, schema_history
from martian.evals.local import create_local_app, open_stores
from martian.evals.mlflow_import import (
    PROJECT_LIMIT,
    CopiedProject,
    MlflowSourceError,
    Plan,
    SourceCheck,
    assessment_score,
    copy_mlflow_store,
    import_mlflow_store,
    read_mlflow_store,
    read_only_engine,
    run_uuid,
    source_url,
    verdict,
)
from martian.evals.model import Project
from tests.evals.evals_contract import validate_response

WRITER = Path(__file__).resolve().parents[1] / "fixtures" / "mlflow" / "write_store.py"
POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")


def write_store(folder: Path, url: str | None = None) -> None:
    env = {**os.environ, "MLFLOW_DISABLE_TELEMETRY": "true", "MLFLOW_DISABLE_AGENT_HINT": "1"}
    env.pop("MLFLOW_TRACKING_URI", None)
    env.pop("MLFLOW_ENABLE_WORKSPACES", None)
    env.pop("MLFLOW_WORKSPACE", None)
    command = [sys.executable, str(WRITER), str(folder), *([url] if url else [])]
    subprocess.run(command, cwd=folder.parent, env=env, check=True, capture_output=True, timeout=300)


@pytest.fixture(scope="module")
def written(tmp_path_factory) -> Path:
    """The folder of a tracking store MLflow wrote: ``mlflow.db`` and ``mlruns``."""
    folder = tmp_path_factory.mktemp("mlflow-source") / "store"
    write_store(folder)
    return folder


@pytest.fixture
def source(written, tmp_path) -> Path:
    """A copy of the store's database for one test to change; its traces' artifacts stay where
    MLflow wrote them."""
    database = tmp_path / "source" / "mlflow.db"
    database.parent.mkdir()
    shutil.copyfile(written / "mlflow.db", database)
    return database


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    for name in ("MLFLOW_TRACKING_URI", "MLFLOW_ENABLE_WORKSPACES", "MLFLOW_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_ui(home: Path) -> TestClient:
    ui = home.parent / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return TestClient(create_local_app(home, ui), base_url="http://127.0.0.1:7600")


def change(database: Path, *statements: str) -> None:
    connection = sqlite3.connect(database)
    with connection:
        for statement in statements:
            connection.execute(statement)
    connection.close()


def test_traces_sessions_datasets_annotations_and_runs_open_in_the_local_edition(written, tmp_path):
    database = written / "mlflow.db"
    before = sha256(database)
    home = tmp_path / "evals-home"
    report = import_mlflow_store(str(database), home)
    assert sha256(database) == before  # the source is only read
    assert report.copied == {
        "projects": 1,
        "datasets": 1,
        "traces": 11,
        "scores": 13,
        "annotations": 2,
        "expectations": 7,
        "runs": 2,
        "case results": 6,
    }
    client = local_ui(home)

    # The experiment is a project; MLflow's empty Default experiment adds nothing.
    projects = client.get("/v1/evals/projects").json()["items"]
    assert [p["name"] for p in projects] == ["Default", "support-bot"]
    project = projects[1]["id"]

    # Every trace of the experiment, with its sessions and users.
    traces = client.post("/v1/evals/traces/search", json={"project_id": project, "limit": 100}).json()["items"]
    assert len(traces) == 11
    sessions = client.post("/v1/evals/sessions/search", json={"project_id": project}).json()
    assert {(s["id"], s["turns"]) for s in sessions["items"]} == {("conversation-1", 2), ("conversation-2", 2)}
    by_user = {t["user_id"] for t in traces if t["session_id"] == "conversation-1"}
    assert by_user == {"alice"}
    failed = [t for t in traces if t["state"] == "ERROR"]
    assert len(failed) == 1 and failed[0]["request"] == '{"question": "Why was I charged twice?"}'

    # A trace keeps MLflow's id, its spans, and its assessments: a person's feedback and an
    # expectation as annotations, a judge's feedback as a score.
    turns = {(t["session_id"], t["request"]): t for t in traces if t["session_id"]}
    first = turns["conversation-1", '{"question": "How do I reset my password?"}']
    second = turns["conversation-1", '{"question": "Can I get a refund after 30 days?"}']
    third = turns["conversation-2", '{"question": "Where is my order?"}']
    detail = client.get(f"/v1/evals/traces/{first['id']}", params={"project_id": project}).json()
    assert [(s["name"], s["type"], s["depth"]) for s in detail["spans"]] == [
        ("support_turn", "CHAIN", 0),
        ("support_agent", "AGENT", 1),
        ("search_help_center", "TOOL", 2),
        ("answer", "CHAT_MODEL", 2),
    ]
    assert detail["info"]["trace_id"] == first["id"]
    assert [(a["name"], a["value"], a["source"]) for a in first["assessments"]] == [
        ("helpful", True, {"source_type": "HUMAN", "source_id": "alice@example.com"})
    ]
    # Trace search lists verdicts, so the expectation is on the trace's detail only.
    assert {(a["name"], a["value"], a["source"]["source_type"]) for a in second["assessments"]} == {
        ("correct", "no", "LLM_JUDGE"),
    }
    second_info = client.get(f"/v1/evals/traces/{second['id']}", params={"project_id": project}).json()["info"]
    expected = {a["assessment_name"]: a for a in second_info["assessments"]}["expected_response"]
    assert (expected["expectation"], "feedback" in expected) == (
        {"value": "No. Refunds are available within 30 days of purchase."},
        False,
    )
    # MLflow keeps the feedback a correction overrides; only the correction is copied.
    assert [(a["name"], a["value"], a["rationale"]) for a in third["assessments"]] == [("helpful", True, "It is fine.")]

    # The evaluation dataset is the organisation's, a case per record under the record's id.
    (dataset,) = client.get("/v1/evals/datasets").json()
    assert (dataset["name"], dataset["item_count"], dataset["scope"]) == ("support-questions", 3, "org")
    cases = client.get("/v1/evals/datasets/support-questions/items").json()["items"]
    assert {c["prompt"]: c["expected"] for c in cases} == {
        "How do I reset my password?": "Open Settings, choose Security, then Reset password.",
        "Can I get a refund after 30 days?": "No. Refunds are available within 30 days of purchase.",
        "Where is my order?": "Track your order under Orders in your account.",
    }
    assert all(c["id"].startswith("dr-") and c["usable"] for c in cases)

    # Each evaluation run is a run: its id is MLflow's, each trace a case named by its record.
    runs = client.get("/v1/evals").json()
    validate_response("get", "/v1/evals", runs)
    assert [(r["run_code"], r["state"], r["passed"], r["failed"], r["dataset_name"]) for r in runs] == [
        ("mlflow-candidate", "completed", 2, 1, "support-questions"),
        ("mlflow-baseline", "completed", 1, 2, "support-questions"),
    ]
    candidate, baseline = runs
    detail = client.get(f"/v1/evals/{baseline['id']}").json()
    validate_response("get", "/v1/evals/{run_id}", detail)
    assert {item["dataset_item_id"] for item in detail["items"]} == {c["id"] for c in cases}
    items = {item["prompt"]: item for item in detail["items"]}
    assert {prompt: item["status"] for prompt, item in items.items()} == {
        "How do I reset my password?": "passed",
        "Can I get a refund after 30 days?": "failed",
        "Where is my order?": "failed",
    }
    assert items["Where is my order?"]["output"] == "Your order ships in two days."
    work = client.get(f"/v1/evals/{baseline['id']}/workbench").json()
    scores = {(a["item_id"], a["name"]): a["value"] for a in work["assessments"]}
    order = items["Where is my order?"]["id"]
    assert (scores[(order, "exact_match")], scores[(order, "answer_length")]) == (False, 29)
    # The case's trace is the evaluation's, with its spans and assessments.
    trace = client.get(f"/v1/evals/{baseline['id']}/items/{order}/trace").json()
    assert [s["name"] for s in trace["spans"]] == ["support_agent", "search_help_center", "answer"]
    assert {a["assessment_name"] for a in trace["annotations"]} == {"exact_match", "answer_length", "expected_response"}
    # The run keeps where it came from: MLflow's run, its metrics and its dataset.
    provenance = client.get(f"/v1/evals/{baseline['id']}/provenance").json()["admission"]["mlflow"]
    assert run_uuid(provenance["run_id"]) == baseline["id"]
    assert (provenance["run_name"], provenance["run_type"], provenance["schema_revision"]) == (
        "baseline",
        "genai_evaluate",
        schema_head(),
    )
    assert provenance["metrics"]["exact_match/mean"] == pytest.approx(1 / 3)

    # The two runs evaluated the same dataset, so they compare case by case.
    compared = client.get("/v1/evals/compare", params={"baseline_id": baseline["id"], "candidate_id": candidate["id"]})
    found = compared.json()
    assert (found["compatible"], found["coverage"]["paired"]) == (True, 3)
    changes = {row["prompt"]: row["change"] for row in found["items"]}
    assert changes["Where is my order?"] == "improved"

    # What the store holds that the core has no place for is reported.
    assert {(s.kind, s.name, s.reason) for s in report.plan.skipped} >= {
        ("experiment", "scratch (2)", "deleted in MLflow, with 1 trace"),
        ("experiment", "Default (0)", "no traces or runs"),
        ("run", "prompt-tuning (support-bot)", "no traces; MLflow keeps its 1 parameter and 1 metric"),
    }


def test_copying_again_copies_only_what_is_new(written, tmp_path):
    home = tmp_path / "evals-home"
    import_mlflow_store(str(written / "mlflow.db"), home)
    again = import_mlflow_store(str(written / "mlflow.db"), home)
    assert again.copied == {}
    assert again.present == {"projects": 1, "datasets": 1, "traces": 11, "runs": 2}
    client = local_ui(home)
    assert len(client.get("/v1/evals").json()) == 2
    project = client.get("/v1/evals/projects").json()["items"][1]["id"]
    found = client.post("/v1/evals/traces/search", json={"project_id": project, "limit": 100}).json()["items"]
    assert len(found) == 11


def test_only_the_experiments_named_are_copied(written, tmp_path):
    plan = read_mlflow_store(str(written / "mlflow.db"), experiments=["Default"])
    assert plan.projects == [] and plan.datasets == []
    assert ("experiment", "support-bot (1)", "not selected") in {(s.kind, s.name, s.reason) for s in plan.skipped}
    with pytest.raises(MlflowSourceError, match="has no experiment nonexistent"):
        read_mlflow_store(str(written / "mlflow.db"), experiments=["nonexistent"])


@pytest.mark.parametrize(
    ("statements", "problem"),
    [
        (
            [f"UPDATE alembic_version SET version_num = '{schema_history()[1]}'"],
            f"has MLflow schema revision {schema_history()[1]}, which an older MLflow wrote",
        ),
        (
            ["UPDATE alembic_version SET version_num = 'ffffffffffff'"],
            "has MLflow schema revision ffffffffffff, which mlflow-skinny .* does not know: a newer MLflow wrote it",
        ),
        (["DELETE FROM alembic_version"], "has 0 schema revisions"),
        (["DROP TABLE review_queue_users"], "lacks tables MLflow schema revision"),
        (["UPDATE experiments SET workspace = 'team-a' WHERE experiment_id = 1"], r"MLflow workspaces \(team-a\)"),
        (["DROP TABLE alembic_version"], "is not an MLflow tracking store"),
    ],
)
def test_a_store_of_another_schema_revision_is_refused_before_anything_is_written(
    source, tmp_path, statements, problem
):
    change(source, *statements)
    before = sha256(source)
    home = tmp_path / "evals-home"
    with pytest.raises(MlflowSourceError, match=problem):
        import_mlflow_store(str(source), home)
    # Refused before anything moved: no data folder, and the source unchanged.
    assert not home.exists()
    assert sha256(source) == before


def test_the_source_is_opened_so_that_it_cannot_be_written(source):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    before = sha256(source)
    engine = read_only_engine(f"sqlite:///{source}")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == schema_head()
    with pytest.raises(OperationalError, match="readonly"), engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'x'"))
    engine.dispose()
    assert sha256(source) == before


def test_spans_kept_among_a_traces_artifacts_are_read_from_there(source, tmp_path):
    """Releases before the spans table kept a trace's spans in ``traces.json`` among its
    artifacts. Here one trace's spans are only in that file, in MLflow's form, and another's file
    is gone."""
    connection = sqlite3.connect(source)
    filed, lost = (
        row[0]
        for row in connection.execute(
            "SELECT request_id FROM trace_request_metadata WHERE key = 'mlflow.trace.user' ORDER BY request_id"
        ).fetchmany(2)
    )
    stored = [
        json.loads(row[0])
        for row in connection.execute(
            "SELECT content FROM spans WHERE trace_id = ? ORDER BY start_time_unix_nano", (filed,)
        )
    ]
    connection.close()
    artifacts = tmp_path / "artifacts" / filed
    artifacts.mkdir(parents=True)
    (artifacts / "traces.json").write_text(json.dumps({"spans": stored}), encoding="utf-8")
    change(
        source,
        f"DELETE FROM trace_tags WHERE key = 'mlflow.trace.spansLocation' AND request_id IN ('{filed}', '{lost}')",
        f"DELETE FROM spans WHERE trace_id IN ('{filed}', '{lost}')",
        f"UPDATE trace_tags SET value = '{artifacts.as_uri()}' WHERE key = 'mlflow.artifactLocation' "
        f"AND request_id = '{filed}'",
        f"UPDATE trace_tags SET value = '{(tmp_path / 'gone').as_uri()}' WHERE key = 'mlflow.artifactLocation' "
        f"AND request_id = '{lost}'",
    )
    plan = read_mlflow_store(str(source))
    traces = {t.id: t for p in plan.projects for t in p.traces}
    assert [s.name for s in traces[filed].spans] == ["support_turn", "support_agent", "search_help_center", "answer"]
    assert traces[lost].spans == []
    assert ("trace", lost) in {(s.kind, s.name) for s in plan.skipped}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, (True, True, "")),
        ("yes", ("yes", True, "")),
        ("no", ("no", False, "")),
        ("unknown", ("unknown", None, "")),
        (3, (3, None, "")),
        (0.5, (0.5, None, "")),
        ({"b": 1, "a": [2]}, ('{"a": [2], "b": 1}', None, "")),
        (float("nan"), (None, None, "the value was nan")),
    ],
)
def test_a_feedback_value_is_kept_with_its_verdict(value, expected):
    from mlflow.entities import AssessmentSource, Feedback

    feedback = Feedback(
        name="quality", value=value, source=AssessmentSource(source_type="CODE", source_id="check"), trace_id="tr-1"
    )
    score = assessment_score(feedback, run_id="r")
    assert (score.value, score.passed, score.error) == expected
    assert (score.run_id, score.source, score.source_id) == ("r", "code", "check")
    assert "martian.annotation" not in score.metadata
    assert verdict(score.value) == expected[1]


def test_a_person_s_feedback_is_an_annotation():
    from mlflow.entities import AssessmentSource, Feedback

    feedback = Feedback(
        name="helpful", value=True, source=AssessmentSource(source_type="HUMAN", source_id="alice"), trace_id="tr-1"
    )
    score = assessment_score(feedback)
    assert (score.source, score.source_id, score.metadata["martian.annotation"]) == ("human", "alice", "feedback")


@pytest.mark.parametrize(
    ("given", "problem"),
    [
        ("http://127.0.0.1:5000", "copying over its REST API is not supported"),
        ("https://user:secret@mlflow.example.com", r"^https://user:\*\*\*@mlflow\.example\.com is an MLflow server"),
        ("databricks://workspace", "copying over its REST API is not supported"),
        ("mysql://user:secret@host/db", r"^mysql://user:\*\*\*@host/db: give a SQLite database file"),
    ],
)
def test_sources_that_are_not_a_database_are_refused(given, problem):
    with pytest.raises(MlflowSourceError, match=problem):
        source_url(given)


def test_source_paths_and_urls(tmp_path):
    with pytest.raises(MlflowSourceError, match="is a folder"):
        source_url(str(tmp_path))
    with pytest.raises(MlflowSourceError, match="there is no file"):
        source_url(str(tmp_path / "missing.db"))
    (tmp_path / "mlflow.db").write_bytes(b"")
    assert source_url(str(tmp_path / "mlflow.db")) == f"sqlite:///{tmp_path / 'mlflow.db'}"
    assert source_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert source_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    (tmp_path / "other.db").write_bytes(b"not sqlite at all")
    with pytest.raises(MlflowSourceError, match="cannot read"):
        read_mlflow_store(str(tmp_path / "other.db"))


def test_the_organisation_project_limit_is_checked_before_anything_is_written(tmp_path):
    store, records = open_stores(tmp_path / "evals-home")
    try:
        check = SourceCheck(source="sqlite://", dialect="sqlite", revision="r", head="r", mlflow="m")
        projects = [
            CopiedProject(project=Project(id=str(uuid.uuid4()), name=f"e{n}"), experiment_id=str(n), traces=[], runs=[])
            for n in range(PROJECT_LIMIT + 1)
        ]
        with pytest.raises(MlflowSourceError, match=f"at most {PROJECT_LIMIT} fit"):
            copy_mlflow_store(Plan(check=check, projects=projects), store, records)
    finally:
        records.close()
    assert [p["name"] for p in local_ui(tmp_path / "evals-home").get("/v1/evals/projects").json()["items"]] == [
        "Default"
    ]


def test_the_command_reports_what_was_copied_and_what_was_not(written, source, tmp_path):
    folder = tmp_path / "elsewhere"
    runner = CliRunner()
    dry = runner.invoke(
        app, ["evals", "import", "mlflow", str(written / "mlflow.db"), "--data-dir", str(folder), "--dry-run"]
    )
    assert dry.exit_code == 0, dry.output
    assert "To copy (nothing was written, --dry-run):" in dry.output and not folder.exists()

    result = runner.invoke(app, ["evals", "import", "mlflow", str(written / "mlflow.db"), "--data-dir", str(folder)])
    assert result.exit_code == 0, result.output
    # MLflow logs as it creates the data folder's store; the report is the rest.
    lines = [line for line in result.output.splitlines() if " INFO mlflow." not in line]
    assert lines[0].endswith(f"MLflow schema revision {schema_head()}, the one mlflow-skinny {version()} reads.")
    assert lines[1:7] == [
        "Copied:",
        "  experiment 1 as project support-bot: 11 traces, 2 runs",
        "    run mlflow-baseline (completed): 3 cases: 1 passed, 2 failed, 0 errors, 0 without a verdict",
        "    run mlflow-candidate (completed): 3 cases: 2 passed, 1 failed, 0 errors, 0 without a verdict",
        "  dataset support-questions: 3 cases, version 1",
        "  New: 1 project, 1 dataset, 11 traces, 13 scores, 2 annotations, 7 expectations, 2 runs, 6 case results",
    ]
    assert lines[7:11] == [
        "Not copied:",
        "  experiment scratch (2): deleted in MLflow, with 1 trace",
        "  experiment Default (0): no traces or runs",
        "  run prompt-tuning (support-bot): no traces; MLflow keeps its 1 parameter and 1 metric",
    ]
    assert lines[11].startswith("  assessment helpful on tr-") and lines[11].endswith(
        ": a later assessment overrides it"
    )
    assert lines[-1] == f"Open them in the local Evals UI: martian ui --data-dir {folder}"
    assert not (tmp_path / "evals-home").exists()

    change(source, f"UPDATE alembic_version SET version_num = '{schema_history()[1]}'")
    refused = runner.invoke(app, ["evals", "import", "mlflow", str(source), "--data-dir", str(tmp_path / "other")])
    assert refused.exit_code == 2
    assert "Import failed:" in refused.output and "which an older MLflow wrote" in refused.output
    assert "Nothing was copied" in refused.output and not (tmp_path / "other").exists()


def version() -> str:
    from importlib.metadata import version as installed

    return installed("mlflow-skinny")


@pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to copy from a store on Postgres")
def test_a_store_on_postgres_is_copied_the_same_way(tmp_path):
    from sqlalchemy import create_engine, text

    from martian.evals._sql import create_schema, postgres_url

    assert POSTGRES_URL is not None
    schema = f"mlflow_source_{uuid.uuid4().hex[:10]}"
    create_schema(POSTGRES_URL, schema)
    url = postgres_url(POSTGRES_URL, schema)
    try:
        write_store(tmp_path / "store", url)
        report = import_mlflow_store(url, tmp_path / "evals-home")
        assert report.plan.check.dialect == "postgresql"
        assert [(r.code, r.counts.passed, r.counts.failed) for r in report.runs] == [
            ("mlflow-baseline", 1, 2),
            ("mlflow-candidate", 2, 1),
        ]
        assert report.copied["traces"] == 11
        # The source's own connection settings, here its schema, are kept; only reads are allowed.
        engine = read_only_engine(source_url(url))
        with pytest.raises(Exception, match="read-only transaction"), engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = 'x'"))
        engine.dispose()
    finally:
        engine = create_engine(POSTGRES_URL)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
