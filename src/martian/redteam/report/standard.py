"""The red team report standard: the numbers every report states, computed from a run.

:func:`build_report` reads a :class:`~martian.redteam.model.RedteamRun` and returns a
:class:`RedteamReport`, which ``redteam-report.json`` holds and every other output renders. It
states:

- what attacked: the service, the attacker and judge models, the attack methods and the budget,
  with what was spent and the limit that stopped the run;
- success per attempt and within k target queries, each with a Wilson 95% interval. Confirmed
  success (evidence level 2 and above) and suspected success (level 1, a model judged the reply)
  are separate rates and are never added together;
- the attacker's refusal rate, for the run and for each method. A refused generation is never a
  resisted attack;
- findings by evidence level, with what verified each and the negative control's result when the run
  carries them, and "not evaluated" when it does not;
- errors, undetermined and stopped attempts as outcomes beside the decided ones.

A rate with no success has no point value: ``rate`` is ``None`` and ``upper_bound`` is the upper end
of its 95% interval, so a report never shows 0%. ``docs/redteam-reports.md`` defines each number.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from martian.evals.stats import rate_text, wilson
from martian.redteam.model import Attempt, EvidenceLevel, Probe, RedteamRun
from martian.redteam.verify.rules import confirmed, label

REPORT_VERSION: Literal["martian-redteam-report-v1"] = "martian-redteam-report-v1"
NOT_EVALUATED = "not evaluated"
MAX_K_POINTS = 20
"""Up to this many target queries per probe, ``within_k`` has a point for every k; beyond it, a point
at 1, 2, 3, 5, 10, 20, 50, 100 and so on, and one at the largest k."""

ErrorKindName = Literal["target", "attacker", "judge", "infrastructure"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Rate(_Out):
    """``successes`` of ``trials`` with its Wilson 95% interval (``low``, ``high``).

    With no trial, every value is ``None``. With no success, ``rate`` is ``None`` and ``upper_bound``
    is ``high``: zero successes bound the rate, they do not measure it as 0%. ``display`` is the text
    every output shows."""

    successes: int = Field(ge=0)
    trials: int = Field(ge=0)
    rate: float | None = None
    low: float | None = None
    high: float | None = None
    upper_bound: float | None = None
    display: str


def rate_of(successes: int, trials: int) -> Rate:
    """The :class:`Rate` of ``successes`` in ``trials``."""
    if trials == 0:
        return Rate(successes=0, trials=0, display="no decided attempts")
    interval = wilson(successes, trials)
    assert interval is not None
    low, high = interval
    if successes == 0:
        bound = math.ceil(high * 1000 - 1e-9) / 10
        return Rate(
            successes=0,
            trials=trials,
            low=low,
            high=high,
            upper_bound=high,
            display=f"≤{bound:g}% (0 of {trials}, 95% upper bound)",
        )
    rate = successes / trials
    return Rate(
        successes=successes,
        trials=trials,
        rate=rate,
        low=low,
        high=high,
        display=f"{rate_text(rate, interval)}, {successes} of {trials}",
    )


class ModelInfo(_Out):
    """A model the run called: its provider and name, and its settings."""

    model: str
    inference_params: dict[str, Any] = Field(default_factory=dict)


class Budget(_Out):
    """What the run was allowed and what it spent. ``attempts_per_probe`` is the most attempts any
    probe planned. ``stopped_by`` names the limit that stopped the run."""

    limits: dict[str, Any] = Field(default_factory=dict)
    spent: dict[str, Any] = Field(default_factory=dict)
    stopped_by: str | None = None
    attempts_per_probe: int | None = None


class ErrorCounts(_Out):
    target: int = 0
    attacker: int = 0
    judge: int = 0
    infrastructure: int = 0


class Outcomes(_Out):
    """The attack attempts by outcome. ``decided`` is hits plus resisted; every rate is over it.
    ``confirmed_hits`` reached level 2 or above, ``suspected_hits`` level 1 only."""

    attempts: int = 0
    decided: int = 0
    hits: int = 0
    confirmed_hits: int = 0
    suspected_hits: int = 0
    resisted: int = 0
    undetermined: int = 0
    stopped: int = 0
    errors: int = 0
    errors_by_kind: ErrorCounts = ErrorCounts()


class WithinK(_Out):
    """Success within ``k`` target queries per probe. ``probes`` counts the probes an attempt
    decided within those queries; ``undecided`` the rest."""

    k: int = Field(ge=1)
    probes: int = 0
    undecided: int = 0
    confirmed: Rate
    suspected: Rate


class Success(_Out):
    """``per_attempt``: hits over decided attempts. ``within_k``: probes that hit within k target
    queries over probes decided within them. ``queries_to_first_confirmed``: for each probe with a
    confirmed hit, the target queries it took, as their median and largest value."""

    per_attempt_confirmed: Rate
    per_attempt_suspected: Rate
    within_k: list[WithinK] = Field(default_factory=list)
    queries_to_first_confirmed_median: float | None = None
    queries_to_first_confirmed_max: int | None = None


class MethodRefusals(_Out):
    method: str
    generations: int = 0
    refusals: int = 0
    rate: Rate


class Refusals(_Out):
    """Generations the attacker refused to write or left empty, over all generations it was asked
    for. A refused generation is never counted as a resisted attack."""

    generations: int = 0
    refusals: int = 0
    rate: Rate
    by_method: list[MethodRefusals] = Field(default_factory=list)


class LevelCount(_Out):
    level: int = Field(ge=0, le=5)
    label: str
    counted_as: Literal["confirmed", "suspected", "not a finding"]
    findings: int = 0


class FindingRow(_Out):
    """One finding as the report shows it. ``verification`` is the verification outcome,
    ``reproductions`` ``"r of n"`` and ``negative_control`` passed, failed or undetermined, each
    ``"not evaluated"`` when the run carries no such result."""

    id: str
    probe_id: str
    vulnerability: str
    vulnerability_type: str
    attack_method: str
    objective_class: str
    evidence_level: int
    label: str
    counted_as: Literal["confirmed", "suspected"]
    severity: str | None = None
    status: str = "open"
    hits: int = 0
    attempts: int = 0
    verification: str = NOT_EVALUATED
    reproductions: str = NOT_EVALUATED
    negative_control: str = NOT_EVALUATED


class Findings(_Out):
    confirmed: int = 0
    suspected: int = 0
    by_level: list[LevelCount] = Field(default_factory=list)
    items: list[FindingRow] = Field(default_factory=list)


class Verifications(_Out):
    """Each probe's verification outcome. ``evaluated`` is false when the run carries none."""

    evaluated: bool = False
    verified: int = 0
    consistent: int = 0
    not_reproducible: int = 0
    unverifiable: int = 0
    note: str = ""


