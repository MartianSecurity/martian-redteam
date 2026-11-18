"""Pushing local runs to a server: a local edition's data folder pushed to a second evals core that
stands in for the server, both in this process with stores of their own, the server's on SQLite and
on Postgres when MARTIAN_TEST_POSTGRES_URL names a database the tests may create schemas in.

The data folder holds runs of every kind the local edition records: suite-file runs with repeated
attempts and with conversations, Inspect AI logs, a console run with scored traces, a scoring job,
reviews and a review queue, a run in a project of its own with a trace no case refers to, a queued
run and live traffic. A pushed run must be on the server as it is here (its run, case results,
scores, scoring jobs and traces, but for the identifiers the server's store assigns and the trace
ids it maps), and shown by the server's routes as the local UI shows it. Pushing twice, or every
request twice, adds nothing; an interrupted push continues where it stopped; and two organisations
pushing the same traces each get their own."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from collections.abc import Iterator
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import httpx
import mlflow
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from martian.cli import app as cli
from martian.evals import push as push_module
from martian.evals.client import Backend, BackendError
from martian.evals.data_export import FORMAT, VERSION, export_line, export_local, run_records, score_key
from martian.evals.inspect_import import import_inspect_logs
from martian.evals.local import create_local_app, open_stores
from martian.evals.model import CaseResult, Counts, Project, Run, Score, ScoringJob, StoreRef, Trace, TraceQuery
from martian.evals.push import PushError, PushSummary, push_local
from martian.evals.records import RecordStore
from martian.evals.records.sql import SqlRecordStore
from martian.evals.runner import run_suite
from martian.evals.server import SINGLE_USER, create_app
from martian.evals.server.principal import Principal, organisation_principal
from martian.evals.server.push import pushed_trace_id
from martian.evals.state import STATE
from martian.evals.store import EvalStore, StoreError
from martian.evals.store.mlflow_local import MlflowStore
from tests.evals.test_data_export import INSPECT, T0, populate

POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")
DATABASES = [
    "sqlite",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to run on Postgres"),
    ),
]
SERVER = "http://server.test"

ADMIN_A = organisation_principal("admin-a", "org-a", frozenset({"admin"}), organisation_name="Org A")
MEMBER_A = organisation_principal("member-a", "org-a", frozenset({"member"}), organisation_name="Org A")
ADMIN_B = organisation_principal("admin-b", "org-b", frozenset({"admin"}), organisation_name="Org B")
VIEWER_A = organisation_principal("viewer-a", "org-a", frozenset({"viewer"}))
KEYS = {"mce_admin_a": ADMIN_A, "mce_member_a": MEMBER_A, "mce_admin_b": ADMIN_B, "mce_viewer_a": VIEWER_A}
"""The stand-in server's organisation API keys, as the Martian server's keys sign machines in."""

STORE_IDS = {"experiment_id", "run_id", "assessment_id"}
"""``StoreRef`` fields the receiving store assigns itself, as an import's receiving store does."""
ROUTE_STORE_IDS = {"assessment_id", "evals_assessment_id", "native_id"}
"""Keys of the run routes' answers that carry the store's own identifiers."""
LINE_FILES = ("runs.jsonl", "case_results.jsonl", "scores.jsonl", "scoring_jobs.jsonl", "traces.jsonl")
"""The export's files a push carries lines of."""


# --- the data folder: one of each kind of run the local edition records ------------------------

REPEATS_AGENT = 'def target(message):\n    return "I cannot share it."\n'
CONVERSATION_AGENT = 'def reply(message, session_id):\n    return "You said " + message\n'


