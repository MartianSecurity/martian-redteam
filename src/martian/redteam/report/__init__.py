"""Red team reports: the report standard and the files a run writes.

:func:`~martian.redteam.report.standard.build_report` computes the report standard from a
:class:`~martian.redteam.model.RedteamRun`: the attacker, the methods and the budget, success per
attempt and within k target queries with Wilson intervals, the attacker's refusal rate, findings by
evidence level, verification and controls, and errors and undetermined attempts as outcomes.
``docs/redteam-reports.md`` defines every number and every output.
"""

from __future__ import annotations

from martian.redteam.report.markdown import render_markdown
from martian.redteam.report.standard import (
    NOT_EVALUATED,
    REPORT_VERSION,
    GateOutcome,
    Rate,
    RedteamReport,
    build_report,
    rate_of,
)

__all__ = [
    "NOT_EVALUATED",
    "REPORT_VERSION",
    "GateOutcome",
    "Rate",
    "RedteamReport",
    "build_report",
    "rate_of",
    "render_markdown",
]