class Controls(_Out):
    """Negative controls: the probe run without the attack. ``passed`` held, ``failed`` showed the
    signal, ``undetermined`` neither. ``evaluated`` is false when no control ran."""

    evaluated: bool = False
    ran: int = 0
    passed: int = 0
    failed: int = 0
    undetermined: int = 0
    note: str = ""


class Group(_Out):
    """The attack attempts of one attack method or one vulnerability. A method's group also has its
    ``kind`` and ``arm`` (``control``, ``static`` or ``adaptive``), so the control arm's results read
    beside the adaptive methods', the ``target_queries`` its attempts made and the median of its
    probes' ``queries_to_first_hit``."""

    name: str
    kind: str | None = None
    arm: str | None = None
    probes: int = 0
    attempts: int = 0
    decided: int = 0
    undetermined: int = 0
    stopped: int = 0
    errors: int = 0
    confirmed: Rate
    suspected: Rate
    target_queries: int = 0
    queries_to_first_hit_median: float | None = None


class ProbeRow(_Out):
    id: str
    vulnerability: str
    vulnerability_type: str
    attack_method: str
    objective_class: str
    checks: list[str] = Field(default_factory=list)
    attempts: int = 0
    hits: int = 0
    resisted: int = 0
    undetermined: int = 0
    stopped: int = 0
    errors: int = 0
    evidence_level: int | None = None
    target_queries: int = 0
    queries_to_first_hit: int | None = None


