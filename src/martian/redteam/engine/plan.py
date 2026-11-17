"""Plan: expand a version 2 run file into probes, each with its checks fixed before the attack runs.

One probe per vulnerability, type and attack method, with ``budget.attempts_per_probe`` attempts. Each
``attacks`` entry is resolved to its configured method (:mod:`martian.redteam.methods`). A goal that
names a tool is checked as a tool call. A built-in vulnerability with neither checks nor such a goal
is decided by its catalogue entry's default form (the judge, for every entry of catalogue 0.2); a
custom one by the judge. A vulnerability without ``severity`` takes its entry's default. The plan
calls no model and loads no target.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from martian.redteam.methods import AttackMethod, MethodError, needs_attacker, resolve
from martian.redteam.model import Check, Probe
from martian.redteam.model.maps import objective_of, probe_id
from martian.redteam.runfile import AttackSpec, CheckDecl, Goal, RunFile, Vulnerability
from martian_redteam.catalog.attacks import Attack, get_attack
from martian_redteam.catalog.entries import CatalogEntry, Form, find_entry
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


def _vulnerability_of(spec: Vulnerability) -> CatalogVulnerability:
    """The catalogue vulnerability a run file's entry names. A custom one decided by its declared
    checks or its goal's tool call needs no criteria: they are for the judge."""
    if spec.is_custom and not (spec.criteria or "").strip():
        assert spec.id is not None  # the run file checks it
        types = tuple(spec.types or ("custom",))
        return CatalogVulnerability(
            id=spec.id, label=spec.id, allowed_types=types, types=types, gated=bool(spec.gated), criteria=None
        )
    return get_vulnerability(spec.name, spec.types, id=spec.id, criteria=spec.criteria, gated=spec.gated)


def _entry_of(spec: Vulnerability) -> CatalogEntry | None:
    """The catalogue entry a run file's vulnerability names; ``None`` for a custom one."""
    return find_entry(spec.name)


def _form_of(where: str, spec: Vulnerability, entry: CatalogEntry | None) -> Form | None:
    """The form that decides the vulnerability when it declares no checks: the one the run file
    names, else the entry's default. A form decided by the goal's tool call needs a goal that names
    a tool."""
    if entry is None:
        if spec.form is not None:
            raise PlanError(f"{where}: a custom vulnerability has no catalogue forms; declare checks")
        return None
    try:
        form = entry.form(spec.form) if spec.form is not None else entry.default_form
    except KeyError as error:
        raise PlanError(f"{where}: {error.args[0]}") from None
    has_goal_tool = spec.goal is not None and spec.goal.tool is not None
    if form.goal is not None and not has_goal_tool and not spec.checks:
        others = ", ".join(item.id for item in entry.forms if item.goal is None)
        raise PlanError(
            f"{where}: {entry.name}'s form {form.id} is decided by the goal's tool call: "
            f"give a goal that names the tool and its arguments, or choose another form ({others})"
        )
    return form


NO_MEMORY = "the target keeps no memory across conversations; state target.memory to run it"
"""Why a memory poisoning probe is skipped."""


def skipped(run_file: RunFile) -> list[dict[str, str]]:
    """The vulnerabilities the run skips, with why: a memory poisoning probe when the run file says
    the target keeps no memory (no ``target.memory``)."""
    chosen, left_out = framework_selection(run_file)
    found = list(left_out)
    for spec in [*run_file.vulnerabilities, *chosen]:
        entry = _entry_of(spec)
        if entry is not None and entry.protocol == "memory" and run_file.target.memory is None:
            found.append({"vulnerability": entry.id, "reason": NO_MEMORY})
    return found


NEEDS_SETTING: dict[str, str] = {
    "file": "it sends a dataset file of your own; list HarmfulContent with type file and its dataset",
    "mcp_tool_description": "it needs tools.decoy",
    "skill_file": "it needs tools.skills_dir",
}
"""Why a type of an entry a framework selects is left out: it needs a setting the run file does not
give."""


def framework_selection(run_file: RunFile) -> tuple[list[Vulnerability], list[dict[str, str]]]:
    """The entries the run file's ``frameworks`` select that its ``vulnerabilities`` do not list, as
    vulnerabilities: every type that needs no setting the run file lacks, decided by the entry's
    default form, or by its first form that needs no goal tool call when the default needs one. And
    the entries left out, with why."""
    from martian_redteam.catalog.frameworks import FRAMEWORKS

    listed = {entry.id for spec in run_file.vulnerabilities if (entry := _entry_of(spec)) is not None}
    chosen: list[Vulnerability] = []
    left_out: list[dict[str, str]] = []
    for selection in run_file.frameworks:
        framework_id, _, item_id = selection.partition(":")
        framework = FRAMEWORKS[framework_id]
        ids = framework.vulnerabilities_of(item_id) if item_id else framework.vulnerability_ids
        for identifier in ids:
            if identifier in listed:
                continue
            listed.add(identifier)
            entry = find_entry(identifier)
            assert entry is not None
            settings = {
                "mcp_tool_description": run_file.tools.decoy is not None,
                "skill_file": run_file.tools.skills_dir is not None,
                "file": False,
            }
            types = [kind for kind in entry.types if settings.get(kind, True)]
            if not types:
                reasons = "; ".join(f"{kind}: {NEEDS_SETTING[kind]}" for kind in entry.types)
                left_out.append({"vulnerability": entry.id, "reason": f"selected by {selection}, but {reasons}"})
                continue
            form = entry.default_form
            if form.goal is not None:
                form = next(item for item in entry.forms if item.goal is None)
            chosen.append(
                Vulnerability(
                    name=entry.name,
                    types=types if len(types) < len(entry.types) else None,
                    form=None if form is entry.default_form else form.id,
                )
            )
    return chosen, left_out


