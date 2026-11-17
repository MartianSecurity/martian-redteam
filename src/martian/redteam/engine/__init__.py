"""The red team engine: plan probes, attack the target, verify what happened.

:class:`Engine` runs a version 2 run file (:mod:`martian.redteam.engine.run`): it plans the probes and
their checks, attacks the target through :class:`~martian.redteam.targets.Target`, decides each check
with the verdict rules and keeps a budget ledger. :func:`~martian.redteam.engine.runs.execute` runs it
and keeps the run in a store; :class:`~martian.redteam.engine.launcher.LocalLauncher` (``evals`` extra)
starts runs for the ``/v1/redteam`` routes.

Attacks are generated and judged by a :class:`~martian_redteam.types.RedteamService`: the hosted
client (:class:`martian.redteam.client.Client`) or the local service on the user's own models
(:class:`LocalService`). :func:`select_service` picks one for a run file and never falls back from one
to the other. The version 0.2 probe loop (:mod:`martian_redteam.orchestrator`) runs with either.
"""

from martian.redteam.engine.budget import BudgetExhausted, Ledger
from martian.redteam.engine.gate import evaluate_run_gate, gate_failures
from martian.redteam.engine.local import EmptyGeneration, LocalService, LocalServiceError, UnreadableJudgment
from martian.redteam.engine.plan import PlanError, PlannedProbe, plan
from martian.redteam.engine.run import ENGINE_VERSION, Engine
from martian.redteam.engine.service import ServiceSelectionError, connection_configured, select_service
from martian_redteam.orchestrator import DEFAULT_TARGET_TIMEOUT_SECONDS, TargetTimeoutError, a_red_team, red_team
from martian_redteam.types import (
    EvaluatorContext,
    EvaluatorOutput,
    ModelUsage,
    RedteamService,
    SimulatorContext,
    SimulatorOutput,
)

__all__ = [
    "DEFAULT_TARGET_TIMEOUT_SECONDS",
    "ENGINE_VERSION",
    "BudgetExhausted",
    "EmptyGeneration",
    "Engine",
    "EvaluatorContext",
    "EvaluatorOutput",
    "Ledger",
    "LocalService",
    "LocalServiceError",
    "ModelUsage",
    "PlanError",
    "PlannedProbe",
    "RedteamService",
    "ServiceSelectionError",
    "SimulatorContext",
    "SimulatorOutput",
    "TargetTimeoutError",
    "UnreadableJudgment",
    "a_red_team",
    "connection_configured",
    "evaluate_run_gate",
    "gate_failures",
    "plan",
    "red_team",
    "select_service",
]