class GateOutcome(_Out):
    """The gate's result, when the report was written by a run that applied one: ``passed`` (exit
    0), what failed it and what it only warns about."""

    passed: bool
    failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class TargetInfo(_Out):
    kind: str
    name: str = ""


class RedteamReport(_Out):
    """The red team report standard (``redteam-report.json``). ``docs/redteam-reports.md`` defines
    every field."""

    version: Literal["martian-redteam-report-v1"] = REPORT_VERSION
    run_id: str
    project_id: str
    source: str
    state: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float = 0.0
    catalog_version: str
    rules_version: str | None = None
    prompts_revision: str | None = None
    engine_version: str | None = None
    target: TargetInfo | None = None
    service: str = "unknown"
    attacker: ModelInfo | None = None
    judge: ModelInfo | None = None
    judge_used: bool = False
    methods: list[str] = Field(default_factory=list)
    budget: Budget = Budget()
    outcomes: Outcomes = Outcomes()
    success: Success
    refusals: Refusals
    findings: Findings = Findings()
    verification: Verifications = Verifications()
    controls: Controls = Controls()
    by_method: list[Group] = Field(default_factory=list)
    by_vulnerability: list[Group] = Field(default_factory=list)
    probes: list[ProbeRow] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    gate: GateOutcome | None = None


# --- computing it -----------------------------------------------------------------------------


def _queries(attempt: Attempt) -> int:
    return attempt.spend.target_queries if attempt.spend is not None else attempt.target_calls


def _confirmed_hit(attempt: Attempt) -> bool:
    return attempt.outcome == "hit" and confirmed(attempt.level)


def _suspected_hit(attempt: Attempt) -> bool:
    return attempt.outcome == "hit" and attempt.level == EvidenceLevel.JUDGED


def _model(provenance: dict[str, Any], role: str) -> ModelInfo | None:
    found = provenance.get(role)
    if isinstance(found, dict) and found.get("model"):
        return ModelInfo(model=str(found["model"]), inference_params=dict(found.get("inference_params") or {}))
    return None


def _outcomes(attempts: Sequence[Attempt]) -> Outcomes:
    kinds = {"target": 0, "attacker": 0, "judge": 0, "infrastructure": 0}
    for attempt in attempts:
        if attempt.outcome == "error":
            kinds[attempt.error_kind or "infrastructure"] += 1
    hits = sum(attempt.outcome == "hit" for attempt in attempts)
    resisted = sum(attempt.outcome == "resisted" for attempt in attempts)
    return Outcomes(
        attempts=len(attempts),
        decided=hits + resisted,
        hits=hits,
        confirmed_hits=sum(_confirmed_hit(attempt) for attempt in attempts),
        suspected_hits=sum(_suspected_hit(attempt) for attempt in attempts),
        resisted=resisted,
        undetermined=sum(attempt.outcome == "undetermined" for attempt in attempts),
        stopped=sum(attempt.outcome == "stopped" for attempt in attempts),
        errors=sum(attempt.outcome == "error" for attempt in attempts),
        errors_by_kind=ErrorCounts(**kinds),
    )


def _k_points(largest: int) -> list[int]:
    if largest <= MAX_K_POINTS:
        return list(range(1, largest + 1))
    points: set[int] = {3, largest}
    scale = 1
    while scale <= largest:
        points |= {step * scale for step in (1, 2, 5) if step * scale <= largest}
        scale *= 10
    return sorted(points)


def _cumulative(attempts: Sequence[Attempt]) -> list[tuple[int, Attempt]]:
    """Each attempt of one probe, in order, with the target queries made up to and including it."""
    total, found = 0, []
    for attempt in sorted(attempts, key=lambda item: item.number):
        total += _queries(attempt)
        found.append((total, attempt))
    return found


