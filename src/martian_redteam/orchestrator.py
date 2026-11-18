"""Single-turn simulate → encode → target → evaluate loop."""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from martian._errors import error_code
from martian_redteam.callback import AsyncTarget, TargetCallback, wrap_callback
from martian_redteam.catalog.attacks import Attack
from martian_redteam.catalog.vulnerabilities import CATALOG_VERSION, Vulnerability
from martian_redteam.client import ClientError, UndecidedJudgment
from martian_redteam.encoding import apply_encoding
from martian_redteam.types import (
    EvaluatorContext,
    RedteamService,
    RiskAssessment,
    SimulatorContext,
    TestCase,
    Turn,
    VulnerabilityTypeResult,
)

DEFAULT_TARGET_TIMEOUT_SECONDS = 120.0


class TargetTimeoutError(TimeoutError):
    """The target callback did not return within ``target_timeout_seconds``."""


async def a_red_team(
    *,
    target: TargetCallback,
    vulnerabilities: Sequence[Vulnerability],
    attacks: Sequence[Attack],
    client: RedteamService,
    purpose: str,
    max_concurrent: int = 2,
    attacks_per_vulnerability_type: int = 1,
    graph_revision: str | None = None,
    target_timeout_seconds: float = DEFAULT_TARGET_TIMEOUT_SECONDS,
    on_probe: Callable[[TestCase], None] | None = None,
) -> RiskAssessment:
    """Run every probe and return the aggregated assessment.

    ``target_timeout_seconds`` bounds each call to the target callback. A call
    that exceeds it is cancelled and recorded as ``error_kind="target"`` so a
    hung target cannot hold the run open until the CI job is killed.
    """
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be >= 1")
    if attacks_per_vulnerability_type < 1:
        raise ValueError("attacks_per_vulnerability_type must be >= 1")
    if not math.isfinite(target_timeout_seconds) or target_timeout_seconds <= 0:
        raise ValueError("target_timeout_seconds must be finite and > 0")
    if not vulnerabilities:
        raise ValueError("at least one vulnerability is required")
    if not attacks:
        raise ValueError("at least one attack is required")
    if not purpose.strip():
        raise ValueError("purpose must be non-empty")

    executor = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="martian-target")
    wrapped = wrap_callback(target, _executor=executor)
    semaphore = asyncio.Semaphore(max_concurrent)
    started = time.perf_counter()
    started_at = datetime.now(UTC)
    jobs: list[tuple[Vulnerability, str, Attack]] = []
    for vulnerability in vulnerabilities:
        types = vulnerability.types or vulnerability.allowed_types
        for vuln_type in types:
            for attack in attacks:
                for _ in range(attacks_per_vulnerability_type):
                    jobs.append((vulnerability, vuln_type, attack))

    async def run_one(vulnerability: Vulnerability, vuln_type: str, attack: Attack) -> TestCase:
        async with semaphore:
            case = await _probe(
                client=client,
                target=wrapped,
                vulnerability=vulnerability,
                vuln_type=vuln_type,
                attack=attack,
                purpose=purpose,
                target_timeout_seconds=target_timeout_seconds,
            )
            if on_probe is not None:
                on_probe(case)
            return case

    async def drain_probes() -> None:
        # gather propagates the first exception while siblings are still live.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Join synchronous callbacks before returning ownership to the caller.
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)

    tasks = [asyncio.create_task(run_one(*job)) for job in jobs]
    completed = False
    try:
        cases = await asyncio.gather(*tasks)
        completed = True
    finally:
        cleanup = asyncio.create_task(drain_probes())
        cancellation = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
        cleanup.result()
        # If gather already failed, the finally block lets that outcome propagate.
        if completed and cancellation is not None:
            raise cancellation
    return _summarize(
        list(cases),
        purpose=purpose,
        duration=time.perf_counter() - started,
        graph_revision=graph_revision,
        vulnerabilities=vulnerabilities,
        started_at=started_at,
        expected_probe_count=len(jobs),
    )._bind_redaction(client.redaction_policy)


