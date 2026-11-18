"""MLflow behaviour the evals core depends on that the store contract does not already pin.

Part of the MLflow compatibility suite, which ``scripts/mlflow_compat.py`` runs on the pinned
mlflow-skinny and on a candidate release. Two of these checks only run under that script:

- ``MARTIAN_MLFLOW_VERSION`` names the release the run is meant to test, so a run that quietly
  fell back to the locked version fails instead of passing for the wrong release.
- ``MARTIAN_MLFLOW_UPGRADE_STORE`` names a folder. The first release writes a store there; each
  later release must open it, migrating MLflow's schema, and read back the same content.
"""

import json
import os
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import pytest
from mlflow.entities import TraceInfo, TraceLocation, TraceState
from mlflow.exceptions import MlflowException

from martian.evals.model import (
    Case,
    Counts,
    Project,
    ReviewQuestion,
    ReviewQueue,
    Run,
    Score,
    ScorerVersion,
    Span,
    TraceQuery,
)
from martian.evals.model import Trace as EvalTrace
from martian.evals.server.principal import organisation_principal
from martian.evals.store.mlflow_local import MlflowStore

ORG = organisation_principal("user-a", "org-a", frozenset({"admin"}))
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")
UPGRADE_STORE = os.environ.get("MARTIAN_MLFLOW_UPGRADE_STORE")
# Trace ids that name something other than one folder under an experiment's "traces" folder.
ESCAPING_TRACE_IDS = ["../../escaped", "..", "a/b", "/tmp/escaped"]


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)


@pytest.mark.skipif("MARTIAN_MLFLOW_VERSION" not in os.environ, reason="run by scripts/mlflow_compat.py")
def test_the_suite_runs_on_the_release_it_was_asked_to_test():
    assert version("mlflow-skinny") == os.environ["MARTIAN_MLFLOW_VERSION"]


@pytest.mark.parametrize("trace_id", ESCAPING_TRACE_IDS)
def test_mlflow_refuses_a_trace_id_that_would_leave_the_trace_folder(tmp_path, trace_id):
    """MLflow builds a trace's artifact location from its id. Up to 3.17 the SQL store took any id
    (the SQL-store twin of CVE-2026-53995, GHSA-wqmh-qccw-pqwf, fixed in 3.18.0 by mlflow#27311)."""
    from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore

    store = SqlAlchemyStore(f"sqlite:///{tmp_path / 'mlflow.db'}", str(tmp_path / "artifacts"))
    experiment_id = store.create_experiment("compat")
    info = TraceInfo(
        trace_id=trace_id,
        trace_location=TraceLocation.from_experiment_id(experiment_id),
        request_time=1,
        state=TraceState.OK,
    )
    with pytest.raises(MlflowException) as refused:
        store.start_trace(info)
    assert refused.value.error_code == "INVALID_PARAMETER_VALUE"


async def test_the_store_gives_mlflow_only_trace_ids_of_mlflow_form(tmp_path):
    """A Martian trace id can be any text; MLflow only ever sees ``tr-`` and 32 hex digits, so the
    trace's artifacts stay in its own folder whatever the MLflow release checks."""
    store = MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path / "artifacts")
    project = await store.create_project(ORG, Project(id="p1", name="Agent"))
    saved = await store.record_trace(ORG, EvalTrace(id="../../escaped", project_id="p1", name="chat", state="ok"))
    assert saved.id == "../../escaped"

    info = await store._call(ORG, lambda: store._trace_info(project.store.experiment_id, saved.id))
    assert info.trace_id.startswith("tr-") and len(info.trace_id) == 35
    location = Path(info.tags["mlflow.artifactLocation"])
    assert ".." not in location.parts and location.is_relative_to((tmp_path / "artifacts").resolve())
    assert location.parts[-3:] == ("traces", info.trace_id, "artifacts")


# --- the upgrade path ------------------------------------------------------------------------


