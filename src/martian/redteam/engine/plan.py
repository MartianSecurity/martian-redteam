"""Plan: expand a version 2 run file into probes, each with its checks fixed before the attack runs.

One probe per vulnerability, type and attack method, with ``budget.attempts_per_probe`` attempts. Each
``attacks`` entry is resolved to its configured method (:mod:`martian.redteam.methods`). A goal that
names a tool is checked as a tool call. A vulnerability with neither checks nor such a goal is decided
by the judge. The plan calls no model and loads no target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from martian.redteam.methods import AttackMethod, MethodError, needs_attacker, resolve
from martian.redteam.model import Check, Probe
from martian.redteam.model.maps import objective_of, probe_id
from martian.redteam.runfile import AttackSpec, CheckDecl, Goal, RunFile, Vulnerability
from martian_redteam.catalog.attacks import Attack, get_attack
from martian_redteam.catalog.vulnerabilities import Vulnerability as CatalogVulnerability
from martian_redteam.catalog.vulnerabilities import get_vulnerability


class PlanError(ValueError):
    """The run file cannot be planned: a check the run cannot decide, or one it needs a model for."""


@dataclass(frozen=True)
class PlannedProbe:
    """A probe, the catalogue entries it was planned from and the method that attacks it.
    ``attack`` is the catalogue's entry for a version 0.2 method, and a stand-in for any other."""

    probe: Probe
    vulnerability: CatalogVulnerability
    attack: Attack
    severity: str | None = None
    method: AttackMethod | None = None

    @property
    def attack_method(self) -> AttackMethod:
        """The configured method; one planned without it is the catalogue method ``attack`` names."""
        return self.method if self.method is not None else resolve(AttackSpec(name=self.attack.id))

    def attempt_ids(self) -> list[str]:
        return [attempt_id(self.probe.id, number) for number in range(1, self.probe.planned_attempts + 1)]


def attempt_id(probe: str, number: int) -> str:
    return f"{probe}#{number}"


def _check_spec(check: CheckDecl) -> dict[str, Any]:
    spec = check.model_dump(exclude={"type"})
    return {key: value for key, value in spec.items() if value is not None}


def _checks(declared: list[CheckDecl], goal: Goal | None = None) -> list[Check]:
    """The probe's checks. A goal that names a tool is checked as a tool call (the ``goal`` check)
    unless a declared tool-call check already names that tool. Without any check, the judge decides."""
    checks = []
    kinds = [check.type for check in declared]
    for index, check in enumerate(declared, start=1):
        identifier = check.type if kinds.count(check.type) == 1 else f"{check.type}-{index}"
        checks.append(Check(id=identifier, kind=check.type, spec=_check_spec(check)))
    if (
        goal is not None
        and goal.tool is not None
        and not any(check.kind == "tool_call" and check.spec.get("tool") == goal.tool for check in checks)
    ):
        checks.append(
            Check(id="goal", kind="tool_call", spec={"tool": goal.tool, "where": goal.where, "executed": False})
        )
    return checks or [Check(id="judge", kind="judge")]


def _task_checks(spec: Vulnerability) -> list[Check]:
    if spec.benign is None:
        return []
    return [
        Check(id=f"task-{index}", kind=check.type, spec=_check_spec(check))
        for index, check in enumerate(spec.benign.checks, start=1)
    ]


def _goal(spec: Vulnerability) -> str:
    if spec.goal is None:
        return ""
    if spec.goal.text:
        return spec.goal.text
    where = ", ".join(f"{name} {value}" for name, value in spec.goal.where.items())
    return f"call {spec.goal.tool}" + (f" with {where}" if where else "")


def _stand_in(method: AttackMethod, weight: float | None) -> Attack:
    try:
        return get_attack(method.id, weight=weight)
    except KeyError:
        if method.kind == "transform":
            return Attack(id=method.id, label=method.label, kind="encoding", weight=weight or 1.0)
        return Attack(id=method.id, label=method.label, kind="llm", weight=weight or 1.0)


def plan(run_file: RunFile) -> list[PlannedProbe]:
    """Every probe of the run, in the run file's order."""
    try:
        methods = [resolve(item) for item in run_file.attacks]
    except MethodError as error:
        raise PlanError(f"attacks: {error}") from None
    local = run_file.models.attacker is not None or (run_file.models.judge is not None and not needs_attacker(run_file))
    attacks = [(_stand_in(method, item.weight), method) for item, method in zip(run_file.attacks, methods, strict=True)]
    planned: list[PlannedProbe] = []
    for index, spec in enumerate(run_file.vulnerabilities):
        vulnerability = get_vulnerability(spec.name, spec.types, id=spec.id, criteria=spec.criteria, gated=spec.gated)
        checks = _checks(list(spec.checks), spec.goal)
        if local and run_file.models.judge is None and any(check.kind == "judge" for check in checks):
            reason = "has no checks, so the judge decides it" if not spec.checks else "has a judge check"
            raise PlanError(f"vulnerabilities[{index}] {reason}: set models.judge")
        for vulnerability_type in vulnerability.types or vulnerability.allowed_types:
            for attack, method in attacks:
                identifier = probe_id(vulnerability.id, vulnerability_type, method.id)
                if any(item.probe.id == identifier for item in planned):
                    raise PlanError(f"vulnerabilities[{index}]: probe {identifier} is planned twice")
                probe = Probe(
                    id=identifier,
                    vulnerability=vulnerability.id,
                    vulnerability_type=vulnerability_type,
                    attack_method=method.id,
                    objective_class=spec.objective or objective_of(vulnerability.id),
                    goal=_goal(spec),
                    gated=vulnerability.gated,
                    criteria=vulnerability.criteria,
                    checks=checks,
                    planned_attempts=run_file.budget.attempts_per_probe,
                    task=spec.benign.task if spec.benign else "",
                    task_checks=_task_checks(spec),
                    metadata={
                        **_metadata(spec.severity, attack, method),
                        **({"plant": spec.plant.model_dump()} if spec.plant is not None else {}),
                    },
                )
                planned.append(PlannedProbe(probe, vulnerability, attack, spec.severity, method))
    return planned


def _metadata(severity: str | None, attack: Attack, method: AttackMethod) -> dict[str, Any]:
    if method.legacy:
        return {"severity": severity, "attack_kind": attack.kind}
    return {"severity": severity, "attack_kind": method.kind, "method": method.describe()}


__all__ = ["PlanError", "PlannedProbe", "attempt_id", "plan"]
