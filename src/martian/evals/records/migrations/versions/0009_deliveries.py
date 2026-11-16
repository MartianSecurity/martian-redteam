"""The outbox: store writes kept until they are delivered (martian.evals.outbox).

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLE = "martian_deliveries"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("space", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name="pk_martian_deliveries"),
    )
    op.create_index("ix_martian_deliveries_due", TABLE, ["space", "state", "next_attempt_at"])
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
    op.drop_index("ix_martian_deliveries_due", TABLE)
    op.drop_table(TABLE)