def red_team(
    *,
    target: TargetCallback,
    vulnerabilities: Sequence[Vulnerability],
    attacks: Sequence[Attack],
    client: RedteamService,
    purpose: str,
    max_concurrent: int = 2,
    attacks_per_vulnerability_type: int = 1,
    graph_revision: str | None = None,
    target_timeout_seconds: float = DEFAULT_TARGET_TIMEOUT_SECONDS,
) -> RiskAssessment:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            a_red_team(
                target=target,
                vulnerabilities=vulnerabilities,
                attacks=attacks,
                client=client,
                purpose=purpose,
                max_concurrent=max_concurrent,
                attacks_per_vulnerability_type=attacks_per_vulnerability_type,
                graph_revision=graph_revision,
                target_timeout_seconds=target_timeout_seconds,
            )
        )
    raise RuntimeError("red_team() cannot run inside an event loop; use await a_red_team(...)")


async def _call_target(target: AsyncTarget, payload: str, timeout_seconds: float) -> Turn:
    """Await the target callback, cancelling it after ``timeout_seconds``.

    Raises ``TargetTimeoutError`` only when this deadline expired. A
    ``TimeoutError`` raised by the callback itself (for example its own HTTP
    timeout) propagates unchanged so the report names the real cause.

    ``asyncio.timeout`` is what ``asyncio.wait_for`` uses internally on
    Python 3.12+; using it directly exposes ``expired()`` to tell the two
    cases apart without wrapping the callback in an extra task.
    """
    message = f"target callback timed out after {timeout_seconds:g} seconds (system_config.target_timeout_seconds)"
    try:
        async with asyncio.timeout(timeout_seconds) as deadline:
            reply = await target(payload, None)
    except TimeoutError as error:
        if deadline.expired():
            raise TargetTimeoutError(message) from error
        raise
    # Some customer libraries suppress cancellation and return a late response.
    if deadline.expired():
        raise TargetTimeoutError(message)
    return reply


async def _probe(
    *,
    client: RedteamService,
    target: AsyncTarget,
    vulnerability: Vulnerability,
    vuln_type: str,
    attack: Attack,
    purpose: str,
    target_timeout_seconds: float = DEFAULT_TARGET_TIMEOUT_SECONDS,
) -> TestCase:
    """Run one probe. Each stage has its own handler so errors keep their real origin.

    Only ``ClientError`` from the hosted calls is an infrastructure error and
    only exceptions raised while awaiting the callback are target errors.
    Anything else (for example an encoding bug) is an SDK defect and
    propagates instead of being blamed on the customer's target.
    """
    base = TestCase(
        vulnerability=vulnerability.id,
        vulnerability_type=vuln_type,
        attack_method=attack.id,
    )
    llm_method = attack.id if attack.kind == "llm" else "prompt_injection"
    try:
        simulated = await client.simulate(
            SimulatorContext(
                vulnerability=vulnerability.id,
                vulnerability_type=vuln_type,
                purpose=purpose,
                attack_method=llm_method,
                mode="baseline",
            )
        )
    except ClientError as error:
        operation = getattr(error, "operation_id", None)
        return base.model_copy(
            update={
                "error": str(error),
                "error_kind": "infrastructure",
                "simulation_operation_id": str(operation) if operation is not None else None,
            }
        )._bind_error_code(error_code(getattr(error, "reason_code", None)) or getattr(error, "code", None))

    payload = simulated.attack
    revision = simulated.graph_revision
    base = base.model_copy(
        update={
            "simulation_operation_id": simulated.operation_id,
            "policy_revision": simulated.policy_revision,
        }
    )
    if attack.kind == "encoding":
        payload = apply_encoding(payload, attack.id)

    try:
        reply_turn = await _call_target(target, payload, target_timeout_seconds)
    except TargetTimeoutError as error:
        return base.model_copy(
            update={
                "input": payload,
                "error": str(error),
                "error_kind": "target",
                "graph_revision": revision,
            }
        )._bind_error_code("target_timeout")
    except Exception as error:  # noqa: BLE001 — target failures become case errors
        return base.model_copy(
            update={
                "input": payload,
                "error": f"target callback failed: {error}",
                "error_kind": "target",
                "graph_revision": revision,
            }
        )._bind_error_code("target_callback_failed")

    reply_content = reply_turn.content
    try:
        evaluated = await client.evaluate(
            EvaluatorContext(
                vulnerability=vulnerability.id,
                vulnerability_type=vuln_type,
                attack_method=attack.id,
                criteria=vulnerability.criteria,
            ),
            attack=payload,
            target_output=reply_content,
            tools_called=reply_turn.tools_called,
        )
    except UndecidedJudgment as error:
        # An answer that could not be read decides nothing: no score, and no error either.
        return base.model_copy(
            update={
                "input": payload,
                "actual_output": reply_content,
                "turns": [reply_turn],
                "reason": f"undetermined: {error}",
                "graph_revision": revision,
            }
        )
    except ClientError as error:
        operation = getattr(error, "operation_id", None)
        return base.model_copy(
            update={
                "input": payload,
                "actual_output": reply_content,
                "turns": [reply_turn],
                "error": str(error),
                "error_kind": "infrastructure",
                "graph_revision": revision,
                "evaluation_operation_id": str(operation) if operation is not None else None,
            }
        )._bind_error_code(error_code(getattr(error, "reason_code", None)) or getattr(error, "code", None))
    return base.model_copy(
        update={
            "input": payload,
            "actual_output": reply_content,
            "turns": [reply_turn],
            "score": evaluated.score,
            "reason": evaluated.reason,
            "graph_revision": evaluated.graph_revision or revision,
            "policy_revision": evaluated.policy_revision or simulated.policy_revision,
            "evaluation_operation_id": evaluated.operation_id,
        }
    )


