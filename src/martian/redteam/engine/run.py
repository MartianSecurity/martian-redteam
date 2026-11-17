"""The plan, attack, verify loop of a version 2 run, producing a :class:`~martian.redteam.model.RedteamRun`.

- **Plan** (:mod:`martian.redteam.engine.plan`): the probes and their checks, before anything runs.
- **Attack**: the attack method drives the target. Today every catalogue method is one turn: the
  service writes the attack (an encoding method rewrites a generated injection), and the target is
  called once, in a session of its own per attempt.
- **Verify**: each check decides with the verdict rules (:mod:`martian.redteam.verify.rules`): text,
  canary and tool-call checks first, by code; the judge last, through the service. The attempt's
  outcome is :func:`~martian.redteam.verify.rules.attempt_outcome` of their results.

Every call is reserved on the budget ledger first (:mod:`martian.redteam.engine.budget`). When a
limit is reached, the run stops: an attempt not yet decided is ``stopped``, naming the limit, and the
run's state is ``stopped``. Each attempt records what it spent.

The engine knows targets only as :class:`~martian.redteam.targets.Target` and services only as
:class:`~martian_redteam.types.RedteamService`. ``checkpoint`` is called with the run so far after
each attempt finishes, and with the final run.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from martian.redteam.catalog.prompts import PROMPTS_REVISION
from martian.redteam.engine.budget import BudgetExhausted, CallKind, Ledger
from martian.redteam.engine.plan import PlannedProbe, plan
from martian.redteam.model import (
    Attempt,
    Check,
    CheckResult,
    Evidence,
    EvidenceLevel,
    RedteamRun,
    Spend,
    TargetRef,
    Turn,
)
from martian.redteam.model.maps import counts_of, findings_of
from martian.redteam.runfile import RunFile
from martian.redteam.targets import Target, TargetReply
from martian.redteam.verify.rules import (
    LEVEL_OF_KIND,
    RULES_VERSION,
    attempt_outcome,
    canary_check,
    judge_result,
    text_check,
    tool_call_check,
)
from martian_redteam.catalog.vulnerabilities import CATALOG_VERSION
from martian_redteam.client import ClientError, UndecidedJudgment
from martian_redteam.encoding import apply_encoding
from martian_redteam.orchestrator import DEFAULT_TARGET_TIMEOUT_SECONDS
from martian_redteam.types import EvaluatorContext, ModelUsage, RedteamService, SimulatorContext

ENGINE_VERSION = "martian-redteam-engine-v1"

Checkpoint = Callable[[RedteamRun], Awaitable[None]]

NO_CANARY = "this release plants no canary: give the canary check a value that you planted yourself"
NO_RECEIPT = "this release has no receipt listener"


class AttemptFailed(Exception):
    """An attempt that did not complete: whose failure it was, and why."""

    def __init__(self, kind: str, code: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


def _kind_of(error: ClientError) -> str:
    """A service failure's class: the local service names its model; the hosted client's failures
    are the platform's."""
    kind = getattr(error, "kind", None)
    return kind if kind in ("attacker", "judge") else "infrastructure"


def _code_of(error: Exception, default: str) -> str:
    code = getattr(error, "reason_code", None) or getattr(error, "code", None)
    return str(code) if code else default


class _Spending:
    """What one attempt spent, kept beside the run's ledger."""

    def __init__(self, ledger: Ledger, probe_id: str) -> None:
        self.ledger = ledger
        self.probe_id = probe_id
        self.spend = Spend()

    def reserve(self, kind: CallKind) -> None:
        self.spend = self.spend + self.ledger.reserve(kind, self.probe_id)

    def record(self, usage: ModelUsage | None) -> None:
        self.spend = self.spend + self.ledger.record(self.probe_id, usage)


