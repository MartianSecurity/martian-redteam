"""Plan: expand a version 2 run file into probes, each with its checks fixed before the attack runs.

One probe per vulnerability, type and attack method, with ``budget.attempts_per_probe`` attempts. A
goal that names a tool is checked as a tool call. A built-in vulnerability with neither checks nor
such a goal is decided by its catalogue entry's default form (the judge, for every entry of catalogue
0.2); a custom one by the judge. A vulnerability without ``severity`` takes its entry's default. The
plan calls no model and loads no target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from martian.redteam.model import Check, Probe
from martian.redteam.model.maps import objective_of, probe_id
from martian.redteam.runfile import CheckDecl, Goal, RunFile, Vulnerability
from martian_redteam.catalog.attacks import Attack, get_attack
from martian_redteam.catalog.entries import CatalogEntry, Form, find_entry
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
    # A switch left off is not written, so a check reads as it did before the switch existed.
    return {key: value for key, value in spec.items() if value is not None and not (key == "with_canary" and not value)}


def _form_checks(form: Form) -> list[CheckDecl]:
    """A catalogue form's checks, as the run file declares checks."""
    from pydantic import TypeAdapter

    adapter: TypeAdapter[CheckDecl] = TypeAdapter(CheckDecl)
    return [adapter.validate_python(dict(check)) for check in form.checks]


def _checks(
    declared: list[CheckDecl], goal: Goal | None = None, form: Form | None = None, *, chosen: bool = False
) -> list[Check]:
    """The probe's checks. A goal that names a tool is checked as a tool call (the ``goal`` check)
    unless a declared tool-call check already names that tool. Without a declared check, the form
    decides with the goal when the run file ``chosen`` it, when it uses the goal itself, or when the
    goal names no tool; else such a goal alone decides. Without a form, the judge. A form whose goal
    call must carry the canary makes the ``goal`` check say so (``with_canary``)."""
    has_goal_tool = goal is not None and goal.tool is not None
    use_form = not declared and form is not None and (chosen or form.goal is not None or not has_goal_tool)
    if use_form:
        assert form is not None
        declared = _form_checks(form)
    with_canary = use_form and form is not None and form.goal == "tool_with_canary"
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
        spec: dict[str, Any] = {"tool": goal.tool, "where": goal.where, "executed": False}
        if with_canary:
            spec["with_canary"] = True
        checks.append(Check(id="goal", kind="tool_call", spec=spec))
    return checks or [Check(id="judge", kind="judge")]


def _task_checks(spec: Vulnerability) -> list[Check]:
    if spec.benign is None:
        return []
    return [
        Check(id=f"task-{index}", kind=check.type, spec=_check_spec(check))
        for index, check in enumerate(spec.benign.checks, start=1)
    ]


CARRY_CANARY = "passing the confidential reference it was given in an argument"
"""What the attacker is told besides the goal's call when the call must carry the run's canary."""


def _goal(spec: Vulnerability, checks: list[Check] | None = None) -> str:
    if spec.goal is None:
        return ""
    if spec.goal.text:
        return spec.goal.text
    where = ", ".join(f"{name} {value}" for name, value in spec.goal.where.items())
    text = f"call {spec.goal.tool}" + (f" with {where}" if where else "")
    if any(check.kind == "tool_call" and check.spec.get("with_canary") for check in checks or []):
        text += f", {CARRY_CANARY}"
    return text


def _entry_of(spec: Vulnerability) -> CatalogEntry | None:
    """The catalogue entry a run file's vulnerability names; ``None`` for a custom one."""
    return find_entry(spec.name)


def _form_of(index: int, spec: Vulnerability, entry: CatalogEntry | None) -> Form | None:
    """The form that decides the vulnerability when it declares no checks: the one the run file
    names, else the entry's default. A form decided by the goal's tool call needs a goal that names
    a tool."""
    if entry is None:
        if spec.form is not None:
            raise PlanError(f"vulnerabilities[{index}]: a custom vulnerability has no catalogue forms; declare checks")
        return None
    try:
        form = entry.form(spec.form) if spec.form is not None else entry.default_form
    except KeyError as error:
        raise PlanError(f"vulnerabilities[{index}]: {error.args[0]}") from None
    has_goal_tool = spec.goal is not None and spec.goal.tool is not None
    if form.goal is not None and not has_goal_tool and not spec.checks:
        others = ", ".join(item.id for item in entry.forms if item.goal is None)
        raise PlanError(
            f"vulnerabilities[{index}]: {entry.name}'s form {form.id} is decided by the goal's tool call: "
            f"give a goal that names the tool and its arguments, or choose another form ({others})"
        )
    return form


