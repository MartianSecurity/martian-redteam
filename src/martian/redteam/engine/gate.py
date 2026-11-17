"""The gate of a version 2 run, as the engine has named it: :mod:`martian.redteam.gate` decides it.

:func:`gate_failures` lists why a run fails its gate and :func:`evaluate_run_gate` raises
:class:`~martian_redteam.gate.GateFailure` with them. Warnings are in :func:`martian.redteam.gate.evaluate`.
"""

from __future__ import annotations

from martian.redteam.gate import SEVERITY_RANK, evaluate
from martian.redteam.model import RedteamRun
from martian.redteam.runfile import Gate
from martian_redteam.gate import GateFailure


def gate_failures(run: RedteamRun, gate: Gate) -> list[str]:
    """Why the run fails its gate; empty when it passes."""
    return evaluate(run, gate).failures


def evaluate_run_gate(run: RedteamRun, gate: Gate) -> None:
    """Raise :class:`~martian_redteam.gate.GateFailure` when the run fails its gate."""
    failures = gate_failures(run, gate)
    if failures:
        raise GateFailure("; ".join(failures))


__all__ = ["SEVERITY_RANK", "evaluate_run_gate", "gate_failures"]