def _within_k(by_probe: dict[str, list[Attempt]]) -> list[WithinK]:
    tracks = {probe: _cumulative(attempts) for probe, attempts in by_probe.items()}
    largest = max((track[-1][0] for track in tracks.values() if track), default=0)
    points = []
    for k in _k_points(largest):
        decided = confirmed_hits = suspected_hits = 0
        for track in tracks.values():
            reached = [attempt for used, attempt in track if used <= k]
            if not any(attempt.outcome in ("hit", "resisted") for attempt in reached):
                continue
            decided += 1
            if any(_confirmed_hit(attempt) for attempt in reached):
                confirmed_hits += 1
            elif any(_suspected_hit(attempt) for attempt in reached):
                suspected_hits += 1
        points.append(
            WithinK(
                k=k,
                probes=decided,
                undecided=len(tracks) - decided,
                confirmed=rate_of(confirmed_hits, decided),
                suspected=rate_of(suspected_hits, decided),
            )
        )
    return points


def _first_hit(track: list[tuple[int, Attempt]], *, confirmed_only: bool) -> int | None:
    for used, attempt in track:
        if (_confirmed_hit(attempt) if confirmed_only else attempt.outcome == "hit") and _queries(attempt) > 0:
            return used
    return None


def _refusals(run: RedteamRun, attempts: Sequence[Attempt]) -> Refusals:
    methods = {probe.id: probe.attack_method for probe in run.probes}
    found: dict[str, list[int]] = {}
    for attempt in attempts:
        item = found.setdefault(methods[attempt.probe_id], [0, 0])
        item[0] += attempt.spend.attacker_calls if attempt.spend is not None else 0
        item[1] += attempt.attacker_refusals
    generations = sum(item[0] for item in found.values())
    refusals = sum(item[1] for item in found.values())
    return Refusals(
        generations=generations,
        refusals=refusals,
        rate=rate_of(refusals, generations),
        by_method=[
            MethodRefusals(method=method, generations=total, refusals=refused, rate=rate_of(refused, total))
            for method, (total, refused) in found.items()
        ],
    )


def method_arm(probe: Probe) -> tuple[str | None, str | None]:
    """A probe's method kind and arm, from what the plan recorded: a configured method's own, a
    version 0.2 method's from its catalogue kind (an encoding is a transform, the rest rewrites, all
    static), and none for a probe of another source, such as a hosted campaign."""
    method = probe.metadata.get("method")
    if isinstance(method, dict) and method.get("kind") and method.get("arm"):
        return str(method["kind"]), str(method["arm"])
    legacy = {"encoding": "transform", "llm": "rewrite"}.get(str(probe.metadata.get("attack_kind")))
    return (legacy, "static") if legacy else (None, None)


def _method_group(name: str, probes: list[Probe], attempts: Sequence[Attempt], rows: dict[str, ProbeRow]) -> Group:
    kinds = {method_arm(probe) for probe in probes}
    kind, arm = kinds.pop() if len(kinds) == 1 else (None, None)
    firsts = [found for probe in probes if (found := rows[probe.id].queries_to_first_hit) is not None]
    return _group(name, probes, attempts).model_copy(
        update={
            "kind": kind,
            "arm": arm,
            "target_queries": sum(_queries(attempt) for attempt in attempts),
            "queries_to_first_hit_median": float(median(firsts)) if firsts else None,
        }
    )


def _group(name: str, probes: Iterable[Probe], attempts: Sequence[Attempt]) -> Group:
    counted = _outcomes(attempts)
    return Group(
        name=name,
        probes=len(list(probes)),
        attempts=counted.attempts,
        decided=counted.decided,
        undetermined=counted.undetermined,
        stopped=counted.stopped,
        errors=counted.errors,
        confirmed=rate_of(counted.confirmed_hits, counted.decided),
        suspected=rate_of(counted.suspected_hits, counted.decided),
    )


