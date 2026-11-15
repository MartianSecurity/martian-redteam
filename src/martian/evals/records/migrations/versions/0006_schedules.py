"""Schedules: suites run against a target on a schedule, and the slots each has run.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SCHEDULES = "martian_schedules"
RUNS = "martian_schedule_runs"


def upgrade() -> None:
    op.create_table(
        SCHEDULES,
        sa.Column("space", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.Column("progress", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name=f"pk_{SCHEDULES}"),
    )
    op.create_table(
        RUNS,
        sa.Column("space", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("schedule_id", sa.String(200), nullable=False),
        sa.Column("slot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("run_id", sa.String(200), nullable=True),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "schedule_id", "slot", name=f"pk_{RUNS}"),
    )
    if op.get_bind().dialect.name == "postgresql":
        # As for the tables of 0001: row-level security filters on the transaction's space again.
        for table in (SCHEDULES, RUNS):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY {table}_space ON {table} "
                "USING (space = current_setting('martian.space', true)) "
                "WITH CHECK (space = current_setting('martian.space', true))"
            )


def downgrade() -> None:
    op.drop_table(RUNS)
    op.drop_table(SCHEDULES)
