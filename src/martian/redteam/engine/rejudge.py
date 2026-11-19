"""Re-judge a stored red team run offline: apply the verifiers again to what it recorded.

:func:`rejudge` reads each stored attempt (what the attacker wrote, what the target received, its
reply, tool calls and evidence such as receipts) and decides the probe's checks again with the
run file's current checks and verifiers, without calling the target. A judge check calls the judge
model; nothing else calls a model. The result is a new run, ``<id>-rejudged-<n>``, that names the
run it re-judged in ``provenance["rejudged_from"]`` and keeps the verifier version of every result;
the stored run is not changed, so its verdict stays readable beside the new one.

A stored attempt keeps a digest of its content unless the run kept content
(``MARTIAN_REPORT_INCLUDE_CONTENT=1``). Its checks are then undetermined: there is nothing to decide
them on.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from martian.redteam.engine.budget import BudgetExhausted
from martian.redteam.engine.plan import PlannedProbe
from martian.redteam.engine.run import Engine, _Spending, completed
from martian.redteam.model import Attempt, CheckResult, EvidenceLevel, RedteamRun
from martian.redteam.runfile import RunFile
from martian.redteam.targets import TargetReply
from martian.redteam.verify.rules import RULES_VERSION, attempt_outcome
from martian.redteam.verify.verifiers import Verifiers
from martian_redteam.types import RedteamService

DIGEST = re.compile(r"^sha256:[0-9a-f]{16}$")
NO_CONTENT = (
    "the stored attempt keeps a digest of its content, not the content: keep content "
    "(MARTIAN_REPORT_INCLUDE_CONTENT=1) to re-judge it"
)


class TargetNotCalled(RuntimeError):
    """Raised by the stand-in target of a re-judged run: re-judging never calls the target."""


class _NoTarget:
    kind = "callback"
    name = "re-judged"

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        raise TargetNotCalled("re-judging never calls the target")

    async def close(self) -> None:
        return None


class _NoService:
    """The service of a re-judged run when no check needs one: it writes and judges nothing."""

    @property
    def redaction_policy(self) -> Any:
        from martian._privacy import RedactionPolicy

        return RedactionPolicy()

    async def simulate(self, *args: Any, **kwargs: Any) -> Any:
        raise TargetNotCalled("re-judging writes no attack")

    async def evaluate(self, *args: Any, **kwargs: Any) -> Any:
        raise TargetNotCalled("this re-judged run has no judge: set models.judge")


def _digested(attempt: Attempt) -> bool:
    return any(value is not None and DIGEST.match(value) for value in (attempt.input, attempt.output))


async def rejudge(
    stored: RedteamRun,
    run_file: RunFile,
    *,
    service: RedteamService | None = None,
    verifiers: Verifiers | None = None,
    number: int = 1,
) -> RedteamRun:
    """``stored`` decided again by ``run_file``'s checks, as a new run. Attempts of a probe the run
    file no longer plans are left out; an attempt that never reached the target is kept as it was."""
    engine = Engine(
        run_file,
        service=service or _NoService(),
        target=_NoTarget(),
        verifiers=verifiers,
        canary=stored.provenance.get("canary") or None,
    )
    engine.run_id = f"{stored.id}-rejudged-{number}"
    engine.project_id, engine.started_at = stored.project_id, datetime.now(UTC)
    engine.listened = "receipts" in stored.provenance
    planned = {item.probe.id: item for item in engine.planned}
    evidence = {item.id: item for item in stored.evidence}
    changed = 0
    for attempt in stored.attempts:
        plan = planned.get(attempt.probe_id)
        if plan is None:
            continue
        again = await _again(engine, plan, attempt, evidence)
        changed += (again.outcome, again.level) != (attempt.outcome, attempt.level)
        engine.attempts[again.id] = again
        for item in again.evidence_ids:
            if item in evidence:
                engine.evidence[item] = evidence[item]
    state = stored.state if stored.state != "running" else "completed"
    run = engine.snapshot("stopped" if engine.ledger.stopped_by is not None else state)
    rejudge_budget = engine.ledger.summary()
    return run.model_copy(
        update={
            "started_at": stored.started_at,
            "budget": {
                **stored.budget,
                "rejudge": rejudge_budget["spent"],
                "rejudge_budget": rejudge_budget,
            },
            "limitations": [*stored.limitations, *(f"Re-judge: {note}" for note in engine.ledger.limitations())],
            "provenance": {
                **run.provenance,
                "rejudged_from": stored.id,
                "rejudged_at": datetime.now(UTC).isoformat(),
                "original_rules_version": stored.provenance.get("rules_version", stored.policy_revision),
                "original_verifiers": stored.provenance.get("verifiers", {}),
                "changed_attempts": changed,
            },
        }
    )


async def _again(engine: Engine, plan: PlannedProbe, attempt: Attempt, evidence: dict[str, Any]) -> Attempt:
    """One stored attempt decided again. No target is called."""
    if attempt.output is None:
        return attempt
    probe = plan.probe
    checks = engine._control_checks(probe) if attempt.role == "control" else probe.checks
    kept = [evidence[item] for item in attempt.evidence_ids if item in evidence]
    if _digested(attempt):
        results = [
            CheckResult(check_id=check.id, kind=check.kind, rationale=NO_CONTENT, evidence_ids=attempt.evidence_ids)
            for check in checks
        ]
        return _decided(attempt, results, [], None, None)
    target_turns = [turn for turn in attempt.turns if turn.role == "target"]
    sent = next((turn.content for turn in attempt.turns if turn.role == "attacker"), attempt.input or "")
    reply_evidence = next((item for item in kept if item.kind == "reply"), None)
    observed = bool(reply_evidence.metadata.get("tool_calls_observed")) if reply_evidence is not None else False
    reply = TargetReply(
        text=attempt.output,
        tool_calls=target_turns[-1].tool_calls if target_turns else [],
        tool_calls_observed=observed,
        evidence=[item for item in kept if item.kind not in ("reply", "receipt")],
    )
    spending = _Spending(engine.ledger, probe.id)
    task_results: list[CheckResult] = []
    if probe.task_checks:
        task = await engine._verify(plan, probe.task_checks, probe.task, sent, reply, spending, kept, attempt.id)
        task_results = task.results
    decided = await engine._verify(plan, checks, attempt.input or "", sent, reply, spending, kept, attempt.id)
    stop = decided.stop if isinstance(decided.stop, BudgetExhausted) else None
    return _decided(attempt, decided.results, task_results, decided.failure, stop)


def _decided(
    attempt: Attempt, results: list[CheckResult], task: list[CheckResult], failure: Any, stop: BudgetExhausted | None
) -> Attempt:
    outcome, level = attempt_outcome(results)
    update: dict[str, Any] = {
        "check_results": results,
        "task_results": task,
        "task_completed": completed(task) if task else None,
        "outcome": outcome,
        "level": level if outcome == "hit" else EvidenceLevel.DELIVERED,
        "error": "",
        "error_kind": None,
        "error_code": None,
        "stopped_by": None,
    }
    if outcome != "hit" and stop is not None:
        update.update(outcome="stopped", stopped_by=stop.limit)
    elif outcome != "hit" and failure is not None:
        update.update(
            outcome="error",
            error=str(failure),
            error_kind=failure.kind,
            error_code=failure.code,
        )
    return Attempt.model_validate({**attempt.model_dump(), **update})


__all__ = ["NO_CONTENT", "RULES_VERSION", "TargetNotCalled", "rejudge"]
