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
    Index("ix_martian_runs_state", "space", "state"),
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
    # The score's source (migration 0012): a person's answer named like a check or a judge sits
    # beside that scorer's score, never replacing it.
    Column("source", String(16), primary_key=True),
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
    Index("ix_martian_scoring_jobs_state", "space", "state"),
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

# The latest audit of each saved suite version (martian.evals.audit).
suite_audits = Table(
    "martian_suite_audits",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("suite_id", String(ID), primary_key=True),
    Column("suite_version", Integer, primary_key=True),
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

saved_views = Table(
    "martian_saved_views",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("record", JSON, nullable=False),
)

# Monitors (migration 0005): ``record`` is the monitor as its routes write it, ``progress`` how far
# the worker has read the project's traces, which only the worker writes.
monitors = Table(
    "martian_monitors",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("record", JSON, nullable=False),
    Column("progress", JSON, nullable=False),
)

# Each trace a monitor took for scoring, once: the key is what keeps a monitor from scoring a trace twice.
monitored_traces = Table(
    "martian_monitored_traces",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("monitor_id", String(ID), primary_key=True),
    Column("trace_id", String(ID), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("pass_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_monitored_traces_created", "space", "project_id", "monitor_id", "created_at"),
)

# Thresholds (migration 0006): ``record`` is the threshold as its route writes it, ``status`` its
# state machine, which only its evaluations write, with ``sequence`` the number of its changes of
# state that a change is conditioned on. ``secret`` signs its webhook alerts; no route reads it.
thresholds = Table(
    "martian_thresholds",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("source_kind", String(16), nullable=False),
    Column("source_id", String(ID), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("record", JSON, nullable=False),
    Column("status", JSON, nullable=False),
    Column("secret", String(256), nullable=True),
    Index("ix_martian_thresholds_source", "space", "project_id", "source_kind", "source_id"),
)

# Each alert a threshold's change of state raised, one per channel, kept after delivery: the key is
# what keeps a change from raising an alert twice. A ``pending`` row is tried at ``next_attempt_at``.
threshold_alerts = Table(
    "martian_threshold_alerts",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("threshold_id", String(ID), nullable=False),
    Column("state", String(16), nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_threshold_alerts_due", "space", "project_id", "threshold_id", "state", "next_attempt_at"),
    Index("ix_martian_threshold_alerts_created", "space", "project_id", "threshold_id", "created_at"),
)

# Schedules (migration 0007): ``record`` is the schedule as its routes write it, ``progress`` when
# its next run is due, which only the worker writes.
schedules = Table(
    "martian_schedules",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("record", JSON, nullable=False),
    Column("progress", JSON, nullable=False),
)

# Each slot a schedule ran or skipped, once: the key is what keeps a slot from running twice.
schedule_runs = Table(
    "martian_schedule_runs",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("schedule_id", String(ID), primary_key=True),
    Column("slot", DateTime(timezone=True), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("run_id", String(ID), nullable=True),
    Column("record", JSON, nullable=False),
)

# The worker's queue (``martian.evals.records.queue``): one row per run launch or scoring job that
# no worker has finished, removed when one does, and one per monitor or schedule while it exists,
# taken again each time it is due. ``work`` is the work as the worker reads it. ``token`` is the current
# claim's, and the claim holds the row until ``lease_expires_at``. A row with ``available_at`` in
# the future is not taken before then (migration 0005).
work_queue = Table(
    "martian_work_queue",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("token", String(64), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("work", JSON, nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=True),
    Index("ix_martian_work_queue_next", "state", "created_at"),
)

# Dataset manifests (migration 0008): what publishing each dataset version wrote (``martian.evals.manifests``).
dataset_manifests = Table(
    "martian_dataset_manifests",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("dataset_id", String(ID), nullable=True),
    Column("state", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_dataset_manifests_dataset", "space", "project_id", "dataset_id"),
)

# The outbox (migration 0009): store writes kept until they are delivered (``martian.evals.outbox``).
deliveries = Table(
    "martian_deliveries",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("state", String(16), nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_deliveries_due", "space", "state", "next_attempt_at"),
)

# Judge calibrations (migration 0010): every calibration saved, newest of a judge version first.
judge_calibrations = Table(
    "martian_judge_calibrations",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("judge", String(ID), nullable=False),
    Column("version", String(40), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_judge_calibrations_judge", "space", "project_id", "judge", "version", "created_at"),
)

# Re-checks of calibrations (migration 0011): queued, then moved on by the worker.
calibration_rechecks = Table(
    "martian_calibration_rechecks",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("judge", String(ID), nullable=False),
    Column("version", String(40), nullable=False),
    Column("state", String(20), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_calibration_rechecks_judge", "space", "project_id", "judge", "created_at"),
)

# Review queues with blind double review (migration 0014): one row per queue, never changed.
blind_queues = Table(
    "martian_blind_queues",
    metadata,
    Column("space", String(SPACE), primary_key=True),
    Column("project_id", String(ID), primary_key=True),
    Column("id", String(ID), primary_key=True),
    Column("run_id", String(ID), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_martian_blind_queues_run", "space", "project_id", "run_id"),
)

TABLES = (
    runs,
    case_results,
    scores,
    scoring_jobs,
    reviews,
    suites,
    suite_audits,
    dataset_drafts,
    dataset_draft_cases,
    saved_views,
    work_queue,
    judge_calibrations,
    calibration_rechecks,
    blind_queues,
    monitors,
    monitored_traces,
    thresholds,
    threshold_alerts,
    schedules,
    schedule_runs,
    dataset_manifests,
    deliveries,
)
