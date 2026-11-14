"""The evals core's own tables: what MLflow does not keep. Portable to SQLite and Postgres.

Each row keeps the whole model object as JSON in ``record``, plus the columns queries filter and
order on. ``space`` is the organisation the row belongs to (``~local`` for the single local
user); every query filters on it, and on Postgres row-level security filters on it again.
The migrations in ``migrations/versions`` create exactly these tables; a test compares them.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

# Every name is spelled out and under Postgres's 63-character limit.
metadata = MetaData(naming_convention={"pk": "pk_%(table_name)s"})

SPACE = 80
ID = 200

runs = Table(
    "martian_runs",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("state", String(20), nullable=False),
    Column("source", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_runs_created", "space", "project_id", "created_at"),
)

case_results = Table(
    "martian_case_results",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("run_id", String(ID), primary_key=True),
    Column("case_id", String(ID), primary_key=True),
    Column("position", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_case_results_position", "space", "project_id", "run_id", "position"),
)

scores = Table(
    "martian_scores",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("run_id", String(ID), primary_key=True),
    Column("case_id", String(ID), primary_key=True),
    Column("scorer", String(ID), primary_key=True),
    # Empty for a run's own scores; a scoring job's scores sit beside them, never replacing them.
    Column("scoring_job_id", String(ID), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
)

scoring_jobs = Table(
    "martian_scoring_jobs",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("run_id", String(ID), nullable=False),
    Column("state", String(20), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_scoring_jobs_run", "space", "project_id", "run_id"),
)

reviews = Table(
    "martian_reviews",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("run_id", String(ID), nullable=False),
    Column("case_id", String(ID), nullable=False),
    Column("previous_id", String(ID), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
    # A review is replaced at most once, so a chain of answers never forks.
    UniqueConstraint("space", "project_id", "previous_id", name="uq_martian_reviews_previous"),
    Index("ix_martian_reviews_case", "space", "project_id", "run_id", "case_id"),
)

suites = Table(
    "martian_suites",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("digest", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
)

dataset_drafts = Table(
    "martian_dataset_drafts",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("published", Boolean, nullable=False),
    Column("record", JSON, nullable=False),
)

dataset_draft_cases = Table(
    "martian_dataset_draft_cases",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("draft_id", String(ID), primary_key=True),
    Column("case_id", String(ID), primary_key=True),
    Column("ordinal", Integer, nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_draft_cases_ordinal", "space", "project_id", "draft_id", "ordinal"),
)

TABLES = (runs, case_results, scores, scoring_jobs, reviews, suites, dataset_drafts, dataset_draft_cases)