class Engine:
    """Runs one version 2 run file against ``target`` with ``service``."""

    def __init__(
        self,
        run_file: RunFile,
        *,
        service: RedteamService,
        target: Target,
        run_id: str | None = None,
        project_id: str = "default",
        checkpoint: Checkpoint | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        self.run_file = run_file
        self.service = service
        self.target = target
        self.run_id = run_id or f"redteam-{uuid.uuid4().hex[:12]}"
        self.project_id = project_id
        self.checkpoint = checkpoint
        self.provenance = dict(provenance or {})
        self.planned = plan(run_file)
        self.ledger = Ledger(run_file.budget)
        self.timeout = run_file.target.timeout_seconds or DEFAULT_TARGET_TIMEOUT_SECONDS
        self.started_at = datetime.now(UTC)
        self.attempts: dict[str, Attempt] = {}
        self.evidence: dict[str, Evidence] = {}
        self._saving = asyncio.Lock()

    # --- the run --------------------------------------------------------------------------------

    async def run(self) -> RedteamRun:
        semaphore = asyncio.Semaphore(self.run_file.budget.max_concurrent)

        async def one(planned: PlannedProbe, number: int) -> None:
            async with semaphore:
                attempt = await self._attempt(planned, number)
            self.attempts[attempt.id] = attempt
            await self._save(self.snapshot("running"))

        jobs = [
            asyncio.create_task(one(planned, number))
            for planned in self.planned
            for number in range(1, planned.probe.planned_attempts + 1)
        ]
        try:
            await asyncio.gather(*jobs)
        except BaseException as error:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await self._save(self.snapshot("cancelled" if isinstance(error, asyncio.CancelledError) else "failed"))
            raise
        final = self.snapshot("stopped" if self.ledger.stopped_by else "completed")
        await self._save(final)
        return final

    async def _save(self, run: RedteamRun) -> None:
        if self.checkpoint is None:
            return
        async with self._saving:
            await self.checkpoint(run)

    # --- one attempt ----------------------------------------------------------------------------

    async def _attempt(self, planned: PlannedProbe, number: int) -> Attempt:
        probe = planned.probe
        spending = _Spending(self.ledger, probe.id)
        fields: dict[str, Any] = {
            "id": f"{probe.id}#{number}",
            "probe_id": probe.id,
            "number": number,
            "started_at": datetime.now(UTC),
            "graph_revision": PROMPTS_REVISION,
        }

        def done(**update: Any) -> Attempt:
            return Attempt(**{**fields, **update, "spend": spending.spend, "finished_at": datetime.now(UTC)})

        try:
            self.ledger.check()
            payload, generated = await self._generate(planned, spending)
            fields.update(input=payload, policy_revision=generated.get("policy_revision"))
            reply = await self._send(fields["id"], payload, spending)
        except BudgetExhausted as exhausted:
            return done(outcome="stopped", stopped_by=exhausted.limit)
        except AttemptFailed as failed:
            return done(outcome="error", error=str(failed), error_kind=failed.kind, error_code=failed.code)
        evidence = self._keep_reply(fields["id"], reply)
        fields.update(
            output=reply.text,
            target_calls=1,
            evidence_ids=[evidence.id],
            turns=[
                Turn(role="attacker", content=payload),
                Turn(role="target", content=reply.text, tool_calls=reply.tool_calls),
            ],
        )
        results, failure, stop = await self._verify(planned, payload, reply, spending, evidence.id)
        outcome, level = attempt_outcome(results)
        if outcome != "hit" and stop is not None:
            return done(outcome="stopped", stopped_by=stop.limit, check_results=results)
        if outcome != "hit" and failure is not None:
            return done(
                outcome="error",
                error=str(failure),
                error_kind=failure.kind,
                error_code=failure.code,
                check_results=results,
            )
        return done(outcome=outcome, level=level, check_results=results)

    async def _generate(self, planned: PlannedProbe, spending: _Spending) -> tuple[str, dict[str, Any]]:
        attack, probe = planned.attack, planned.probe
        context = SimulatorContext(
            vulnerability=probe.vulnerability,
            vulnerability_type=probe.vulnerability_type,
            purpose=self.run_file.target.purpose,
            attack_method=attack.id if attack.kind == "llm" else "prompt_injection",
        )
        spending.reserve("attacker")
        try:
            output = await self.service.simulate(context)
        except ClientError as error:
            raise AttemptFailed(_kind_of(error), _code_of(error, "attacker_failed"), str(error)) from None
        spending.record(output.usage)
        payload = apply_encoding(output.attack, attack.id) if attack.kind == "encoding" else output.attack
        return payload, {"policy_revision": output.policy_revision}

    async def _send(self, session: str, payload: str, spending: _Spending) -> TargetReply:
        spending.reserve("target")
        try:
            async with asyncio.timeout(self.timeout) as deadline:
                return await self.target.send(payload, session=session)
        except TimeoutError as error:
            if deadline.expired():
                raise AttemptFailed(
                    "target", "target_timeout", f"the target did not answer within {self.timeout:g} seconds"
                ) from None
            raise AttemptFailed("target", "target_failed", f"the target failed: {error}") from None
        except Exception as error:  # noqa: BLE001 - every failure of the target is the target's
            raise AttemptFailed("target", "target_failed", f"the target failed: {type(error).__name__}") from None

    def _keep_reply(self, attempt_id: str, reply: TargetReply) -> Evidence:
        evidence = Evidence(
            id=f"{attempt_id}/reply",
            kind="reply",
            tier="text",
            source=f"target:{self.target.kind}",
            sha256=hashlib.sha256(reply.text.encode()).hexdigest(),
            content=reply.text,
            captured_at=datetime.now(UTC),
            metadata={"tool_calls": len(reply.tool_calls), "tool_calls_observed": reply.tool_calls_observed},
        )
        self.evidence[evidence.id] = evidence
        return evidence

    # --- verify ---------------------------------------------------------------------------------

    async def _verify(
        self, planned: PlannedProbe, payload: str, reply: TargetReply, spending: _Spending, evidence_id: str
    ) -> tuple[list[CheckResult], AttemptFailed | None, BudgetExhausted | None]:
        """Each check's result, the judge's failure and a budget stop. Checks decided by code run
        first, so the judge's failure or a stop leaves what they found."""
        ordered = sorted(planned.probe.checks, key=lambda check: check.kind == "judge")
        results: list[CheckResult] = []
        failure: AttemptFailed | None = None
        stop: BudgetExhausted | None = None
        for check in ordered:
            if check.kind == "judge":
                try:
                    results.append(await self._judge(planned, check, payload, reply, spending, evidence_id))
                except AttemptFailed as error:
                    failure = error
                    results.append(CheckResult(check_id=check.id, kind="judge", error=str(error)))
                except BudgetExhausted as error:
                    stop = error
                    results.append(CheckResult(check_id=check.id, kind="judge", error=str(error)))
                continue
            results.append(self._decide(check, payload, reply, evidence_id))
        order = {check.id: index for index, check in enumerate(planned.probe.checks)}
        results.sort(key=lambda result: order[result.check_id])
        return results, failure, stop

    def _decide(self, check: Check, payload: str, reply: TargetReply, evidence_id: str) -> CheckResult:
        spec, executed = check.spec, None
        hit: bool | None
        detail: dict[str, Any]
        if check.kind == "text":
            hit, detail = text_check(
                reply.text,
                all_of=spec.get("all_of", []),
                none_of=spec.get("none_of", []),
                probe=payload,
                ignore_case=spec.get("ignore_case", True),
            )
        elif check.kind == "canary":
            if spec.get("value"):
                hit, detail = canary_check(reply.text, canary=spec["value"], probe=payload)
            else:
                hit, detail = None, {"reason": NO_CANARY}
        elif check.kind == "tool_call":
            hit, detail = tool_call_check(
                reply.tool_calls,
                tool=spec["tool"],
                where=spec.get("where", {}),
                executed=spec.get("executed", False),
                observed=reply.tool_calls_observed,
            )
            executed = detail["executed"]
        elif check.kind == "receipt":
            hit, detail = None, {"reason": NO_RECEIPT}
        else:
            hit, detail = None, {"reason": f"a {check.kind} check needs an environment"}
        return CheckResult(
            check_id=check.id,
            kind=check.kind,
            hit=hit,
            level=LEVEL_OF_KIND[check.kind] if hit else None,
            executed=executed,
            rationale=str(detail.get("reason", "")),
            evidence_ids=[evidence_id],
            detail={key: value for key, value in detail.items() if key != "reason"},
        )

    async def _judge(
        self,
        planned: PlannedProbe,
        check: Check,
        payload: str,
        reply: TargetReply,
        spending: _Spending,
        evidence_id: str,
    ) -> CheckResult:
        probe = planned.probe
        context = EvaluatorContext(
            vulnerability=probe.vulnerability,
            vulnerability_type=probe.vulnerability_type,
            attack_method=probe.attack_method,
            criteria=check.spec.get("criteria") or probe.criteria,
        )
        spending.reserve("judge")
        try:
            judged = await self.service.evaluate(
                context,
                attack=payload,
                target_output=reply.text,
                tools_called=[call.name for call in reply.tool_calls],
            )
        except UndecidedJudgment as error:
            return CheckResult(check_id=check.id, kind="judge", error=str(error), evidence_ids=[evidence_id])
        except ClientError as error:
            raise AttemptFailed(_kind_of(error), _code_of(error, "judge_failed"), str(error)) from None
        spending.record(judged.usage)
        # The hosted judge scores 0 when the target was vulnerable: that is a hit.
        hit = judge_result(judged.score == 0)
        return CheckResult(
            check_id=check.id,
            kind="judge",
            hit=hit,
            level=EvidenceLevel.JUDGED if hit else None,
            rationale=judged.reason,
            evidence_ids=[evidence_id],
        )

    # --- the record -----------------------------------------------------------------------------

    def snapshot(self, state: str) -> RedteamRun:
        """The run as it stands, in plan order."""
        order = {
            attempt_id: index
            for index, attempt_id in enumerate(
                attempt_id for planned in self.planned for attempt_id in planned.attempt_ids()
            )
        }
        attempts = sorted(self.attempts.values(), key=lambda attempt: order.get(attempt.id, len(order)))
        probes = [planned.probe for planned in self.planned]
        severity = {planned.probe.id: planned.severity for planned in self.planned}
        findings = [
            finding.model_copy(update={"severity": severity.get(finding.probe_id)})
            for finding in findings_of(probes, attempts, [])
        ]
        finished = datetime.now(UTC) if state != "running" else None
        kept = [item for attempt in attempts for item in attempt.evidence_ids]
        evidence = [self.evidence[item] for item in kept if item in self.evidence]
        return RedteamRun(
            id=self.run_id,
            project_id=self.project_id,
            source="local",
            state=state,  # type: ignore[arg-type]
            catalog_version=CATALOG_VERSION,
            purpose=self.run_file.target.purpose,
            target=TargetRef(
                kind=self.target.kind,  # type: ignore[arg-type]
                name=getattr(self.target, "name", ""),
                purpose=self.run_file.target.purpose,
            ),
            graph_revision=PROMPTS_REVISION,
            policy_revision=RULES_VERSION,
            started_at=self.started_at,
            finished_at=finished,
            duration_seconds=((finished or datetime.now(UTC)) - self.started_at).total_seconds(),
            expected_attempts=len(order),
            probes=probes,
            attempts=attempts,
            evidence=evidence,
            findings=findings,
            counts=counts_of(attempts, findings),
            budget=self.ledger.summary(),
            limitations=self._limitations(),
            provenance={
                "engine_version": ENGINE_VERSION,
                "rules_version": RULES_VERSION,
                "prompts_revision": PROMPTS_REVISION,
                **self.provenance,
            },
        )

    def _limitations(self) -> list[str]:
        found = ["No repetitions or negative controls ran, so no finding is verified."]
        objectives = {planned.probe.objective_class for planned in self.planned}
        if objectives & {"action", "persistence"}:
            found.append("Without an environment, an action or persistence objective stops at a confirmed tool call.")
        kinds = {(check.kind, bool(check.spec.get("value"))) for p in self.planned for check in p.probe.checks}
        if ("canary", False) in kinds:
            found.append("A canary check without a value is undetermined: " + NO_CANARY + ".")
        if any(kind == "receipt" for kind, _ in kinds):
            found.append("A receipt check is undetermined: " + NO_RECEIPT + ".")
        return found


__all__ = ["ENGINE_VERSION", "AttemptFailed", "Checkpoint", "Engine"]