def _dataset_of(
    where: str, run_file: RunFile, spec: Vulnerability, entry: CatalogEntry | None, types: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """For ``HarmfulContent``: the dataset each type sends, once its licence passes the run's
    commercial-use switch. A registered dataset by its id; ``file``, the run file's own."""
    if entry is None or entry.id != "harmful_content":
        if spec.dataset is not None:
            raise PlanError(f"{where}: dataset applies to HarmfulContent of type file")
        return {}
    from martian.redteam.catalog.datasets import DATASETS, DatasetError, check_licence

    commercial = run_file.datasets.commercial_use
    found: dict[str, dict[str, Any]] = {}
    try:
        for kind in types:
            if kind == "file":
                if spec.dataset is None:
                    raise PlanError(f"{where}: type file needs dataset: the file, its column and licence")
                check_licence(Path(spec.dataset.file).name, spec.dataset.licence, commercial_use=commercial)
                found[kind] = {"id": "file", **spec.dataset.model_dump()}
            else:
                registered = DATASETS[kind]
                check_licence(kind, registered.licence, commercial_use=commercial)
                found[kind] = {"id": kind, "licence": registered.licence}
    except DatasetError as error:
        raise PlanError(f"{where}: {error}") from None
    if spec.dataset is not None and "file" not in types:
        raise PlanError(f"{where}: dataset is the file of type file; select type file")
    return found


def _plantable(where: str, run_file: RunFile, types: tuple[str, ...]) -> None:
    """A poisoning probe's type names where it plants: the decoy server needs ``tools.decoy``, a
    skill needs ``tools.skills_dir``."""
    if "mcp_tool_description" in types and run_file.tools.decoy is None:
        raise PlanError(
            f"{where}: mcp_tool_description plants in the inert test server's tool description: "
            "set tools.decoy, and bind the agent's tools from the proxy's session"
        )
    if "skill_file" in types and run_file.tools.skills_dir is None:
        raise PlanError(
            f"{where}: skill_file plants a skill: set tools.skills_dir to the folder the agent loads skills from"
        )


def _stand_in(method: AttackMethod, weight: float | None) -> Attack:
    try:
        return get_attack(method.id, weight=weight)
    except KeyError:
        if method.kind == "transform":
            return Attack(id=method.id, label=method.label, kind="encoding", weight=weight or 1.0)
        return Attack(id=method.id, label=method.label, kind="llm", weight=weight or 1.0)


def plan(run_file: RunFile) -> list[PlannedProbe]:
    """Every probe of the run, in the run file's order. A vulnerability :func:`skipped` names has none."""
    try:
        methods = [resolve(item) for item in run_file.attacks]
    except MethodError as error:
        raise PlanError(f"attacks: {error}") from None
    local = run_file.models.attacker is not None or (run_file.models.judge is not None and not needs_attacker(run_file))
    attacks = [(_stand_in(method, item.weight), method) for item, method in zip(run_file.attacks, methods, strict=True)]
    planned: list[PlannedProbe] = []
    left_out = {item["vulnerability"] for item in skipped(run_file)}
    selected = [(f"vulnerabilities[{index}]", spec) for index, spec in enumerate(run_file.vulnerabilities)]
    selected += [(f"frameworks ({spec.name})", spec) for spec in framework_selection(run_file)[0]]
    for index, (where, spec) in enumerate(selected):
        if (found := _entry_of(spec)) is not None and found.id in left_out:
            continue
        vulnerability = _vulnerability_of(spec)
        entry = _entry_of(spec)
        form = _form_of(where, spec, entry)
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
            raise PlanError(f"{where} {reason}: set models.judge")
        dataset = _dataset_of(where, run_file, spec, entry, vulnerability.types or vulnerability.allowed_types)
        if entry is not None and entry.protocol == "planted":
            _plantable(where, run_file, vulnerability.types or vulnerability.allowed_types)
        for vulnerability_type in vulnerability.types or vulnerability.allowed_types:
            for attack, method in attacks:
                identifier = probe_id(vulnerability.id, vulnerability_type, method.id)
                if any(item.probe.id == identifier for item in planned):
                    raise PlanError(f"{where}: probe {identifier} is planned twice")
                probe = Probe(
                    id=identifier,
                    vulnerability=vulnerability.id,
                    vulnerability_type=vulnerability_type,
                    attack_method=method.id,
                    objective_class=spec.objective or objective_of(vulnerability.id),
                    goal=_goal(spec, checks),
                    gated=vulnerability.gated,
                    criteria=vulnerability.criteria,
                    checks=checks,
                    planned_attempts=run_file.budget.attempts_per_probe,
                    task=spec.benign.task if spec.benign else "",
                    task_checks=_task_checks(spec),
                    metadata={
                        **_metadata(severity, attack, method),
                        **({"protocol": entry.protocol} if entry is not None and entry.protocol != "message" else {}),
                        **({"dataset": dataset[vulnerability_type]} if dataset else {}),
                        **({"plant": spec.plant.model_dump()} if spec.plant is not None else {}),
                    },
                )
                planned.append(PlannedProbe(probe, vulnerability, attack, severity, method))
    return planned


def _metadata(severity: str | None, attack: Attack, method: AttackMethod) -> dict[str, Any]:
    if method.legacy:
        return {"severity": severity, "attack_kind": attack.kind}
    return {"severity": severity, "attack_kind": method.kind, "method": method.describe()}


__all__ = ["NO_MEMORY", "PlanError", "PlannedProbe", "attempt_id", "plan", "skipped"]
