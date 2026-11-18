"""Red team runs: a run's kind, and the records only a red team run has (MartianSecurity/martian-notes#228).

A red team run is an eval run of kind ``redteam``: its attempts are case results and its checks are
scores. ``martian_runs.kind`` tells the kinds apart, so a listing shows one kind at a time; every run
recorded before this migration is an eval run. Four tables keep what only red team has: probes,
evidence, verifications and findings, each keyed to its run.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

SPACE = 80
ID = 200


def _record_table(name: str, key: str, *extra: sa.Column[int] | sa.Column[str]) -> None:
    op.create_table(
        name,
        sa.Column("space", sa.String(SPACE), nullable=False),
        sa.Column("project_id", sa.String(ID), nullable=False),
        sa.Column("run_id", sa.String(ID), nullable=False),
        sa.Column(key, sa.String(ID), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        *extra,
        sa.Column("record", sa.JSON, nullable=False),
        sa.PrimaryKeyConstraint("space", "project_id", "run_id", key, name=f"pk_{name}"),
    )
    if op.get_bind().dialect.name == "postgresql":
        # As for the tables of 0001: row-level security filters on the transaction's space again.
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {name}_space ON {name} "
            "USING (space = current_setting('martian.space', true)) "
            "WITH CHECK (space = current_setting('martian.space', true))"
        )


def upgrade() -> None:
    with op.batch_alter_table("martian_runs") as runs:
        runs.add_column(sa.Column("kind", sa.String(16), nullable=False, server_default="eval"))
    op.create_index("ix_martian_runs_kind", "martian_runs", ["space", "project_id", "kind", "created_at"])
    _record_table("martian_redteam_probes", "id")
    _record_table("martian_redteam_evidence", "id")
    _record_table("martian_redteam_verifications", "probe_id")
    _record_table(
        "martian_redteam_findings",
        "id",
        sa.Column("level", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
    )
    op.create_index("ix_martian_redteam_findings_status", "martian_redteam_findings", ["space", "project_id", "status"])


def downgrade() -> None:
    for name in (
        "martian_redteam_findings",
        "martian_redteam_verifications",
        "martian_redteam_evidence",
        "martian_redteam_probes",
    ):
        op.drop_table(name)
    op.drop_index("ix_martian_runs_kind", table_name="martian_runs")
    with op.batch_alter_table("martian_runs") as runs:
        runs.drop_column("kind")
