"""The in-process mlflow-skinny binding passes the store contract on SQLite, and on Postgres when
MARTIAN_TEST_POSTGRES_URL names a database it may create schemas in."""

import contextlib
import os
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from martian.evals.model import Counts, Project, Run, Trace, TraceQuery
from martian.evals.server.principal import SINGLE_USER, organisation_principal
from martian.evals.store import EvalStore
from tests.evals.store_contract import ORG_A, StoreContract

POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")
LONGEST_ARTIFACT_ROOT = 157
"""MLflow keeps a run's artifact location in ``runs.artifact_uri``, 200 characters, and a run's
folder adds ``/<32-digit run id>/artifacts``, 43, to the artifact root."""


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)


@pytest.fixture
def unpooled(monkeypatch):
    """MLflow keeps each database URL's engine, and its open connections, for the life of the
    process, and every Postgres test's schema is a new URL. Without a pool a test's connections
    close as it finishes, so a full run, or several on one server, stay within its connection limit."""
    monkeypatch.setenv("MLFLOW_SQLALCHEMYSTORE_POOLCLASS", "NullPool")


@contextlib.contextmanager
def application_tracing(monkeypatch, tracking_uri: str, project: Project) -> Iterator[None]:
    """MLflow tracing in this process recording into the local user's workspace and the project's
    experiment, as an application does; the traces are sent before it ends."""
    import mlflow

    monkeypatch.setenv("MLFLOW_ENABLE_WORKSPACES", "true")
    monkeypatch.setenv("MLFLOW_WORKSPACE", "local")
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    # MLflow also writes these two to the environment; monkeypatch puts them back afterwards.
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", str(project.store.experiment_id))
    mlflow.set_tracking_uri(tracking_uri)
    try:
        mlflow.set_experiment(experiment_id=project.store.experiment_id)
        yield
        mlflow.flush_trace_async_logging()
    finally:
        mlflow.set_tracking_uri(old_uri)
    monkeypatch.delenv("MLFLOW_ENABLE_WORKSPACES")
    monkeypatch.delenv("MLFLOW_WORKSPACE")