def _control_word(passed: bool | None, ran: bool) -> str:
    if passed is True:
        return "passed"
    if passed is False:
        return "failed"
    return "undetermined" if ran else NOT_EVALUATED


def _findings(run: RedteamRun, attack: dict[str, list[Attempt]]) -> Findings:
    verifications = {item.probe_id: item for item in run.verifications}
    controls = {attempt.probe_id for attempt in run.attempts if attempt.role == "control"}
    rows = []
    for finding in run.findings:
        verification = verifications.get(finding.probe_id)
        probe = run.probe(finding.probe_id)
        attempts = attack.get(finding.probe_id, [])
        rows.append(
            FindingRow(
                id=finding.id,
                probe_id=finding.probe_id,
                vulnerability=finding.vulnerability,
                vulnerability_type=finding.vulnerability_type,
                attack_method=probe.attack_method,
                objective_class=finding.objective_class,
                evidence_level=int(finding.evidence_level),
                label=finding.label,
                counted_as="confirmed" if confirmed(finding.evidence_level) else "suspected",
                severity=finding.severity,
                status=finding.status,
                hits=sum(attempt.outcome == "hit" for attempt in attempts),
                attempts=len(attempts),
                verification=verification.outcome if verification is not None else NOT_EVALUATED,
                reproductions=(
                    f"{verification.successful_reproductions} of {verification.repetitions}"
                    if verification is not None and verification.repetitions
                    else NOT_EVALUATED
                ),
                negative_control=_control_word(
                    verification.negative_control_passed if verification is not None else None,
                    finding.probe_id in controls,
                ),
            )
        )
    levels = []
    for level in EvidenceLevel:
        if level == EvidenceLevel.DELIVERED:
            continue
        counted_as: Literal["confirmed", "suspected"] = "confirmed" if confirmed(level) else "suspected"
        levels.append(
            LevelCount(
                level=int(level),
                label=label(level, executed=True) if level == EvidenceLevel.TOOL_CALL else label(level),
                counted_as=counted_as,
                findings=sum(finding.evidence_level == level for finding in run.findings),
            )
        )
    return Findings(
        confirmed=sum(confirmed(finding.evidence_level) for finding in run.findings),
        suspected=sum(finding.evidence_level == EvidenceLevel.JUDGED for finding in run.findings),
        by_level=levels,
        items=rows,
    )


def _verifications(run: RedteamRun) -> Verifications:
    if not run.verifications:
        return Verifications(note="Not evaluated: the run carries no verification.")
    outcomes = [item.outcome for item in run.verifications]
    return Verifications(
        evaluated=True,
        verified=outcomes.count("verified"),
        consistent=outcomes.count("consistent"),
        not_reproducible=outcomes.count("not_reproducible"),
        unverifiable=outcomes.count("unverifiable"),
    )


def _controls(run: RedteamRun) -> Controls:
    ran = [attempt for attempt in run.attempts if attempt.role == "control"]
    if ran:
        passed = sum(attempt.outcome == "resisted" for attempt in ran)
        failed = sum(attempt.outcome == "hit" for attempt in ran)
        return Controls(
            evaluated=True, ran=len(ran), passed=passed, failed=failed, undetermined=len(ran) - passed - failed
        )
    told = [item.negative_control_passed for item in run.verifications if item.negative_control_passed is not None]
    if told:
        passed = sum(told)
        return Controls(evaluated=True, ran=len(told), passed=passed, failed=len(told) - passed)
    return Controls(note="Not evaluated: no negative control ran.")


