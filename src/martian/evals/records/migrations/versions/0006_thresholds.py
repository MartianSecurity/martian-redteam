"""Thresholds on monitor scores, their state, and the alerts their changes of state raise.

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

THRESHOLDS = "martian_thresholds"
ALERTS = "martian_threshold_alerts"


def upgrade() -> None:
    op.create_table(
        THRESHOLDS,
        sa.Column("space", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.Column("status", sa.JSON, nullable=False),
        sa.Column("secret", sa.String(256), nullable=True),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name=f"pk_{THRESHOLDS}"),
    )
    op.create_index("ix_martian_thresholds_source", THRESHOLDS, ["space", "project_id", "source_kind", "source_id"])
    op.create_table(
        ALERTS,
        sa.Column("space", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("threshold_id", sa.String(200), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name=f"pk_{ALERTS}"),
    )
    op.create_index(
        "ix_martian_threshold_alerts_due", ALERTS, ["space", "project_id", "threshold_id", "state", "next_attempt_at"]
    )
    op.create_index(
        "ix_martian_threshold_alerts_created", ALERTS, ["space", "project_id", "threshold_id", "created_at"]
    )
    if op.get_bind().dialect.name == "postgresql":
        # As for the tables of 0001: row-level security filters on the transaction's space again.
        for table in (THRESHOLDS, ALERTS):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY {table}_space ON {table} "
                "USING (space = current_setting('martian.space', true)) "
                "WITH CHECK (space = current_setting('martian.space', true))"
            )


def downgrade() -> None:
    op.drop_index("ix_martian_threshold_alerts_created", table_name=ALERTS)
    op.drop_index("ix_martian_threshold_alerts_due", table_name=ALERTS)
    op.drop_table(ALERTS)
    op.drop_index("ix_martian_thresholds_source", table_name=THRESHOLDS)
    op.drop_table(THRESHOLDS)