class MlflowTracingContract:
    """Traces an application records with MLflow tracing itself, read through the binding. A
    binding's class provides ``tracking_uri``: the database the store keeps MLflow's tables in."""

    async def test_traces_recorded_with_mlflow_tracing_form_sessions_and_turns(
        self, store: EvalStore, tracking_uri: str, monkeypatch
    ) -> None:
        import mlflow

        project = await store.create_project(SINGLE_USER, Project(id="p1", name="Support agent"))
        recorded: dict[str, list[str]] = {}
        with application_tracing(monkeypatch, tracking_uri, project):

            def turn(message: str, **trace_fields: object) -> None:
                with mlflow.start_span(name="chat") as span:
                    span.set_inputs({"message": message})
                    mlflow.update_current_trace(**trace_fields)  # type: ignore[arg-type]
                    span.set_outputs(f"echo {message}")
                recorded.setdefault(message.split()[0], []).append(span.trace_id)
                time.sleep(0.01)  # a distinct request time for each turn

            turn("alice hello", session_id="conversation-1", user="alice")
            turn("alice again", session_id="conversation-1", user="alice")
            turn("nobody here")
            # The metadata keys MLflow's documentation gives for users and sessions.
            turn("bob hi", metadata={"mlflow.trace.session": "conversation-2", "mlflow.trace.user": "bob"})

        found = await store.search_sessions(SINGLE_USER, TraceQuery(project_id="p1"))
        assert (found.traces_examined, found.ungrouped, found.has_more) == (4, 1, False)
        newest, older = found.items
        assert (newest.id, newest.turns, newest.preview) == ("conversation-2", 1, '{"message": "bob hi"}')
        assert (older.id, older.turns, older.errors) == ("conversation-1", 2, 0)
        assert older.preview == '{"message": "alice hello"}'
        assert older.started_at is not None and older.last_at is not None and older.started_at < older.last_at

        turns = await store.search_traces(
            SINGLE_USER, TraceQuery(project_id="p1", session_id="conversation-1", oldest_first=True)
        )
        assert [t.id for t in turns.items] == recorded["alice"]
        assert {(t.user_id, t.name, t.state) for t in turns.items} == {("alice", "chat", "ok")}
        by_user = await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1", user_id="bob"))
        assert [t.id for t in by_user.items] == recorded["bob"]
        bob_sessions = await store.search_sessions(SINGLE_USER, TraceQuery(project_id="p1", user_id="bob"))
        assert [s.id for s in bob_sessions.items] == ["conversation-2"]

    async def test_a_trace_recorded_inside_a_runs_mlflow_run_belongs_to_the_run(
        self, store: EvalStore, tracking_uri: str, monkeypatch
    ) -> None:
        import mlflow

        project = await store.create_project(SINGLE_USER, Project(id="p1", name="Support agent"))
        run = await store.start_run(SINGLE_USER, Run(id="r1", project_id="p1", source="local", state="running"))
        tagged = await store.record_trace(
            SINGLE_USER, Trace(id="case-1", project_id="p1", name="case 1", run_id="r1", request="hi")
        )
        recorded: dict[str, str] = {}
        with application_tracing(monkeypatch, tracking_uri, project):

            def call(name: str) -> None:
                with mlflow.start_span(name=name) as span:
                    span.set_inputs({"message": name})
                recorded[name] = span.trace_id

            # MLflow tracing names the active MLflow run in the trace's `mlflow.sourceRun`.
            with mlflow.start_run(run_id=run.store.run_id):
                call("in the run")
            with mlflow.start_run():  # the application's own MLflow run, which is no run of ours
                call("in its own run")
            call("in no run")

        found = await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1"))
        assert {t.id: t.run_id for t in found.items} == {
            tagged.id: "r1",
            recorded["in the run"]: "r1",
            recorded["in its own run"]: None,
            recorded["in no run"]: None,
        }
        in_run = await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1", run_id="r1", oldest_first=True))
        assert [t.id for t in in_run.items] == [tagged.id, recorded["in the run"]]
        read = await store.get_trace(SINGLE_USER, "p1", recorded["in the run"])
        assert read is not None and read.run_id == "r1" and [s.name for s in read.spans] == ["in the run"]
        own = await store.get_trace(SINGLE_USER, "p1", recorded["in its own run"])
        assert own is not None and own.run_id is None
        assert (await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1", run_id="r2"))).items == []


def artifact_root_of_length(tmp_path: Path, length: int) -> Path:
    """A folder under ``tmp_path`` whose absolute path is ``length`` characters long."""
    root = tmp_path.resolve()
    while (missing := length - len(str(root)) - 1) > 60:
        root = root / ("a" * 50)
    if missing > 0:
        root = root / ("a" * missing)
    assert len(str(root)) == length, "the test's temporary folder is longer than the root it needs"
    return root


async def artifact_uri(store: EvalStore, project_id: str, run_id: str) -> str:
    """The artifact location MLflow keeps for the run."""
    from martian.evals.store.mlflow_local import MlflowStore

    assert isinstance(store, MlflowStore)
    experiment = await store._call(ORG_A, lambda: store._experiment(project_id))
    found = await store._call(ORG_A, lambda: store._mlflow_run(experiment.experiment_id, run_id))
    return str(found.info.artifact_uri)


class ArtifactRootContract:
    """Where the binding keeps a run's artifacts. A binding's class provides ``store_at``: a
    function opening the store on the class's database with the artifact root it is given."""

    async def test_a_run_starts_with_the_longest_artifact_root(
        self, store_at: Callable[[Path], EvalStore], tmp_path
    ) -> None:
        root = artifact_root_of_length(tmp_path, LONGEST_ARTIFACT_ROOT)
        store = store_at(root)
        # An organisation's MLflow workspace has the longest name, 44 characters.
        await store.create_project(ORG_A, Project(id="p1", name="Agent"))
        run = await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        await store.put_artifact(ORG_A, "p1", "r1", "summary.json", b'{"passed": 1}')
        finished = await store.finish_run(ORG_A, "p1", "r1", state="completed", counts=Counts(cases=1, passed=1))

        assert await store.get_run(ORG_A, "p1", "r1") == finished
        assert await store.get_artifact(ORG_A, "p1", "r1", "summary.json") == b'{"passed": 1}'
        location = await artifact_uri(store, "p1", "r1")
        assert location == f"{root}/{run.store.run_id}/artifacts"
        assert len(location) == 200

    async def test_runs_written_with_the_earlier_artifact_layout_still_read(
        self, store_at: Callable[[Path], EvalStore], tmp_path, monkeypatch
    ) -> None:
        """An earlier release kept a project's experiment artifacts in MLflow's own layout,
        ``workspaces/<workspace>/<experiment id>`` under the root. Such an experiment keeps that
        folder, for the runs it already has and for new ones."""
        from mlflow.store.tracking.sqlalchemy_workspace_store import WorkspaceAwareSqlAlchemyStore

        from martian.evals._mlflow import WorkspaceStore
        from martian.evals.store.mlflow_local import workspace_name

        root = tmp_path / "artifacts"
        with monkeypatch.context() as earlier:
            earlier.setattr(
                WorkspaceStore, "_get_artifact_location", WorkspaceAwareSqlAlchemyStore._get_artifact_location
            )
            before = store_at(root)
            project = await before.create_project(ORG_A, Project(id="p1", name="Agent"))
            old = await before.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
            await before.put_artifact(ORG_A, "p1", "r1", "summary.json", b"old")
        experiment_folder = root.resolve() / "workspaces" / workspace_name(ORG_A) / str(project.store.experiment_id)
        assert await artifact_uri(before, "p1", "r1") == f"{experiment_folder}/{old.store.run_id}/artifacts"

        store = store_at(root)
        assert await store.get_run(ORG_A, "p1", "r1") == old
        assert await store.get_artifact(ORG_A, "p1", "r1", "summary.json") == b"old"
        assert await store.list_artifacts(ORG_A, "p1", "r1") == ["summary.json"]
        finished = await store.finish_run(ORG_A, "p1", "r1", state="completed", counts=Counts(cases=1, passed=1))
        assert await store.get_run(ORG_A, "p1", "r1") == finished

        # A new run in the earlier experiment goes in that experiment's folder, as MLflow places it.
        second = await store.start_run(ORG_A, Run(id="r2", project_id="p1", source="local", state="running"))
        await store.put_artifact(ORG_A, "p1", "r2", "summary.json", b"new")
        assert await store.get_artifact(ORG_A, "p1", "r2", "summary.json") == b"new"
        assert await artifact_uri(store, "p1", "r2") == f"{experiment_folder}/{second.store.run_id}/artifacts"
        # A new project's runs go in the root itself.
        await store.create_project(ORG_A, Project(id="p2", name="Other agent"))
        later = await store.start_run(ORG_A, Run(id="r3", project_id="p2", source="local", state="running"))
        assert await artifact_uri(store, "p2", "r3") == f"{root.resolve()}/{later.store.run_id}/artifacts"
        assert await store.get_run(ORG_A, "p2", "r3") == later


class TestMlflowStoreOnSqlite(StoreContract, MlflowTracingContract, ArtifactRootContract):
    @pytest.fixture
    def tracking_uri(self, tmp_path) -> str:
        return f"sqlite:///{tmp_path / 'mlflow.db'}"

    @pytest.fixture
    def store_at(self, tracking_uri) -> Callable[[Path], EvalStore]:
        from martian.evals.store.mlflow_local import MlflowStore

        return lambda root: MlflowStore(tracking_uri, root)

    @pytest.fixture
    def store(self, tmp_path, store_at) -> EvalStore:
        return store_at(tmp_path / "artifacts")

    async def test_sqlite_takes_an_artifact_root_longer_than_postgres_allows(self, store_at, tmp_path) -> None:
        """SQLite does not enforce the length of MLflow's ``runs.artifact_uri``."""
        store = store_at(artifact_root_of_length(tmp_path, LONGEST_ARTIFACT_ROOT + 1))
        await store.create_project(ORG_A, Project(id="p1", name="Agent"))
        run = await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        assert await store.get_run(ORG_A, "p1", "r1") == run


@pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to run the store contract on Postgres")
@pytest.mark.usefixtures("unpooled")
class TestMlflowStoreOnPostgres(StoreContract, MlflowTracingContract, ArtifactRootContract):
    @pytest.fixture
    def schema(self) -> str:
        return f"mlflow_test_{uuid.uuid4().hex[:10]}"

    @pytest.fixture
    def tracking_uri(self, schema) -> str:
        from martian.evals._sql import postgres_url

        assert POSTGRES_URL is not None
        return postgres_url(POSTGRES_URL, schema)

    @pytest.fixture
    def store_at(self, schema) -> Iterator[Callable[[Path], EvalStore]]:
        from sqlalchemy import create_engine, text

        from martian.evals.store.mlflow_local import MlflowStore

        assert POSTGRES_URL is not None
        yield lambda root: MlflowStore(POSTGRES_URL, root, schema=schema)
        engine = create_engine(POSTGRES_URL)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()

    @pytest.fixture
    def store(self, tmp_path, store_at) -> EvalStore:
        return store_at(tmp_path / "artifacts")

    def test_postgres_refuses_an_artifact_root_too_long_for_mlflow_before_writing(
        self, store_at, schema, tmp_path
    ) -> None:
        from sqlalchemy import create_engine, text

        root = artifact_root_of_length(tmp_path, LONGEST_ARTIFACT_ROOT + 1)
        with pytest.raises(ValueError) as refused:
            store_at(root)
        message = str(refused.value)
        assert "artifact_root" in message and f"at most {LONGEST_ARTIFACT_ROOT} characters" in message
        assert f"{LONGEST_ARTIFACT_ROOT + 1} characters" in message
        assert not root.exists()
        engine = create_engine(POSTGRES_URL)
        try:
            with engine.begin() as connection:
                found = connection.execute(
                    text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"), {"schema": schema}
                ).first()
            assert found is None, "the store created its schema before refusing the root"
        finally:
            engine.dispose()


async def test_the_binding_never_turns_workspaces_on_for_the_whole_process(tmp_path):
    from martian.evals.store.mlflow_local import MlflowStore

    store = MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path / "artifacts")
    await store.create_project(ORG_A, Project(id="p1", name="Agent"))
    assert "MLFLOW_ENABLE_WORKSPACES" not in os.environ
    assert "MLFLOW_WORKSPACE" not in os.environ


