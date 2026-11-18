"""``redteam-attempts.csv``: one row per attempt, for a spreadsheet.

A cell that starts with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return gets a leading ``'``, so
a spreadsheet never reads target content as a formula.
"""

from __future__ import annotations

import csv
import io

from martian.redteam.model import Attempt, RedteamRun
from martian.redteam.verify.rules import label

COLUMNS = [
    "run_id",
    "attempt_id",
    "probe_id",
    "vulnerability",
    "vulnerability_type",
    "attack_method",
    "objective_class",
    "role",
    "number",
    "outcome",
    "evidence_level",
    "label",
    "checks",
    "error_kind",
    "error_code",
    "stopped_by",
    "target_queries",
    "attacker_calls",
    "judge_calls",
    "attacker_refusals",
    "input",
    "output",
]

_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA) else text


def _checks(attempt: Attempt) -> str:
    words = {True: "hit", False: "no hit", None: "undetermined"}
    return "; ".join(f"{result.check_id}={words[result.hit]}" for result in attempt.check_results)


def render_csv(run: RedteamRun) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for attempt in run.attempts:
        probe = run.probe(attempt.probe_id)
        executed = any(result.executed for result in attempt.check_results if result.hit)
        spend = attempt.spend
        writer.writerow(
            _safe(value)
            for value in (
                run.id,
                attempt.id,
                probe.id,
                probe.vulnerability,
                probe.vulnerability_type,
                probe.attack_method,
                probe.objective_class,
                attempt.role,
                attempt.number,
                attempt.outcome,
                None if attempt.level is None else int(attempt.level),
                label(attempt.level, executed=executed, objective=probe.objective_class) if attempt.level else "",
                _checks(attempt),
                attempt.error_kind,
                attempt.error_code,
                attempt.stopped_by,
                spend.target_queries if spend is not None else attempt.target_calls,
                spend.attacker_calls if spend is not None else "",
                spend.judge_calls if spend is not None else "",
                attempt.attacker_refusals,
                attempt.input,
                attempt.output,
            )
        )
    return buffer.getvalue()


__all__ = ["COLUMNS", "render_csv"]
