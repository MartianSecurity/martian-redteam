"""The red team engine: plan probes, attack the target, verify what happened.

Today the engine is the version 0.2 probe loop (:mod:`martian_redteam.orchestrator`): one attack per
probe, generated and judged by a :class:`~martian_redteam.types.RedteamService`, which the hosted
client implements. The local engine, on the user's own model, is added here behind the same
interface.
"""

from martian_redteam.orchestrator import DEFAULT_TARGET_TIMEOUT_SECONDS, TargetTimeoutError, a_red_team, red_team
from martian_redteam.types import (
    EvaluatorContext,
    EvaluatorOutput,
    RedteamService,
    SimulatorContext,
    SimulatorOutput,
)

__all__ = [
    "DEFAULT_TARGET_TIMEOUT_SECONDS",
    "EvaluatorContext",
    "EvaluatorOutput",
    "RedteamService",
    "SimulatorContext",
    "SimulatorOutput",
    "TargetTimeoutError",
    "a_red_team",
    "red_team",
]
