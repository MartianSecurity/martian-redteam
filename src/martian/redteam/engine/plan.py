"""Plan: expand a version 2 run file into probes, each with its checks fixed before the attack runs.

One probe per vulnerability, type and attack method, with ``budget.attempts_per_probe`` attempts. A
vulnerability without checks is decided by the judge. The plan calls no model and loads no target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from martian.redteam.model import Check, Probe
from martian.redteam.model.maps import objective_of, probe_id
from martian.redteam.runfile import CheckDecl, RunFile, Vulnerability
from martian_redteam.catalog.attacks import Attack, get_attack
from martian_redteam.catalog.vulnerabilities import Vulnerability as CatalogVulnerability
from martian_redteam.catalog.vulnerabilities import get_vulnerability


class PlanError(ValueError):
    """The run file cannot be planned: a check the run cannot decide, or one it needs a model for."""


@dataclass(frozen=True)
class PlannedProbe:
    """A probe, and the catalogue entries it was planned from."""

    probe: Probe
    vulnerability: CatalogVulnerability
    attack: Attack
    severity: str | None = None

    def attempt_ids(self) -> list[str]:
        return [attempt_id(self.probe.id, number) for number in range(1, self.probe.planned_attempts + 1)]


def attempt_id(probe: str, number: int) -> str:
    return f"{probe}#{number}"


def _check_spec(check: CheckDecl) -> dict[str, Any]:
    spec = check.model_dump(exclude={"type"})
    return {key: value for key, value in spec.items() if value is not None}


def _checks(declared: list[CheckDecl]) -> list[Check]:
    if not declared:
        return [Check(id="judge", kind="judge")]
    kinds = [check.type for check in declared]
    checks = []
    for index, check in enumerate(declared, start=1):
        identifier = check.type if kinds.count(check.type) == 1 else f"{check.type}-{index}"
        checks.append(Check(id=identifier, kind=check.type, spec=_check_spec(check)))
    return checks


def _goal(spec: Vulnerability) -> str:
    if spec.goal is None:
        return ""
    if spec.goal.text:
        return spec.goal.text
    where = ", ".join(f"{name} {value}" for name, value in spec.goal.where.items())
    return f"call {spec.goal.tool}" + (f" with {where}" if where else "")


def plan(run_file: RunFile) -> list[PlannedProbe]:
    """Every probe of the run, in the run file's order."""
    local = run_file.models.attacker is not None
    attacks = [get_attack(item.name, weight=item.weight) for item in run_file.attacks]
    planned: list[PlannedProbe] = []
    for index, spec in enumerate(run_file.vulnerabilities):
        vulnerability = get_vulnerability(spec.name, spec.types, id=spec.id, criteria=spec.criteria, gated=spec.gated)
        checks = _checks(list(spec.checks))
        if local and run_file.models.judge is None and any(check.kind == "judge" for check in checks):
            reason = "has no checks, so the judge decides it" if not spec.checks else "has a judge check"
            raise PlanError(f"vulnerabilities[{index}] {reason}: set models.judge")
        for vulnerability_type in vulnerability.types or vulnerability.allowed_types:
            for attack in attacks:
                identifier = probe_id(vulnerability.id, vulnerability_type, attack.id)
                if any(item.probe.id == identifier for item in planned):
                    raise PlanError(f"vulnerabilities[{index}]: probe {identifier} is planned twice")
                probe = Probe(
                    id=identifier,
                    vulnerability=vulnerability.id,
                    vulnerability_type=vulnerability_type,
                    attack_method=attack.id,
                    objective_class=spec.objective or objective_of(vulnerability.id),
                    goal=_goal(spec),
                    gated=vulnerability.gated,
                    criteria=vulnerability.criteria,
                    checks=checks,
                    planned_attempts=run_file.budget.attempts_per_probe,
                    metadata={
                        "severity": spec.severity,
                        "attack_kind": attack.kind,
                        **({"plant": spec.plant.model_dump()} if spec.plant is not None else {}),
                    },
                )
                planned.append(PlannedProbe(probe, vulnerability, attack, spec.severity))
    return planned


__all__ = ["PlanError", "PlannedProbe", "attempt_id", "plan"]
