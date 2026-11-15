"""A score's source joins its key: a person's answer named like a check or a judge is a score of its
own, beside that scorer's, never replacing it (MartianSecurity/martian-notes#345).

The upgrade fills ``source`` from each score's record. The downgrade drops it again, and a case can
then keep one score per scorer and scoring job: where scores of one name differ only by source, a
judge's stays, else a check's, and a person's goes (its review still holds the answer).

On Postgres row-level security applies to the tables' owner, which runs the migrations, and no
organisation is set while they run, so the table stops forcing it while every organisation's rows
are rewritten, in the same transaction.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TABLE = "martian_scores"
PRIMARY_KEY = "pk_martian_scores"
KEY = ["space", "project_id", "run_id", "case_id", "scorer", "scoring_job_id"]


def _rank(table: str) -> str:
    """Which of a key's scores the downgrade keeps: the lowest rank."""
    return f"CASE {table}.source WHEN 'llm_judge' THEN 0 WHEN 'code' THEN 1 ELSE 2 END"


def _force_row_level_security(force: bool) -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {TABLE} {'' if force else 'NO '}FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _force_row_level_security(False)
    op.add_column(TABLE, sa.Column("source", sa.String(16), nullable=True))
    postgres = op.get_bind().dialect.name == "postgresql"
    source = "record ->> 'source'" if postgres else "json_extract(record, '$.source')"
    op.execute(f"UPDATE {TABLE} SET source = {source}")
    with op.batch_alter_table(TABLE) as batch:
        batch.alter_column("source", existing_type=sa.String(16), nullable=False)
        batch.drop_constraint(PRIMARY_KEY, type_="primary")
        batch.create_primary_key(PRIMARY_KEY, [*KEY, "source"])
    _force_row_level_security(True)


def downgrade() -> None:
    _force_row_level_security(False)
    same_key = " AND ".join(f"kept.{column} = {TABLE}.{column}" for column in KEY)
    op.execute(
        f"DELETE FROM {TABLE} WHERE EXISTS "
        f"(SELECT 1 FROM {TABLE} AS kept WHERE {same_key} AND {_rank('kept')} < {_rank(TABLE)})"
    )
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(PRIMARY_KEY, type_="primary")
        batch.create_primary_key(PRIMARY_KEY, KEY)
        batch.drop_column("source")
    _force_row_level_security(True)
