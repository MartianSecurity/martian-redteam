"""The worker's queue: run launches and scoring jobs waiting for a worker, or held by one.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLE = "martian_work_queue"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("space", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("work", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name="pk_martian_work_queue"),
    )
    op.create_index("ix_martian_work_queue_next", TABLE, ["state", "created_at"])
    if op.get_bind().dialect.name == "postgresql":
        # As for the other tables, row-level security filters on the transaction's space again. A
        # worker takes the next work of any organisation, so the claim, and nothing else, also sets
        # martian.claim for its own transaction (martian.evals.records.queue).
        op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
        allowed = "(space = current_setting('martian.space', true) OR current_setting('martian.claim', true) = 'on')"
        op.execute(f"CREATE POLICY {TABLE}_space ON {TABLE} USING {allowed} WITH CHECK {allowed}")


def downgrade() -> None:
    op.drop_index("ix_martian_work_queue_next", table_name=TABLE)
    op.drop_table(TABLE)