async def test_each_organisation_and_the_local_user_get_their_own_workspace(tmp_path):
    from martian.evals.store.mlflow_local import MlflowStore

    store = MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path / "artifacts")
    org = organisation_principal("u", "Org With Spaces/And:Symbols", frozenset())
    names = {store._workspace(p) for p in (SINGLE_USER, org, ORG_A)}
    assert len(names) == 3 and "local" in names
    assert all(n == "local" or (n.startswith("org-") and len(n) == 44) for n in names)
    assert store._workspace(org) == store._workspace(
        organisation_principal("other user", "Org With Spaces/And:Symbols", frozenset())
    )


@pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to check the schema on Postgres")
@pytest.mark.usefixtures("unpooled")
async def test_mlflow_tables_live_in_their_own_postgres_schema(tmp_path):
    from sqlalchemy import create_engine, text

    from martian.evals.store.mlflow_local import MlflowStore

    schema = f"mlflow_test_{uuid.uuid4().hex[:10]}"
    store = MlflowStore(POSTGRES_URL, tmp_path / "artifacts", schema=schema)
    await store.create_project(ORG_A, Project(id="p1", name="Agent"))
    engine = create_engine(POSTGRES_URL)
    try:
        with engine.begin() as connection:
            tables = connection.execute(
                text("SELECT table_schema, table_name FROM information_schema.tables WHERE table_name = 'experiments'")
            ).all()
            assert (schema, "experiments") in tables
            assert ("public", "experiments") not in tables
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()
