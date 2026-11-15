"""Monitors: scorers on a project's live traces, the traces each took, and queue rows due later.

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

MONITORS = "martian_monitors"
TRACES = "martian_monitored_traces"
QUEUE = "martian_work_queue"


def upgrade() -> None:
    op.create_table(
        MONITORS,
        sa.Column("space", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.Column("progress", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name=f"pk_{MONITORS}"),
    )
    op.create_table(
        TRACES,
        sa.Column("space", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("monitor_id", sa.String(200), nullable=False),
        sa.Column("trace_id", sa.String(200), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("pass_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "monitor_id", "trace_id", name=f"pk_{TRACES}"),
    )
    op.create_index("ix_martian_monitored_traces_created", TRACES, ["space", "project_id", "monitor_id", "created_at"])
    # A monitor's row in the queue is taken again each time it is due, not before.
    op.add_column(QUEUE, sa.Column("available_at", sa.DateTime(timezone=True), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        # As for the tables of 0001: row-level security filters on the transaction's space again.
        for table in (MONITORS, TRACES):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY {table}_space ON {table} "
                "USING (space = current_setting('martian.space', true)) "
                "WITH CHECK (space = current_setting('martian.space', true))"
            )


def downgrade() -> None:
    with op.batch_alter_table(QUEUE) as batch:
        batch.drop_column("available_at")
    op.drop_index("ix_martian_monitored_traces_created", table_name=TRACES)
    op.drop_table(TRACES)
    op.drop_table(MONITORS)
