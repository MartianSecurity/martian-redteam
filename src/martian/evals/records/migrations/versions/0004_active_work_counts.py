"""Indexes for counting an organisation's queued and running runs and scoring jobs.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_martian_runs_state", "martian_runs", ["space", "state"])
    op.create_index("ix_martian_scoring_jobs_state", "martian_scoring_jobs", ["space", "state"])


def downgrade() -> None:
    op.drop_index("ix_martian_scoring_jobs_state", table_name="martian_scoring_jobs")
    op.drop_index("ix_martian_runs_state", table_name="martian_runs")
