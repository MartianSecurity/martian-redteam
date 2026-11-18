"""Database handling shared by the stores: SQLite, or Postgres with a schema of the store's own, and
what a store says when its database refuses a statement."""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.exc import DataError, DBAPIError, IntegrityError

from martian.evals._digits import ascii_digits
from martian.evals.store import StoreError

_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CODE = re.compile(r"[A-Z0-9_]+")
_STATEMENT_TABLE = re.compile(r'\b(?:INTO|UPDATE|FROM)\s+"?([A-Za-z_][A-Za-z0-9_]*)"?', re.IGNORECASE)
_DUPLICATE_KEY = frozenset({"23505", "SQLITE_CONSTRAINT_PRIMARYKEY", "SQLITE_CONSTRAINT_UNIQUE"})


def is_postgres(database_url: str) -> bool:
    return urlsplit(database_url).scheme.split("+")[0] == "postgresql"


def check_schema(schema: str) -> str:
    if not _SCHEMA.fullmatch(schema):
        raise ValueError("schema must be a lowercase identifier")
    return schema


def postgres_url(database_url: str, schema: str) -> str:
    """The URL with libpq's search path set to the schema, so unqualified tables live there."""
    parts = urlsplit(database_url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
    query.append(("options", f"-csearch_path={check_schema(schema)}"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def create_schema(database_url: str, schema: str) -> None:
    """Create the schema if it does not exist. An existing schema is left alone, so a role that may
    not create schemas can use one an administrator made for it: Postgres checks that privilege
    even for ``CREATE SCHEMA IF NOT EXISTS``."""
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"), {"schema": check_schema(schema)}
            ).first()
            if exists is None:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        engine.dispose()


def operation_name(work: Callable[..., object]) -> str:
    """The store method ``work`` was defined in: ``record_trace`` for the function
    ``MlflowStore.record_trace`` hands to MLflow."""
    return work.__qualname__.split(".<locals>.")[0].rsplit(".", 1)[-1]


def database_cause(error: BaseException) -> DBAPIError | None:
    """The database's error that ``error`` was raised from, such as the one under an MLflow error."""
    found = error.__cause__ or error.__context__
    for _ in range(10):
        if found is None or isinstance(found, DBAPIError):
            return found
        found = found.__cause__ or found.__context__
    return None


def database_refusal(operation: str, error: DBAPIError) -> StoreError:
    """The store's error for a statement the database refused or failed. It names the operation,
    the table (and the column or constraint when Postgres names one) and the driver's error class
    and code, never the statement's values nor the database's message: both can quote what was
    written, such as a request's text, and Postgres repeats a duplicate key's values.

    A duplicate key is a ``conflict``; any other refusal, and any other failure, is ``invalid``."""
    original = error.orig
    diagnosis = getattr(original, "diag", None)  # psycopg's
    column = getattr(diagnosis, "column_name", None)
    constraint = getattr(diagnosis, "constraint_name", None)
    code = getattr(original, "sqlstate", None) or getattr(original, "sqlite_errorname", None)  # psycopg, sqlite3
    where = ""
    if table := _STATEMENT_TABLE.search(error.statement or ""):
        where = table.group(1)
        if isinstance(column, str) and _NAME.fullmatch(column):
            where += f".{column}"
    reason = [type(original).__name__]
    if isinstance(code, str) and _CODE.fullmatch(code):
        reason.append(f"SQLSTATE {code}" if ascii_digits(code[0]) else code)
    if isinstance(constraint, str) and _NAME.fullmatch(constraint):
        reason.append(f"constraint {constraint}")
    said, table_name = ", ".join(reason), where or "a table"
    if isinstance(error, IntegrityError) and code in _DUPLICATE_KEY:
        return StoreError("conflict", f"{operation}: the database refused a duplicate key in {table_name} ({said})")
    if isinstance(error, DataError | IntegrityError):
        return StoreError("invalid", f"{operation}: the database refused a value for {table_name} ({said})")
    return StoreError("invalid", f"{operation}: the database failed on {table_name} ({said})")