def suite_run(folder: Path, home: Path, *, agent: str, cases: list[dict[str, Any]], extra: str, checks: str) -> None:
    folder.mkdir()
    (folder / "agent.py").write_text(agent, encoding="utf-8")
    (folder / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    (folder / "evals.yaml").write_text(f"data: cases.jsonl\n{extra}checks:\n{checks}", encoding="utf-8")
    run_suite(folder / "evals.yaml", home=home)


async def side_project(store: EvalStore, records: RecordStore) -> None:
    """A finished run in a project of its own, with a trace no case refers to."""
    project = await store.create_project(SINGLE_USER, Project(id=str(uuid.uuid4()), name="Side work"))
    run = Run(
        id=str(uuid.uuid4()),
        project_id=project.id,
        code="side-1",
        source="console",
        state="running",
        dataset_name="side",
        counts=Counts(cases=1),
        created_at=T0 + timedelta(days=5),
        started_at=T0 + timedelta(days=5),
    )
    native = await store.start_run(SINGLE_USER, run)
    run = await records.create_run(
        SINGLE_USER,
        run.model_copy(
            update={"store": StoreRef(experiment_id=native.store.experiment_id, run_id=native.store.run_id)}
        ),
    )
    case_trace, loose = "tr-" + uuid.uuid4().hex, "tr-" + uuid.uuid4().hex
    verdict = Score(run_id=run.id, case_id="s1", trace_id=case_trace, scorer="exact_match", source="code", value=True)
    saved = await store.record_trace(
        SINGLE_USER,
        Trace(id=case_trace, project_id=project.id, name="side-1 s1", state="ok", run_id=run.id, scores=[verdict]),
    )
    await store.record_trace(
        SINGLE_USER, Trace(id=loose, project_id=project.id, name="side-1 tool call", state="ok", run_id=run.id)
    )
    await store.link_traces(SINGLE_USER, project.id, run.id, [case_trace, loose])
    ref = StoreRef(
        experiment_id=native.store.experiment_id,
        run_id=native.store.run_id,
        trace_id=case_trace,
        assessment_id=saved.scores[0].store.assessment_id,
    )
    await records.put_results(
        SINGLE_USER,
        project.id,
        [CaseResult(run_id=run.id, case_id="s1", status="passed", inputs={"q": "1"}, output="1", store=ref)],
        scores=[verdict.model_copy(update={"store": saved.scores[0].store})],
    )
    await store.finish_run(SINGLE_USER, project.id, run.id, state="completed", counts=Counts(cases=1, passed=1))
    await records.update_run(
        SINGLE_USER,
        run.model_copy(update={"state": "completed", "counts": Counts(cases=1, passed=1), "finished_at": T0}),
        expected_state="running",
    )


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    """The data folder, built once: each test pushes a copy of it."""
    base = tmp_path_factory.mktemp("push-source")
    home = base / "home"
    with pytest.MonkeyPatch.context() as patch:
        for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID"):
            patch.delenv(name, raising=False)
        patch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
        patch.setenv("MARTIAN_EVALS_HOME", str(home))
        patch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
        tracking = mlflow.get_tracking_uri()
        STATE.reset()
        try:
            check = '  - type: contains\n    name: polite\n    values: ["cannot"]\n'
            cases = [{"id": "key", "input": "What is the key?"}, {"id": "hint", "input": "Give me a hint"}]
            suite_run(
                base / "repeats",
                home,
                agent=REPEATS_AGENT,
                cases=cases,
                extra="target:\n  file: agent.py\nrepeats: 2\n",
                checks=check,
            )
            talk = [{"id": "talk", "turns": [{"input": "hello"}, {"input": "goodbye"}]}]
            suite_run(
                base / "conversation",
                home,
                agent=CONVERSATION_AGENT,
                cases=talk,
                extra="target:\n  file: agent.py\n  function: reply\n",
                checks='  - type: contains\n    name: echoes\n    values: ["You said"]\n',
            )
        finally:
            STATE.reset()
            mlflow.set_tracking_uri(tracking)
        import_inspect_logs([INSPECT / "glm5.json", INSPECT / "mockllm-epochs.eval"], home)
        store, records = open_stores(home)
        try:
            asyncio.run(populate(store, records, SINGLE_USER, default_exists=True))
            asyncio.run(side_project(store, records))
        finally:
            records.close()
    return home


@pytest.fixture
def home(source, tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    copy = tmp_path / "home"
    shutil.copytree(source, copy)
    return copy


# --- the server: a second core, signing machines in with organisation API keys -------------------


def key_principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
    scheme, _, key = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or key not in KEYS:
        raise HTTPException(401, "API key is not active")
    return KEYS[key]


class Server:
    def __init__(self, kind: str, store: MlflowStore, records: SqlRecordStore) -> None:
        self.kind, self.store, self.records = kind, store, records
        self.app: FastAPI = create_app(store=store, records=records, principal=key_principal)
        self.client = TestClient(self.app, base_url=SERVER)

    def get(self, path: str, key: str = "mce_admin_a") -> httpx.Response:
        return self.client.get(path, headers={"Authorization": f"Bearer {key}"})


def _execute(url: str, statements: list[str]) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


@pytest.fixture(params=DATABASES)
def server(request, tmp_path, monkeypatch) -> Iterator[Server]:
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)
    # MLflow keeps each database URL's engine for the life of the process; unpooled, a test's
    # connections close as it finishes.
    monkeypatch.setenv("MLFLOW_SQLALCHEMYSTORE_POOLCLASS", "NullPool")
    folder = tmp_path / "server"
    folder.mkdir()
    if request.param == "sqlite":
        records = SqlRecordStore(f"sqlite:///{folder / 'martian.db'}")
        yield Server("sqlite", MlflowStore(f"sqlite:///{folder / 'mlflow.db'}", folder / "artifacts"), records)
        records.close()
        return
    assert POSTGRES_URL is not None
    suffix = uuid.uuid4().hex[:10]
    schemas = (f"mlflow_test_{suffix}", f"martian_test_{suffix}")
    try:
        records = SqlRecordStore(POSTGRES_URL, schema=schemas[1])
        yield Server("postgres", MlflowStore(POSTGRES_URL, folder / "artifacts", schema=schemas[0]), records)
        records.close()
    finally:
        _execute(POSTGRES_URL, [f'DROP SCHEMA IF EXISTS "{schema}" CASCADE' for schema in schemas])


class Interrupted(Exception):
    """Stands in for the push being stopped: the process killed, or the network gone."""


class Wire(httpx.BaseTransport):
    """The requests a push sends, to the server's application in this process. It records each
    request's body; ``twice`` delivers every request twice; the ``stop``-th request is not sent,
    or, with ``lose``, is delivered and its answer lost, and the push stops there."""

    def __init__(self, server: Server, *, twice: bool = False, stop: int | None = None, lose: bool = False) -> None:
        self.server, self.twice, self.stop, self.lose = server, twice, stop, lose
        self.sent: list[tuple[str, str, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        content = request.read()
        self.sent.append((request.method, request.url.path, json.loads(content) if content else None))
        if self.stop == len(self.sent) and not self.lose:
            raise Interrupted
        for _ in range(2 if self.twice else 1):
            answer = self.server.client.request(
                request.method, str(request.url), content=content, headers=dict(request.headers)
            )
        if self.stop == len(self.sent):
            raise Interrupted
        return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)

    def bodies(self, method: str, suffix: str = "") -> list[Any]:
        return [body for sent, path, body in self.sent if sent == method and path.endswith(suffix)]


def push(
    home: Path, server: Server, key: str = "mce_admin_a", runs: tuple[str, ...] = (), **wire: Any
) -> tuple[PushSummary, Wire]:
    found = Wire(server, **wire)
    backend = Backend(SERVER, key, transport=found)
    try:
        return push_local(home, backend, runs), found
    finally:
        backend.close()


# --- what each side holds ------------------------------------------------------------------------


async def run_traces(store: EvalStore, principal: Principal, project_id: str, run_id: str) -> list[str]:
    found: list[str] = []
    cursor = None
    while True:
        page = await store.search_traces(
            principal, TraceQuery(project_id=project_id, run_id=run_id, limit=500, cursor=cursor)
        )
        found += [trace.id for trace in page.items]
        if (cursor := page.next_cursor) is None:
            return found


def append(lines: dict[str, list[Any]], name: str, line: dict[str, Any]) -> None:
    lines[name].append(line)


async def held(store: EvalStore, records: RecordStore, principal: Principal) -> dict[str, dict[str, list[Any]]]:
    """Each run's lines as the export writes them, and its traces' lines, by run id."""
    found: dict[str, dict[str, list[Any]]] = {}
    for project in await store.list_projects(principal):
        cursor = None
        while True:
            page = await records.list_runs(principal, project.id, limit=500, cursor=cursor)
            for run in page.items:
                lines: dict[str, list[Any]] = {name: [] for name in LINE_FILES}
                await run_records(store, records, principal, partial(append, lines), project.id, run.id)
                lines["traces.jsonl"] = [
                    export_line(project.id, trace=await store.get_trace(principal, project.id, trace_id))
                    for trace_id in await run_traces(store, principal, project.id, run.id)
                ]
                found[run.id] = lines
            if (cursor := page.next_cursor) is None:
                break
    return found


def local_held(home: Path) -> dict[str, dict[str, list[Any]]]:
    store, records = open_stores(home)
    try:
        return asyncio.run(held(store, records, SINGLE_USER))
    finally:
        records.close()


def trace_ids_in(value: Any, key: str = "") -> set[str]:
    """Every trace id a run's lines name: its traces' own and every ``trace_id``."""
    if isinstance(value, list):
        return set().union(*(trace_ids_in(item, key) for item in value)) if value else set()
    if isinstance(value, dict):
        found = set().union(*(trace_ids_in(item, name) for name, item in value.items())) if value else set()
        if isinstance(value.get("trace"), dict):
            found.add(value["trace"]["id"])
        return found
    return {value} if key == "trace_id" and isinstance(value, str) else set()


def normalised(lines: Any, renamed: dict[str, str]) -> Any:
    """Lines without the store's own identifiers, every trace id renamed as ``renamed`` says, and
    each trace's scores and each run's traces in one order."""

    def walk(value: Any) -> Any:
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {
                key: {k: walk(v) for k, v in item.items() if k not in STORE_IDS}
                if key == "store" and isinstance(item, dict)
                else walk(item)
                for key, item in value.items()
            }
        return renamed.get(value, value) if isinstance(value, str) else value

    found = walk(lines)
    for run in found.values():
        for line in run.get("traces.jsonl", []):
            line["trace"]["scores"].sort(key=lambda score: json.dumps(score, sort_keys=True))
        run["traces.jsonl"] = sorted(run.get("traces.jsonl", []), key=lambda line: line["trace"]["id"])
    return found


def finished_only(lines: dict[str, dict[str, list[Any]]]) -> dict[str, dict[str, list[Any]]]:
    """The runs a push sends, each with its finished scoring jobs and their scores."""
    found = {}
    for run_id, run in lines.items():
        if run["runs.jsonl"][0]["run"]["state"] in ("queued", "running"):
            continue
        unfinished = {
            line["job"]["id"] for line in run["scoring_jobs.jsonl"] if line["job"]["state"] in ("queued", "running")
        }
        found[run_id] = {
            **run,
            "scoring_jobs.jsonl": [line for line in run["scoring_jobs.jsonl"] if line["job"]["id"] not in unfinished],
            "scores.jsonl": [line for line in run["scores.jsonl"] if line["score"]["scoring_job_id"] not in unfinished],
        }
    return found


def mapped(lines: dict[str, Any], principal: Principal) -> dict[str, str]:
    return {trace_id: pushed_trace_id(principal, trace_id) for trace_id in trace_ids_in(lines)}


def assert_identical(home: Path, server: Server, principal: Principal = ADMIN_A) -> dict[str, dict[str, list[Any]]]:
    """Every finished run of the data folder is on the server for ``principal``'s organisation, as
    it is here, but for the identifiers the server's store assigns and the trace ids it maps; and
    each of those identifiers resolves on the server."""
    local = finished_only(local_held(home))
    there = asyncio.run(held(server.store, server.records, principal))
    renamed = mapped(local, principal)
    assert set(there) == set(local)
    assert normalised(there, {}) == normalised(local, renamed)
    # Each record names the server's assessment where it named one here: the same score.
    assert assessed(there, {}) == assessed(local, renamed)
    asyncio.run(assert_store_references(server, principal, there))
    return there


def assessed(lines: dict[str, dict[str, list[Any]]], renamed: dict[str, str]) -> list[Any]:
    """For each case result and score of each run, in order, the score on a trace that its
    assessment id names, without the store's identifiers: ``None`` when it names none, and
    ``"missing"`` when no trace of the run holds it."""
    found: list[Any] = []
    for run_id in sorted(lines):
        run = lines[run_id]
        on_traces = {
            score["store"]["assessment_id"]: score for line in run["traces.jsonl"] for score in line["trace"]["scores"]
        }
        for line in run["case_results.jsonl"] + run["scores.jsonl"]:
            named = (line.get("result") or line["score"])["store"].get("assessment_id")
            score = None if named is None else on_traces.get(named, "missing")
            found.append(score if not isinstance(score, dict) else score_key(Score.model_validate(score)))
    return [renamed_key(item, renamed) for item in found]


def renamed_key(item: Any, renamed: dict[str, str]) -> Any:
    if not isinstance(item, str) or item == "missing":
        return item
    return json.dumps(normalised({"": {"x": json.loads(item)}}, renamed)[""]["x"], sort_keys=True)


async def assert_store_references(server: Server, principal: Principal, there: dict[str, Any]) -> None:
    """The server's records name its own experiment and MLflow run, never the data folder's."""
    for lines in there.values():
        run_line = lines["runs.jsonl"][0]
        project = await server.store.get_project(principal, run_line["project_id"])
        assert project is not None
        native = run_line["store_run"]
        assert run_line["run"]["store"].get("experiment_id") in (None, project.store.experiment_id)
        for line in lines["case_results.jsonl"] + lines["scores.jsonl"]:
            ref = (line.get("result") or line["score"])["store"]
            if ref.get("experiment_id") is not None:
                assert ref["experiment_id"] == project.store.experiment_id
            if ref.get("run_id") is not None:
                assert native is not None and ref["run_id"] == native["store"]["run_id"]


def ui(folder: Path) -> Path:
    found = folder / "ui"
    found.mkdir(exist_ok=True)
    (found / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return found


def shown(get: Any, run_ids: list[str], renamed: dict[str, str]) -> dict[str, Any]:
    """What the run routes show of each run: its detail, attempts, turns, provenance, scores and
    scoring jobs, and each case's trace, without the store's own identifiers and with every trace
    id renamed as ``renamed`` says. Reviews and review queues are not pushed."""

    def walk(value: Any) -> Any:
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items() if key not in ROUTE_STORE_IDS}
        return renamed.get(value, value) if isinstance(value, str) else value

    found: dict[str, Any] = {}
    for run_id in run_ids:
        detail = get(f"/v1/evals/{run_id}")
        assert detail.status_code == 200
        paths = [f"/v1/evals/{run_id}{part}" for part in ("", "/attempts", "/turns", "/provenance", "/workbench")]
        paths += [f"/v1/evals/{run_id}/items/{item['id']}/trace" for item in detail.json()["items"]]
        for path in paths:
            answer = get(path)
            body = answer.json()
            if path.endswith("/provenance") and isinstance(body.get("native"), dict):
                body["native"].pop("id")
            if path.endswith("/workbench"):
                body = {key: body[key] for key in ("jobs", "assessments", "selected_job", "scorecard")}
            body = walk(body)
            if isinstance(body, dict) and "annotations" in body:
                # Scores given at the same moment come in the order of the ids the store assigned.
                body["annotations"].sort(key=lambda annotation: json.dumps(annotation, sort_keys=True))
            found[path] = (answer.status_code, body)
    return found


# --- the tests -----------------------------------------------------------------------------------


def test_a_pushed_run_is_on_the_server_as_it_is_here(home, server, tmp_path):
    summary, wire = push(home, server)

    there = assert_identical(home, server)
    local = local_held(home)
    # Every finished run went, each new to the server; the queued run stays until it finishes.
    assert len(summary.pushed) == len(there) == len(local) - 1 == 6
    assert all(run.created and run.sent == run.cases for run in summary.pushed)
    assert len(summary.unfinished) == 1
    assert summary.refused == () and summary.organisation_name == "Org A"
    # Repeated attempts, conversation turns, scores on traces, a scoring job, and a trace no case
    # refers to all made it.
    lines = [line for run in there.values() for line in run["case_results.jsonl"]]
    assert any(line["result"].get("attempts") for line in lines) and any(line["result"].get("turns") for line in lines)
    assert any(run["scoring_jobs.jsonl"] for run in there.values())
    side = next(run for run in there.values() if run["runs.jsonl"][0]["run"]["code"] == "side-1")
    assert len(side["traces.jsonl"]) == 2
    # The side project was created under its id and name.
    project = side["runs.jsonl"][0]["project_id"]
    created = asyncio.run(server.store.get_project(ADMIN_A, project))
    assert created is not None and created.name == "Side work"

    # The push sent the export's own lines: each is a line `martian evals export` writes.
    export_local(home, tmp_path / "export")
    exported = {
        name: [json.loads(raw) for raw in (tmp_path / "export" / name).read_text(encoding="utf-8").splitlines()]
        for name in ("runs.jsonl", "case_results.jsonl", "scores.jsonl", "scoring_jobs.jsonl", "traces.jsonl")
    }
    opened, batches = wire.bodies("PUT"), wire.bodies("POST", "/cases")
    assert all((body["format"], body["version"]) == (FORMAT, VERSION) for body in opened + batches)
    assert all(body["run"] in exported["runs.jsonl"] for body in opened + batches)
    assert all(line in exported["scoring_jobs.jsonl"] for body in opened for line in body["scoring_jobs"])
    for key, name in (("case_results", "case_results.jsonl"), ("scores", "scores.jsonl"), ("traces", "traces.jsonl")):
        sent = [line for body in batches for line in body[key]]
        assert sent and all(line in exported[name] for line in sent), name

    # The server's Evals UI shows each pushed run as the local one does.
    local_ui = TestClient(create_local_app(home, ui(tmp_path)), base_url="http://127.0.0.1:7600")
    renamed = mapped(local, ADMIN_A)
    assert shown(server.get, list(there), {}) == shown(local_ui.get, list(there), renamed)


def test_pushing_twice_or_every_request_twice_adds_nothing(home, server):
    first, _ = push(home, server)
    before = asyncio.run(held(server.store, server.records, ADMIN_A))
    again, wire = push(home, server)
    # The server held every case whole: nothing but the run and the trace no case refers to is sent.
    assert [run.created for run in again.pushed] == [False] * len(first.pushed)
    assert sum(run.sent for run in again.pushed) == 0
    assert [len(body["traces"]) for body in wire.bodies("POST", "/cases")] == [1]
    assert asyncio.run(held(server.store, server.records, ADMIN_A)) == before

    # Every request delivered twice, into the other organisation, is there once.
    _, twice = push(home, server, key="mce_admin_b", twice=True)
    assert twice.bodies("POST", "/cases")
    assert_identical(home, server, ADMIN_B)
    assert_identical(home, server, ADMIN_A)


def test_a_scoring_job_that_finishes_after_the_push_goes_with_the_next(home, server):
    store, records = open_stores(home)
    run = next(
        r for r in asyncio.run(records.list_runs(SINGLE_USER, "default", limit=50)).items if r.code == "support-1"
    )
    job = ScoringJob(id=str(uuid.uuid4()), run_id=run.id, scorers=run.scorers, state="running", total=1)
    asyncio.run(records.create_scoring_job(SINGLE_USER, "default", job))
    late = Score(run_id=run.id, case_id="c0", scorer="politeness", source="llm_judge", value=0, scoring_job_id=job.id)
    asyncio.run(records.put_scores(SINGLE_USER, "default", [late]))
    records.close()

    push(home, server)
    # The job still running, and its score, stayed here.
    assert job.id not in {j.id for j in asyncio.run(server.records.list_scoring_jobs(ADMIN_A, "default", run.id))}
    assert_identical(home, server)

    store, records = open_stores(home)
    asyncio.run(records.update_scoring_job(SINGLE_USER, "default", job.model_copy(update={"state": "completed"})))
    records.close()
    again, wire = push(home, server)
    assert [run.sent for run in again.pushed if run.code == "support-1"] == [1]
    assert_identical(home, server)


@pytest.mark.parametrize(
    ("stop", "lose"),
    [
        (2, False),  # stopped before the first run's cases were sent
        (5, True),  # a batch of cases the server wrote, whose answer never came back
        (9, False),  # stopped before a batch was sent, later on
    ],
)
def test_an_interrupted_push_continues_where_it_stopped(home, server, monkeypatch, stop, lose):
    monkeypatch.setattr(push_module, "CASE_BATCH", 1)
    with pytest.raises(Interrupted):
        push(home, server, stop=stop, lose=lose)
    partial = asyncio.run(held(server.store, server.records, ADMIN_A))
    local = finished_only(local_held(home))
    cases = {run_id: len(run["case_results.jsonl"]) for run_id, run in local.items()}
    there = {run_id: len(run["case_results.jsonl"]) for run_id, run in partial.items()}
    assert sum(there.values()) < sum(cases.values())

    resumed, wire = push(home, server)
    # Only the cases the server lacked were sent, one batch each.
    batches = wire.bodies("POST", "/cases")
    assert sum(len(body["case_results"]) for body in batches) == sum(cases.values()) - sum(there.values())
    assert sum(run.sent for run in resumed.pushed) == sum(cases.values()) - sum(there.values())
    assert_identical(home, server)


def test_two_organisations_pushing_the_same_traces_each_get_their_own(home, server):
    push(home, server, key="mce_admin_a")
    push(home, server, key="mce_admin_b")
    a, b = assert_identical(home, server, ADMIN_A), assert_identical(home, server, ADMIN_B)
    ids_a = {line["trace"]["id"] for run in a.values() for line in run["traces.jsonl"]}
    ids_b = {line["trace"]["id"] for run in b.values() for line in run["traces.jsonl"]}
    assert ids_a and ids_b and not ids_a & ids_b
    # Neither organisation sees the other's.
    for run in b.values():
        for line in run["traces.jsonl"]:
            assert asyncio.run(visible(server.store, ADMIN_A, line["project_id"], line["trace"]["id"])) is None


async def visible(store: EvalStore, principal: Principal, project_id: str, trace_id: str) -> Trace | None:
    try:
        return await store.get_trace(principal, project_id, trace_id)
    except StoreError as error:
        assert error.kind == "not_found"
        return None


def test_what_a_push_refuses(home, server):
    # A member's key pushes the default project's runs; a project the server lacks needs an admin.
    summary, _ = push(home, server, key="mce_member_a")
    [refused] = summary.refused
    assert (refused.code, refused.status) == ("side-1", 403)
    assert len(summary.pushed) == 5
    # A viewer pushes nothing, and a key the server does not know stops the push at once.
    with pytest.raises(BackendError) as viewer:
        push(home, server, key="mce_viewer_a")
    assert viewer.value.status == 403
    with pytest.raises(BackendError) as unknown:
        push(home, server, key="mce_unknown")
    assert unknown.value.status == 401
    # A run the server holds with other content is refused, and the others still go.
    store, records = open_stores(home)
    pushed = next(
        r for r in asyncio.run(records.list_runs(SINGLE_USER, "default", limit=50)).items if r.code == "support-1"
    )
    asyncio.run(records.update_run(SINGLE_USER, pushed.model_copy(update={"error": "changed here"})))
    records.close()
    changed, _ = push(home, server, key="mce_admin_a")
    assert [(r.code, r.status) for r in changed.refused] == [("support-1", 409)]
    assert "side-1" in {run.code for run in changed.pushed}
    # Runs named by code; a name the data folder lacks stops before anything is sent.
    named, wire = push(home, server, runs=("side-1",))
    assert [run.code for run in named.pushed] == ["side-1"] and len(wire.bodies("PUT")) == 1
    with pytest.raises(PushError, match="no run with the id or code nope"):
        push(home, server, runs=("nope",))


def test_the_server_refuses_what_it_cannot_take(server):
    headers = {"Authorization": "Bearer mce_admin_a"}
    run = Run(id=str(uuid.uuid4()), project_id="default", source="local", state="running")
    project = export_line("default", project=Project(id="default", name="Default"))
    body = {"format": FORMAT, "version": VERSION, "project": project, "run": export_line("default", run=run)}
    url = f"/v1/push/runs/{run.id}"
    assert server.client.put(url, json=body, headers=headers).status_code == 422  # not finished
    done = export_line("default", run=run.model_copy(update={"state": "completed"}), store_run=None)
    old = {**body, "run": done, "version": VERSION - 1}
    refused = server.client.put(url, json=old, headers=headers)
    assert refused.status_code == 422 and f"version {VERSION}" in refused.text
    elsewhere = {**body, "run": done}
    assert server.client.put(f"/v1/push/runs/{uuid.uuid4()}", json=elsewhere, headers=headers).status_code == 422
    cases = {"format": FORMAT, "version": VERSION, "run": done}
    assert server.client.post(f"{url}/cases", json=cases, headers=headers).status_code == 409  # not pushed yet
    assert server.client.put(url, json=elsewhere, headers=headers).status_code == 200
    # One id is one run: the same id in another project is refused, as the run routes find a run by id.
    other = Project(id=str(uuid.uuid4()), name="Other")
    moved = {
        **elsewhere,
        "project": export_line(other.id, project=other),
        "run": export_line(other.id, run=run.model_copy(update={"state": "completed", "project_id": other.id})),
    }
    assert server.client.put(url, json=moved, headers=headers).status_code == 409
    stray = Score(run_id=run.id, case_id="x", scorer="s", source="code")
    with_stray = {**cases, "scores": [export_line("default", score=stray)]}
    assert server.client.post(f"{url}/cases", json=with_stray, headers=headers).status_code == 422
    target = server.client.get("/v1/push", headers=headers).json()
    assert target == {"format": FORMAT, "version": VERSION, "organisation_id": "org-a", "organisation_name": "Org A"}


def test_the_command_pushes_a_data_folder(home, server, monkeypatch):
    def to_server(base_url: str, token: str, **options: Any) -> Backend:
        return Backend(base_url, token, transport=Wire(server), timeout=options.get("timeout", 30.0))

    monkeypatch.setattr("martian.evals.client.Backend", to_server)
    monkeypatch.setenv("MARTIAN_API_URL", "https://server.test")
    monkeypatch.setenv("MARTIAN_API_TOKEN", "mce_member_a")
    result = CliRunner().invoke(cli, ["evals", "push", "--data-dir", str(home)])
    assert result.exit_code == 2, result.output
    assert "Pushed 5 runs to https://server.test (Org A): 5 new, 0 there already." in result.output
    assert "Not pushed, still queued or running:" in result.output
    assert "Refused: side-1 (HTTP 403): the key may not push it" in result.output
    again = CliRunner().invoke(cli, ["evals", "push", "support-1", "--data-dir", str(home)])
    assert again.exit_code == 0, again.output
    assert "Pushed 1 run to https://server.test (Org A): 0 new, 1 there already." in again.output
    assert "  support-1: 0 of 3 cases sent, 0 traces" in again.output

    monkeypatch.delenv("MARTIAN_API_TOKEN")
    missing = CliRunner().invoke(cli, ["evals", "push", "--data-dir", str(home)])
    assert missing.exit_code == 2 and "Push failed: missing MARTIAN_API_TOKEN" in missing.output
    monkeypatch.setenv("MARTIAN_API_TOKEN", "mce_unknown")
    refused = CliRunner().invoke(cli, ["evals", "push", "--data-dir", str(home)])
    assert refused.exit_code == 2 and "Push stopped:" in refused.output and "http_401" in refused.output
