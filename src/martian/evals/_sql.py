"""Database URL handling shared by the stores: SQLite, or Postgres with a schema of the store's own."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}")


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
