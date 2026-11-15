"""The latest audit of each saved suite version.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TABLE = "martian_suite_audits"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("space", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("suite_id", sa.String(200), nullable=False),
        sa.Column("suite_version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "suite_id", "suite_version", name="pk_martian_suite_audits"),
    )
    if op.get_bind().dialect.name == "postgresql":
        # As for the tables of 0001: row-level security filters on the transaction's space again.
        op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {TABLE}_space ON {TABLE} "
            "USING (space = current_setting('martian.space', true)) "
            "WITH CHECK (space = current_setting('martian.space', true))"
        )


def downgrade() -> None:
    op.drop_table(TABLE)
