"""The red team engine: plan probes, attack the target, verify what happened.

Attacks are generated and judged by a :class:`~martian_redteam.types.RedteamService`: the hosted
client (:class:`martian.redteam.client.Client`) or the local service on the user's own models
(:class:`LocalService`). :func:`select_service` picks one for a run file and never falls back from one
to the other. The version 0.2 probe loop (:mod:`martian_redteam.orchestrator`) runs with either.
"""

from martian.redteam.engine.local import EmptyGeneration, LocalService, LocalServiceError, UnreadableJudgment
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
    "EmptyGeneration",
    "EvaluatorContext",
    "EvaluatorOutput",
    "LocalService",
    "LocalServiceError",
    "ModelUsage",
    "RedteamService",
    "ServiceSelectionError",
    "SimulatorContext",
    "SimulatorOutput",
    "TargetTimeoutError",
    "UnreadableJudgment",
    "a_red_team",
    "connection_configured",
    "red_team",
    "select_service",
]
