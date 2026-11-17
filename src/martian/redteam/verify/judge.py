"""The judge verifier: a model decides a ``judge`` check. It is never deterministic, so its hits are
level 1, suspected, and never counted as confirmed.

:class:`ServiceJudge` asks the run's red team service (``evaluate``): the Martian platform when the
run file names no judge model.
"""

from __future__ import annotations

from typing import Any

from martian.redteam.model import Check, CheckKind, CheckResult, EvidenceLevel
from martian.redteam.verify.rules import judge_result
from martian.redteam.verify.verifiers import Meter, Observation, VerifierFailed, result
from martian_redteam.client import ClientError, UndecidedJudgment
from martian_redteam.types import EvaluatorContext, RedteamService


def failure_kind(error: ClientError) -> str:
    """A service failure's class: the local service names its model; the hosted client's failures
    are the platform's."""
    kind = getattr(error, "kind", None)
    return kind if kind in ("attacker", "judge") else "infrastructure"


def failure_code(error: Exception, default: str) -> str:
    code = getattr(error, "reason_code", None) or getattr(error, "code", None)
    return str(code) if code else default


class ServiceJudge:
    """A judge check decided by the red team service's ``evaluate``."""

    kind: CheckKind = "judge"
    deterministic = False

    def __init__(self, service: RedteamService, *, version: str = "service-judge-v1") -> None:
        self.service = service
        self.version = version

    async def verify(self, check: Check, seen: Observation, meter: Meter | None = None) -> CheckResult:
        probe = seen.probe
        context = EvaluatorContext(
            vulnerability=probe.vulnerability,
            vulnerability_type=probe.vulnerability_type,
            attack_method=probe.attack_method,
            criteria=check.spec.get("criteria") or probe.criteria,
        )
        if meter is not None:
            meter.reserve("judge")
        try:
            judged = await self.service.evaluate(
                context,
                attack=seen.attack,
                target_output=seen.reply,
                tools_called=[call.name for call in seen.tool_calls],
            )
        except UndecidedJudgment as error:
            found = result(self, check, seen, None, source="judge:service")
            return found.model_copy(update={"error": str(error)})
        except ClientError as error:
            raise VerifierFailed(failure_kind(error), failure_code(error, "judge_failed"), str(error)) from None
        if meter is not None:
            meter.record(judged.usage)
        # The service scores 0 when the target was vulnerable: that is a hit.
        hit = judge_result(judged.score == 0)
        detail: dict[str, Any] = {}
        return result(
            self,
            check,
            seen,
            hit,
            detail,
            level=EvidenceLevel.JUDGED,
            source="judge:service",
            rationale=judged.reason,
        )


__all__ = ["ServiceJudge", "failure_code", "failure_kind"]