def build_report(run: RedteamRun, *, gate: GateOutcome | None = None) -> RedteamReport:
    """The report standard for ``run``. Counts and rates are of attack attempts; repetitions and
    controls are reported under verification and controls."""
    attack = [attempt for attempt in run.attempts if attempt.role == "attack"]
    by_probe: dict[str, list[Attempt]] = {probe.id: [] for probe in run.probes}
    for attempt in attack:
        by_probe[attempt.probe_id].append(attempt)
    outcomes = _outcomes(attack)
    tracks = {probe: _cumulative(attempts) for probe, attempts in by_probe.items()}
    firsts = [found for track in tracks.values() if (found := _first_hit(track, confirmed_only=True)) is not None]
    methods = list(dict.fromkeys(probe.attack_method for probe in run.probes))
    vulnerabilities = list(dict.fromkeys(probe.vulnerability for probe in run.probes))
    provenance, budget = run.provenance, run.budget
    probe_rows = []
    for probe in run.probes:
        attempts = by_probe[probe.id]
        counted = _outcomes(attempts)
        levels = [attempt.level for attempt in attempts if attempt.level is not None]
        probe_rows.append(
            ProbeRow(
                id=probe.id,
                vulnerability=probe.vulnerability,
                vulnerability_type=probe.vulnerability_type,
                attack_method=probe.attack_method,
                objective_class=probe.objective_class,
                checks=[check.kind for check in probe.checks],
                attempts=counted.attempts,
                hits=counted.hits,
                resisted=counted.resisted,
                undetermined=counted.undetermined,
                stopped=counted.stopped,
                errors=counted.errors,
                evidence_level=int(max(levels)) if levels else None,
                target_queries=sum(_queries(attempt) for attempt in attempts),
                queries_to_first_hit=_first_hit(tracks[probe.id], confirmed_only=False),
            )
        )
    return RedteamReport(
        run_id=run.id,
        project_id=run.project_id,
        source=run.source,
        state=run.state,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=run.duration_seconds,
        catalog_version=run.catalog_version,
        rules_version=run.policy_revision,
        prompts_revision=run.graph_revision,
        engine_version=provenance.get("engine_version"),
        target=TargetInfo(kind=run.target.kind, name=run.target.name) if run.target is not None else None,
        service=str(provenance.get("service", "unknown")),
        attacker=_model(provenance, "attacker"),
        judge=_model(provenance, "judge"),
        judge_used=any(check.kind == "judge" for probe in run.probes for check in probe.checks),
        methods=methods,
        budget=Budget(
            limits=dict(budget.get("limits") or {}),
            spent=dict(budget.get("spent") or {}),
            stopped_by=budget.get("stopped_by"),
            attempts_per_probe=max((probe.planned_attempts for probe in run.probes), default=None),
        ),
        outcomes=outcomes,
        success=Success(
            per_attempt_confirmed=rate_of(outcomes.confirmed_hits, outcomes.decided),
            per_attempt_suspected=rate_of(outcomes.suspected_hits, outcomes.decided),
            within_k=_within_k(by_probe),
            queries_to_first_confirmed_median=float(median(firsts)) if firsts else None,
            queries_to_first_confirmed_max=max(firsts) if firsts else None,
        ),
        refusals=_refusals(run, attack),
        findings=_findings(run, by_probe),
        verification=_verifications(run),
        controls=_controls(run),
        by_method=[
            _method_group(
                method,
                [probe for probe in run.probes if probe.attack_method == method],
                [attempt for attempt in attack if run.probe(attempt.probe_id).attack_method == method],
                {row.id: row for row in probe_rows},
            )
            for method in methods
        ],
        by_vulnerability=[
            _group(
                name,
                (probe for probe in run.probes if probe.vulnerability == name),
                [attempt for attempt in attack if run.probe(attempt.probe_id).vulnerability == name],
            )
            for name in vulnerabilities
        ],
        probes=probe_rows,
        limitations=list(run.limitations),
        gate=gate,
    )


__all__ = [
    "NOT_EVALUATED",
    "REPORT_VERSION",
    "Budget",
    "Controls",
    "ErrorCounts",
    "FindingRow",
    "Findings",
    "GateOutcome",
    "Group",
    "LevelCount",
    "MethodRefusals",
    "ModelInfo",
    "Outcomes",
    "ProbeRow",
    "Rate",
    "RedteamReport",
    "Refusals",
    "Success",
    "TargetInfo",
    "Verifications",
    "WithinK",
    "build_report",
    "method_arm",
    "rate_of",
]
