"""The in-process mlflow-skinny binding passes the store contract on SQLite, and on Postgres when
MARTIAN_TEST_POSTGRES_URL names a database it may create schemas in."""

import os
import time
import uuid

import pytest

from martian.evals.model import Project, TraceQuery
from martian.evals.server.principal import SINGLE_USER, organisation_principal
from martian.evals.store import EvalStore
from tests.evals.store_contract import ORG_A, StoreContract

POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)


class MlflowTracingContract:
    """Traces an application records with MLflow tracing itself, read through the binding. A
    binding's class provides ``tracking_uri``: the database the store keeps MLflow's tables in."""

    async def test_traces_recorded_with_mlflow_tracing_form_sessions_and_turns(
        self, store: EvalStore, tracking_uri: str, monkeypatch
    ) -> None:
        import mlflow

        project = await store.create_project(SINGLE_USER, Project(id="p1", name="Support agent"))
        # How an application records traces into the local user's workspace with MLflow tracing.
        monkeypatch.setenv("MLFLOW_ENABLE_WORKSPACES", "true")
        monkeypatch.setenv("MLFLOW_WORKSPACE", "local")
        monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
        old_uri = mlflow.get_tracking_uri()
        # MLflow also writes these two to the environment; monkeypatch puts them back afterwards.
        monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
        monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", str(project.store.experiment_id))
        mlflow.set_tracking_uri(tracking_uri)
        recorded: dict[str, list[str]] = {}
        try:
            mlflow.set_experiment(experiment_id=project.store.experiment_id)

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
            mlflow.flush_trace_async_logging()
        finally:
            mlflow.set_tracking_uri(old_uri)
        monkeypatch.delenv("MLFLOW_ENABLE_WORKSPACES")
        monkeypatch.delenv("MLFLOW_WORKSPACE")

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


class TestMlflowStoreOnSqlite(StoreContract, MlflowTracingContract):
    @pytest.fixture
    def tracking_uri(self, tmp_path) -> str:
        return f"sqlite:///{tmp_path / 'mlflow.db'}"

    @pytest.fixture
    def store(self, tmp_path, tracking_uri) -> EvalStore:
        from martian.evals.store.mlflow_local import MlflowStore

        return MlflowStore(tracking_uri, tmp_path / "artifacts")


@pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to run the store contract on Postgres")
class TestMlflowStoreOnPostgres(StoreContract, MlflowTracingContract):
    @pytest.fixture
    def schema(self) -> str:
        return f"mlflow_test_{uuid.uuid4().hex[:10]}"

    @pytest.fixture
    def tracking_uri(self, schema) -> str:
        from martian.evals._sql import postgres_url

        assert POSTGRES_URL is not None
        return postgres_url(POSTGRES_URL, schema)

    @pytest.fixture
    def store(self, tmp_path, schema):
        from sqlalchemy import create_engine, text

        from martian.evals.store.mlflow_local import MlflowStore

        yield MlflowStore(POSTGRES_URL, tmp_path / "artifacts", schema=schema)
        engine = create_engine(POSTGRES_URL)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
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
