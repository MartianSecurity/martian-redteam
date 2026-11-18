"""The evals core's tables: runs, case results, scores, scoring jobs, reviews, suites and dataset drafts.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SPACE = 80
ID = 200
TABLES = (
    "martian_runs",
    "martian_case_results",
    "martian_scores",
    "martian_scoring_jobs",
    "martian_reviews",
    "martian_suites",
    "martian_dataset_drafts",
    "martian_dataset_draft_cases",
)


def _key(*names: str) -> list[sa.Column[Any]]:
    columns: list[sa.Column[Any]] = [
        sa.Column("space", sa.String(SPACE), nullable=False),
        sa.Column("project_id", sa.String(ID), nullable=False),
    ]
    for name in names:
        kind: sa.types.TypeEngine[Any] = sa.Integer() if name == "version" else sa.String(ID)
        columns.append(sa.Column(name, kind, nullable=False))
    return columns


def upgrade() -> None:
    op.create_table(
        "martian_runs",
        *_key("id"),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name="pk_martian_runs"),
    )
    op.create_index("ix_martian_runs_created", "martian_runs", ["space", "project_id", "created_at"])
    op.create_table(
        "martian_case_results",
        *_key("run_id", "case_id"),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "run_id", "case_id", name="pk_martian_case_results"),
    )
    op.create_index(
        "ix_martian_case_results_position",
        "martian_case_results",
        ["space", "project_id", "run_id", "position"],
    )
    op.create_table(
        "martian_scores",
        *_key("run_id", "case_id", "scorer", "scoring_job_id"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint(
            "space", "project_id", "run_id", "case_id", "scorer", "scoring_job_id", name="pk_martian_scores"
        ),
    )
    op.create_table(
        "martian_scoring_jobs",
        *_key("id"),
        sa.Column("run_id", sa.String(ID), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name="pk_martian_scoring_jobs"),
    )
    op.create_index("ix_martian_scoring_jobs_run", "martian_scoring_jobs", ["space", "project_id", "run_id"])
    op.create_table(
        "martian_reviews",
        *_key("id"),
        sa.Column("run_id", sa.String(ID), nullable=False),
        sa.Column("case_id", sa.String(ID), nullable=False),
        sa.Column("previous_id", sa.String(ID), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name="pk_martian_reviews"),
        sa.UniqueConstraint("space", "project_id", "previous_id", name="uq_martian_reviews_previous"),
    )
    op.create_index("ix_martian_reviews_case", "martian_reviews", ["space", "project_id", "run_id", "case_id"])
    op.create_table(
        "martian_suites",
        *_key("id", "version"),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", "version", name="pk_martian_suites"),
    )
    op.create_table(
        "martian_dataset_drafts",
        *_key("id"),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("published", sa.Boolean, nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "id", name="pk_martian_dataset_drafts"),
    )
    op.create_table(
        "martian_dataset_draft_cases",
        *_key("draft_id", "case_id"),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "draft_id", "case_id", name="pk_martian_dataset_draft_cases"),
    )
    op.create_index(
        "ix_martian_draft_cases_ordinal",
        "martian_dataset_draft_cases",
        ["space", "project_id", "draft_id", "ordinal"],
    )
    if op.get_bind().dialect.name == "postgresql":
        # The store filters every query on space. Row-level security filters again, against the
        # space the store sets for its transaction, and applies to the tables' owner too.
        for table in TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY {table}_space ON {table} "
                "USING (space = current_setting('martian.space', true)) "
                "WITH CHECK (space = current_setting('martian.space', true))"
            )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
