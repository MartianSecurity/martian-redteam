"""The in-process mlflow-skinny binding passes the store contract on SQLite, and on Postgres when
MARTIAN_TEST_POSTGRES_URL names a database it may create schemas in."""

import os
import uuid

import pytest

from martian.evals.model import Project
from martian.evals.server.principal import SINGLE_USER, organisation_principal
from martian.evals.store import EvalStore
from tests.evals.store_contract import ORG_A, StoreContract

POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)


class TestMlflowStoreOnSqlite(StoreContract):
    @pytest.fixture
    def store(self, tmp_path) -> EvalStore:
        from martian.evals.store.mlflow_local import MlflowStore

        return MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path / "artifacts")


@pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to run the store contract on Postgres")
class TestMlflowStoreOnPostgres(StoreContract):
    @pytest.fixture
    def store(self, tmp_path):
        from sqlalchemy import create_engine, text

        from martian.evals.store.mlflow_local import MlflowStore

        schema = f"mlflow_test_{uuid.uuid4().hex[:10]}"
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
