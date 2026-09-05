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
    VulnerabilityTypeResult,
)


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
) -> RiskAssessment:
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be >= 1")
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
) -> RiskAssessment:
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
        )
    )


async def _probe(
    *,
    client: Client,
    target: AsyncTarget,
    vulnerability: Vulnerability,
    vuln_type: str,
    attack: Attack,
    purpose: str,
) -> TestCase:
    base = TestCase(
        vulnerability=vulnerability.id,
        vulnerability_type=vuln_type,
        attack_method=attack.id,
    )
    try:
        llm_method = attack.id if attack.kind == "llm" else "prompt_injection"
        simulated = await client.simulate(
            SimulatorContext(
                vulnerability=vulnerability.id,
                vulnerability_type=vuln_type,
                purpose=purpose,
                attack_method=llm_method,
                mode="baseline",
            )
        )
        payload = simulated.attack
        method = attack.id
        if attack.kind == "encoding":
            payload = apply_encoding(payload, attack.id)
        reply = await target(payload, None)
        evaluated = await client.evaluate(
            EvaluatorContext(
                vulnerability=vulnerability.id,
                vulnerability_type=vuln_type,
                attack_method=method,
            ),
            attack=payload,
            target_output=reply.content,
            tools_called=reply.tools_called,
        )
        return base.model_copy(
            update={
                "input": payload,
                "actual_output": reply.content,
                "turns": [reply],
                "score": evaluated.score,
                "reason": evaluated.reason,
            }
        )
    except ClientError as error:
        return base.model_copy(update={"error": str(error)})
    except Exception as error:  # noqa: BLE001 — target failures become case errors
        return base.model_copy(update={"error": f"target callback failed: {error}"})


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
    return RiskAssessment(
        catalog_version=CATALOG_VERSION,
        graph_revision=graph_revision,
        purpose=purpose,
        run_duration_seconds=duration,
        test_cases=cases,
        vulnerability_type_results=results,
        errored=sum(1 for case in cases if case.error is not None),
    )
