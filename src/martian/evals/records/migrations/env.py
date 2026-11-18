"""Alembic environment for the evals core's tables. Run through ``martian.evals.records.sql``, which
hands over an open connection; there is no ``alembic.ini``."""

from __future__ import annotations

from alembic import context

from martian.evals.records.tables import metadata

VERSION_TABLE = "martian_alembic_version"

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=metadata,
    version_table=VERSION_TABLE,
    render_as_batch=connection.dialect.name == "sqlite",
)
with context.begin_transaction():
    context.run_migrations()