async def _write_reference(store: MlflowStore) -> None:
    await store.create_project(ORG, Project(id="p1", name="Support agent"))
    await store.create_dataset(
        ORG,
        "p1",
        "questions",
        [
            Case(id="c0", inputs={"message": "hi"}, expectations={"answer": "hello"}, tags={"kind": "greeting"}),
            Case(id="c1", inputs={"message": "refund?"}, expectations={"answer": "no"}, metadata={"n": 1}),
        ],
    )
    await store.start_run(ORG, Run(id="r1", project_id="p1", source="local", state="running", dataset_name="questions"))
    span = Span(id="s1", name="model call", kind="LLM", status="ok", inputs={"q": 1}, outputs="hello")
    for trace_id, minutes in (("t1", 0), ("t2", 1)):
        await store.record_trace(
            ORG,
            EvalTrace(
                id=trace_id,
                project_id="p1",
                name="chat",
                state="ok",
                request_time=T0.replace(minute=minutes),
                request="hi",
                response="hello",
                run_id="r1",
                session_id="s1",
                user_id="alice",
                tags={"team": "a"},
                spans=[span],
            ),
        )
    await store.log_score(ORG, "p1", "t1", Score(scorer="exact_match", source="code", value=True, passed=True))
    await store.log_score(
        ORG, "p1", "t2", Score(scorer="rubric_judge", source="llm_judge", value=False, passed=False, rationale="No.")
    )
    await store.finish_run(ORG, "p1", "r1", state="completed", counts=Counts(cases=2, passed=1, failed=1))
    await store.register_scorer(
        ORG,
        "p1",
        ScorerVersion(name="politeness", kind="llm_judge", type="guidelines", criteria={"guidelines": ["Be kind."]}),
    )
    question = ReviewQuestion(name="correct", instruction="Is the answer correct?")
    await store.save_question(ORG, "p1", question)
    await store.create_queue(
        ORG,
        "p1",
        ReviewQueue(id="q1", run_id="r1", name="Check answers", questions=[question], case_ids=["c0", "c1"]),
        ["t1", "t2"],
    )
    await store.put_artifact(ORG, "p1", "r1", "summary.json", b'{"passed": 1}')


async def _read_reference(store: MlflowStore) -> dict[str, object]:
    (dataset,) = await store.list_datasets(ORG, "p1")
    traces = await store.search_traces(ORG, TraceQuery(project_id="p1", oldest_first=True))
    full = await store.get_trace(ORG, "p1", "t1")
    queue = await store.get_queue(ORG, "p1", "q1")
    assert full is not None and queue is not None
    artifact = await store.get_artifact(ORG, "p1", "r1", "summary.json")
    return {
        "projects": [p.model_dump(mode="json") for p in await store.list_projects(ORG)],
        "dataset": dataset.model_dump(mode="json"),
        "cases": [c.model_dump(mode="json") for c in (await store.read_cases(ORG, "p1", dataset.id)).items],
        "run": (await store.get_run(ORG, "p1", "r1")).model_dump(mode="json"),  # type: ignore[union-attr]
        "traces": [t.model_dump(mode="json") for t in traces.items],
        "trace": full.model_dump(mode="json"),
        "sessions": (await store.search_sessions(ORG, TraceQuery(project_id="p1"))).model_dump(mode="json"),
        "scorers": [s.model_dump(mode="json") for s in await store.list_scorers(ORG, "p1")],
        "questions": [q.model_dump(mode="json") for q in await store.list_questions(ORG, "p1")],
        "queue": queue.model_dump(mode="json"),
        "queue_items": [i.model_dump(mode="json") for i in await store.queue_items(ORG, "p1", "q1")],
        "artifacts": [await store.list_artifacts(ORG, "p1", "r1"), (artifact or b"").decode()],
    }


@pytest.mark.skipif(not UPGRADE_STORE, reason="run by scripts/mlflow_compat.py")
@pytest.mark.parametrize("database", ["sqlite", "postgres"])
async def test_a_store_written_by_an_earlier_release_reads_back_the_same(database):
    """The first release writes the store; a later one opens it (MLflow migrates its schema in
    place) and must read back exactly what was written, then take new writes."""
    if database == "postgres" and not POSTGRES_URL:
        pytest.skip("set MARTIAN_TEST_POSTGRES_URL to check the upgrade on Postgres")
    assert UPGRADE_STORE is not None
    folder = Path(UPGRADE_STORE) / database
    record = folder / "written.json"
    written = json.loads(record.read_text(encoding="utf-8")) if record.exists() else None
    schema = written["schema"] if written else f"mlflow_upgrade_{uuid.uuid4().hex[:10]}"
    url = POSTGRES_URL if database == "postgres" else f"sqlite:///{folder / 'mlflow.db'}"
    assert url is not None
    folder.mkdir(parents=True, exist_ok=True)
    keep = False  # a written Postgres store stays for the next release; anything else is dropped
    try:
        store = MlflowStore(url, folder / "artifacts", schema=schema)
        if written is None:
            await _write_reference(store)
            content = await _read_reference(store)
            record.write_text(
                json.dumps({"mlflow": version("mlflow-skinny"), "schema": schema, "content": content}),
                encoding="utf-8",
            )
            keep = True
            return
        assert await _read_reference(store) == written["content"], (
            f"a store written by mlflow-skinny {written['mlflow']} reads back differently on {version('mlflow-skinny')}"
        )
        later = await store.record_trace(ORG, EvalTrace(id="t3", project_id="p1", name="chat", state="ok"))
        assert (await store.get_trace(ORG, "p1", later.id)) is not None
    finally:
        if database == "postgres" and not keep:
            from sqlalchemy import create_engine, text

            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            engine.dispose()
