"""Single-turn simulate → encode → target → evaluate loop."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Sequence

from martian_redteam.callback import AsyncTarget, TargetCallback, wrap_callback
from martian_redteam.catalog.attacks import Attack
from martian_redteam.catalog.vulnerabilities import CATALOG_VERSION, Vulnerability
from martian_redteam.client import Client, ClientError
from martian_redteam.encoding import apply_encoding
from martian_redteam.types import (
    EvaluatorContext,
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
    client: Client,
    purpose: str,
    max_concurrent: int = 2,
    attacks_per_vulnerability_type: int = 1,
    graph_revision: str | None = None,
    target_timeout_seconds: float = DEFAULT_TARGET_TIMEOUT_SECONDS,
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
    if target_timeout_seconds <= 0:
        raise ValueError("target_timeout_seconds must be > 0")
    if not vulnerabilities:
        raise ValueError("at least one vulnerability is required")
    if not attacks:
        raise ValueError("at least one attack is required")
    if not purpose.strip():
        raise ValueError("purpose must be non-empty")

    wrapped = wrap_callback(target)
    semaphore = asyncio.Semaphore(max_concurrent)
    started = time.perf_counter()
    jobs: list[tuple[Vulnerability, str, Attack]] = []
    for vulnerability in vulnerabilities:
        types = vulnerability.types or vulnerability.allowed_types
        for vuln_type in types:
            for attack in attacks:
                for _ in range(attacks_per_vulnerability_type):
                    jobs.append((vulnerability, vuln_type, attack))

    async def run_one(vulnerability: Vulnerability, vuln_type: str, attack: Attack) -> TestCase:
        async with semaphore:
            return await _probe(
                client=client,
                target=wrapped,
                vulnerability=vulnerability,
                vuln_type=vuln_type,
                attack=attack,
                purpose=purpose,
                target_timeout_seconds=target_timeout_seconds,
            )

    cases = await asyncio.gather(*(run_one(*job) for job in jobs))
    return _summarize(
        list(cases),
        purpose=purpose,
        duration=time.perf_counter() - started,
        graph_revision=graph_revision,
        vulnerabilities=vulnerabilities,
    )


def red_team(
    *,
    target: TargetCallback,
    vulnerabilities: Sequence[Vulnerability],
    attacks: Sequence[Attack],
    client: Client,
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
    try:
        async with asyncio.timeout(timeout_seconds) as deadline:
            return await target(payload, None)
    except TimeoutError as error:
        if deadline.expired():
            raise TargetTimeoutError(
                f"target callback timed out after {timeout_seconds:g} seconds (system_config.target_timeout_seconds)"
            ) from error
        raise


async def _probe(
    *,
    client: Client,
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
        return base.model_copy(update={"error": str(error), "error_kind": "infrastructure"})

    payload = simulated.attack
    revision = simulated.graph_revision
    if attack.kind == "encoding":
        payload = apply_encoding(payload, attack.id)

    try:
        reply_turn = await _call_target(target, payload, target_timeout_seconds)
    except TargetTimeoutError as error:
        return base.model_copy(update={"input": payload, "error": str(error), "error_kind": "target"})
    except Exception as error:  # noqa: BLE001 — target failures become case errors
        return base.model_copy(
            update={
                "input": payload,
                "error": f"target callback failed: {error}",
                "error_kind": "target",
            }
        )

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
    except ClientError as error:
        return base.model_copy(
            update={
                "input": payload,
                "actual_output": reply_content,
                "turns": [reply_turn],
                "error": str(error),
                "error_kind": "infrastructure",
            }
        )
    return base.model_copy(
        update={
            "input": payload,
            "actual_output": reply_content,
            "turns": [reply_turn],
            "score": evaluated.score,
            "reason": evaluated.reason,
            "graph_revision": evaluated.graph_revision or revision,
        }
    )


def _summarize(
    cases: list[TestCase],
    *,
    purpose: str,
    duration: float,
    graph_revision: str | None,
    vulnerabilities: Sequence[Vulnerability],
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
    return RiskAssessment(
        catalog_version=CATALOG_VERSION,
        graph_revision=graph_revision or observed_revision,
        purpose=purpose,
        run_duration_seconds=duration,
        test_cases=cases,
        vulnerability_type_results=results,
        errored=infrastructure_errors + target_errors,
        infrastructure_errors=infrastructure_errors,
        target_errors=target_errors,
    )
