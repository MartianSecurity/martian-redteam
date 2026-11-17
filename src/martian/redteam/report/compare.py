"""Compare two red team runs on the same probes.

:func:`compare_runs` pairs a baseline run and a candidate run probe by probe and says which findings
are new, fixed or unchanged, and labels the difference in confirmed success with the exact McNemar
test on the attack attempts both runs decided (:func:`martian.evals.stats.compare_outcomes`).

Attack success depends on the attacker, the judge, the budget and the rules that decide verdicts, so
two runs that differ in any of them are flagged not comparable, with a warning for each difference.
The comparison is still made; it is the reader's to discount.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from martian.evals.stats import compare_outcomes
from martian.redteam.model import Attempt, Finding, RedteamRun
from martian.redteam.verify.rules import confirmed

COMPARISON_VERSION: Literal["martian-redteam-comparison-v1"] = "martian-redteam-comparison-v1"

ChangeStatus = Literal["new", "fixed", "unchanged", "undecided"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FindingChange(_Out):
    """One probe's finding in the two runs. ``baseline_level`` and ``candidate_level`` are ``None``
    where that run has no finding for the probe."""

    probe_id: str
    vulnerability: str
    vulnerability_type: str
    attack_method: str
    status: ChangeStatus
    baseline_level: int | None = None
    candidate_level: int | None = None
    baseline_label: str | None = None
    candidate_label: str | None = None
    counted_as: Literal["confirmed", "suspected"]


class PairedTest(_Out):
    """Confirmed success of the attack attempts both runs decided, paired by probe and attempt
    number. ``label`` is ``better`` (fewer confirmed hits), ``worse`` or ``within_noise``."""

    test: str = "mcnemar_exact"
    compared: int = 0
    label: Literal["better", "worse", "within_noise"] = "within_noise"
    p_value: float | None = None
    improved: int = 0
    regressed: int = 0
    baseline_rate: float | None = None
    candidate_rate: float | None = None
    effect: float | None = None
    minimum_detectable_effect: float | None = None


class Comparison(_Out):
    """``redteam-comparison.json``. ``docs/redteam-reports.md`` defines every field."""

    version: Literal["martian-redteam-comparison-v1"] = COMPARISON_VERSION
    baseline_run: str
    candidate_run: str
    comparable: bool
    warnings: list[str] = Field(default_factory=list)
    compared_probes: int = 0
    only_in_baseline: list[str] = Field(default_factory=list)
    only_in_candidate: list[str] = Field(default_factory=list)
    new: list[FindingChange] = Field(default_factory=list)
    fixed: list[FindingChange] = Field(default_factory=list)
    unchanged: list[FindingChange] = Field(default_factory=list)
    undecided: list[FindingChange] = Field(default_factory=list)
    paired: PairedTest = PairedTest()
    changed: bool = False


def _model(run: RedteamRun, role: str) -> Any:
    found = run.provenance.get(role)
    if isinstance(found, dict):
        return {"model": found.get("model"), "inference_params": found.get("inference_params") or {}}
    return None


def _shown(value: Any) -> str:
    if isinstance(value, dict) and set(value) == {"model", "inference_params"}:
        settings = ", ".join(f"{key} {item}" for key, item in sorted(value["inference_params"].items()))
        return f"{value['model']}" + (f" ({settings})" if settings else "")
    if isinstance(value, dict):
        return ", ".join(f"{key} {item}" for key, item in sorted(value.items())) or "no limit"
    return "not recorded" if value is None else str(value)


def differences(baseline: RedteamRun, candidate: RedteamRun) -> list[str]:
    """What differs between the runs in what produced their numbers. Empty when they are comparable."""
    found = []
    pairs = [
        ("attackers", _model(baseline, "attacker"), _model(candidate, "attacker")),
        ("judges", _model(baseline, "judge"), _model(candidate, "judge")),
        ("services", baseline.provenance.get("service"), candidate.provenance.get("service")),
        ("budgets", dict(baseline.budget.get("limits") or {}), dict(candidate.budget.get("limits") or {})),
        (
            "attempts per probe",
            max((probe.planned_attempts for probe in baseline.probes), default=None),
            max((probe.planned_attempts for probe in candidate.probes), default=None),
        ),
        ("catalogue versions", baseline.catalog_version, candidate.catalog_version),
        ("verdict rules", baseline.policy_revision, candidate.policy_revision),
        ("attacker and judge prompts", baseline.graph_revision, candidate.graph_revision),
    ]
    for name, before, after in pairs:
        if before != after:
            found.append(f"different {name}: {_shown(before)} in the baseline, {_shown(after)} in the candidate")
    checks_before = {probe.id: [check.model_dump() for check in probe.checks] for probe in baseline.probes}
    for probe in candidate.probes:
        if probe.id in checks_before and checks_before[probe.id] != [check.model_dump() for check in probe.checks]:
            found.append(f"different checks for {probe.id}")
    return found


def _attack(run: RedteamRun) -> dict[tuple[str, int], Attempt]:
    return {(attempt.probe_id, attempt.number): attempt for attempt in run.attempts if attempt.role == "attack"}


def _change(
    probe_id: str, run: RedteamRun, status: ChangeStatus, before: Finding | None, after: Finding | None
) -> FindingChange:
    probe = run.probe(probe_id)
    shown = after or before
    assert shown is not None
    return FindingChange(
        probe_id=probe_id,
        vulnerability=probe.vulnerability,
        vulnerability_type=probe.vulnerability_type,
        attack_method=probe.attack_method,
        status=status,
        baseline_level=None if before is None else int(before.evidence_level),
        candidate_level=None if after is None else int(after.evidence_level),
        baseline_label=None if before is None else before.label,
        candidate_label=None if after is None else after.label,
        counted_as="confirmed" if confirmed(shown.evidence_level) else "suspected",
    )


def compare_runs(baseline: RedteamRun, candidate: RedteamRun) -> Comparison:
    """``candidate`` compared with ``baseline`` on the probes both ran."""
    warnings = differences(baseline, candidate)
    before_probes = {probe.id for probe in baseline.probes}
    after_probes = {probe.id for probe in candidate.probes}
    shared = [probe.id for probe in candidate.probes if probe.id in before_probes]
    before = {finding.probe_id: finding for finding in baseline.findings}
    after = {finding.probe_id: finding for finding in candidate.findings}
    decided = {
        attempt.probe_id
        for attempt in candidate.attempts
        if attempt.role == "attack" and attempt.outcome in ("hit", "resisted")
    }
    changes: dict[ChangeStatus, list[FindingChange]] = {"new": [], "fixed": [], "unchanged": [], "undecided": []}
    for probe_id in shared:
        old, now = before.get(probe_id), after.get(probe_id)
        if old is None and now is None:
            continue
        if old is None:
            status: ChangeStatus = "new"
        elif now is not None:
            status = "unchanged"
        else:
            status = "fixed" if probe_id in decided else "undecided"
        changes[status].append(_change(probe_id, candidate, status, old, now))
    first, second = _attack(baseline), _attack(candidate)
    pairs = [
        (first[key], second[key])
        for key in sorted(set(first) & set(second))
        if key[0] in after_probes
        and first[key].outcome in ("hit", "resisted")
        and second[key].outcome in ("hit", "resisted")
    ]
    tested = compare_outcomes(
        [attempt.outcome == "hit" and confirmed(attempt.level) for attempt, _ in pairs],
        [attempt.outcome == "hit" and confirmed(attempt.level) for _, attempt in pairs],
        higher_is_better=False,
    )
    paired = (
        PairedTest()
        if tested is None
        else PairedTest(
            compared=tested.compared,
            label=tested.label,
            p_value=tested.p_value,
            improved=tested.improved,
            regressed=tested.regressed,
            baseline_rate=tested.baseline,
            candidate_rate=tested.candidate,
            effect=tested.effect,
            minimum_detectable_effect=tested.minimum_detectable_effect,
        )
    )
    moved = any(item.baseline_level != item.candidate_level for item in changes["unchanged"])
    return Comparison(
        baseline_run=baseline.id,
        candidate_run=candidate.id,
        comparable=not warnings,
        warnings=warnings,
        compared_probes=len(shared),
        only_in_baseline=sorted(before_probes - after_probes),
        only_in_candidate=sorted(after_probes - before_probes),
        new=changes["new"],
        fixed=changes["fixed"],
        unchanged=changes["unchanged"],
        undecided=changes["undecided"],
        paired=paired,
        changed=bool(
            changes["new"] or changes["fixed"] or changes["undecided"] or moved or paired.improved or paired.regressed
        ),
    )


def render_markdown(comparison: Comparison) -> str:
    lines = [f"# Red team run {comparison.candidate_run} compared with {comparison.baseline_run}", ""]
    if not comparison.comparable:
        lines.append("**Not comparable.** Attack success depends on what produced it, and these runs differ:")
        lines += [f"- {item}" for item in comparison.warnings]
        lines.append("")
    paired = comparison.paired
    lines.append(
        "No change."
        if not comparison.changed
        else f"{len(comparison.new)} new, {len(comparison.fixed)} fixed, "
        f"{len(comparison.unchanged)} unchanged findings."
    )
    lines.append(
        f"Compared on {comparison.compared_probes} probes. Confirmed success over {paired.compared} attack "
        f"attempts both runs decided: {paired.label.replace('_', ' ')}"
        + (
            ""
            if paired.p_value is None
            else f" (exact McNemar p = {paired.p_value:.3g}, {paired.improved} fewer hits, {paired.regressed} more)"
        )
        + "."
    )
    for title, items in (
        ("New findings", comparison.new),
        ("Fixed findings", comparison.fixed),
        ("Unchanged findings", comparison.unchanged),
        ("Findings the candidate did not decide", comparison.undecided),
    ):
        if not items:
            continue
        lines += [
            "",
            f"## {title}",
            "",
            "| Vulnerability | Type | Attack | Baseline | Candidate |",
            "| --- | --- | --- | --- | --- |",
        ]
        for item in items:
            was = "-" if item.baseline_level is None else f"{item.baseline_label} (level {item.baseline_level})"
            now = "-" if item.candidate_level is None else f"{item.candidate_label} (level {item.candidate_level})"
            lines.append(f"| {item.vulnerability} | {item.vulnerability_type} | {item.attack_method} | {was} | {now} |")
    for title, names in (
        ("Probes only in the baseline", comparison.only_in_baseline),
        ("Probes only in the candidate", comparison.only_in_candidate),
    ):
        if names:
            lines += ["", f"## {title}", ""] + [f"- {name}" for name in names]
    return "\n".join(lines) + "\n"


__all__ = [
    "COMPARISON_VERSION",
    "Comparison",
    "FindingChange",
    "PairedTest",
    "compare_runs",
    "differences",
    "render_markdown",
]