NO_MEMORY = "the target keeps no memory across conversations; state target.memory to run it"
"""Why a memory poisoning probe is skipped."""


def skipped(run_file: RunFile) -> list[dict[str, str]]:
    """The vulnerabilities the run skips, with why: a memory poisoning probe when the run file says
    the target keeps no memory (no ``target.memory``)."""
    found = []
    for spec in run_file.vulnerabilities:
        entry = _entry_of(spec)
        if entry is not None and entry.protocol == "memory" and run_file.target.memory is None:
            found.append({"vulnerability": entry.id, "reason": NO_MEMORY})
    return found


def _plantable(index: int, run_file: RunFile, types: tuple[str, ...]) -> None:
    """A poisoning probe's type names where it plants: the decoy server needs ``tools.decoy``, a
    skill needs ``tools.skills_dir``."""
    if "mcp_tool_description" in types and run_file.tools.decoy is None:
        raise PlanError(
            f"vulnerabilities[{index}]: mcp_tool_description plants in the inert test server's tool description: "
            "set tools.decoy, and bind the agent's tools from the proxy's session"
        )
    if "skill_file" in types and run_file.tools.skills_dir is None:
        raise PlanError(
            f"vulnerabilities[{index}]: skill_file plants a skill: set tools.skills_dir to the folder the agent loads "
            "skills from"
        )


def plan(run_file: RunFile) -> list[PlannedProbe]:
    """Every probe of the run, in the run file's order. A vulnerability :func:`skipped` names has none."""
    local = run_file.models.attacker is not None
    attacks = [get_attack(item.name, weight=item.weight) for item in run_file.attacks]
    planned: list[PlannedProbe] = []
    left_out = {item["vulnerability"] for item in skipped(run_file)}
    for index, spec in enumerate(run_file.vulnerabilities):
        if (found := _entry_of(spec)) is not None and found.id in left_out:
            continue
        vulnerability = get_vulnerability(spec.name, spec.types, id=spec.id, criteria=spec.criteria, gated=spec.gated)
        entry = _entry_of(spec)
        form = _form_of(index, spec, entry)
        checks = _checks(list(spec.checks), spec.goal, form, chosen=spec.form is not None)
        if not spec.checks and any(
            check.kind == "canary" and check.spec.get("plant") == "system_prompt" for check in checks
        ):
            try:
                run_file._templated_target(index)
            except ValueError as error:
                raise PlanError(str(error)) from None
        severity = spec.severity or (entry.severity if entry is not None else None)
        if local and run_file.models.judge is None and any(check.kind == "judge" for check in checks):
            reason = "has no checks, so the judge decides it" if not spec.checks else "has a judge check"
            raise PlanError(f"vulnerabilities[{index}] {reason}: set models.judge")
        if entry is not None and entry.protocol == "planted":
            _plantable(index, run_file, vulnerability.types or vulnerability.allowed_types)
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
                    goal=_goal(spec, checks),
                    gated=vulnerability.gated,
                    criteria=vulnerability.criteria,
                    checks=checks,
                    planned_attempts=run_file.budget.attempts_per_probe,
                    task=spec.benign.task if spec.benign else "",
                    task_checks=_task_checks(spec),
                    metadata={
                        "severity": severity,
                        "attack_kind": attack.kind,
                        **({"protocol": entry.protocol} if entry is not None and entry.protocol != "message" else {}),
                    },
                )
                planned.append(PlannedProbe(probe, vulnerability, attack, severity))
    return planned


__all__ = ["NO_MEMORY", "PlanError", "PlannedProbe", "attempt_id", "plan", "skipped"]
