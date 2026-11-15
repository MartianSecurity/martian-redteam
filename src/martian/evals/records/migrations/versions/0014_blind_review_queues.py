"""Review queues with blind double review.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

TABLE = "martian_blind_queues"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("space", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("run_id", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name="pk_martian_blind_queues"),
    )
    op.create_index("ix_martian_blind_queues_run", TABLE, ["space", "project_id", "run_id"])
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