def _summarize(
    cases: list[TestCase],
    *,
    purpose: str,
    duration: float,
    graph_revision: str | None,
    vulnerabilities: Sequence[Vulnerability],
    started_at: datetime | None = None,
    expected_probe_count: int | None = None,
) -> RiskAssessment:
    gated_ids = {item.id for item in vulnerabilities if item.gated}
    groups: dict[tuple[str, str], list[TestCase]] = defaultdict(list)
    for case in cases:
        groups[(case.vulnerability, case.vulnerability_type)].append(case)
    results: list[VulnerabilityTypeResult] = []
    for (vuln, vuln_type), group in sorted(groups.items()):
        passing = sum(1 for case in group if case.score == 1)
        failing = sum(1 for case in group if case.score == 0)
        errored = sum(1 for case in group if case.error is not None)
        scored = passing + failing
        pass_rate = passing / scored if scored else 0.0
        results.append(
            VulnerabilityTypeResult(
                vulnerability=vuln,
                vulnerability_type=vuln_type,
                pass_rate=pass_rate,
                passing=passing,
                failing=failing,
                errored=errored,
                gated=vuln in gated_ids,
            )
        )
    infrastructure_errors = sum(1 for case in cases if case.error_kind == "infrastructure")
    target_errors = sum(1 for case in cases if case.error_kind == "target")
    observed_revision = next((case.graph_revision for case in cases if case.graph_revision), None)
    policies = {case.policy_revision for case in cases if case.policy_revision}
    return RiskAssessment(
        catalog_version=CATALOG_VERSION,
        graph_revision=graph_revision or observed_revision,
        policy_revision=next(iter(policies)) if len(policies) == 1 else "mixed" if policies else None,
        purpose=purpose,
        run_duration_seconds=duration,
        test_cases=cases,
        vulnerability_type_results=results,
        errored=infrastructure_errors + target_errors,
        infrastructure_errors=infrastructure_errors,
        target_errors=target_errors,
        started_at=started_at or datetime.now(UTC),
        expected_probe_count=expected_probe_count,
    )
