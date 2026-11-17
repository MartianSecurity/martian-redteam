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
- with a pool of attackers, each attacker's attempts, success rates, calls and refusals, and on each
  finding the attackers whose attempts hit;
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
from martian.redteam.verify.rules import confirmed, counted_as, label

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
    ``confirmed_hits`` reached level 2 or above (a tool call that only was asked for excepted),
    ``attempted_hits`` only a tool call that was asked for and not known to run, ``suspected_hits``
    level 1 only."""

    attempts: int = 0
    decided: int = 0
    hits: int = 0
    confirmed_hits: int = 0
    attempted_hits: int = 0
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
    attempted: Rate | None = None
    """Probes whose best hit within k queries is an attempted action: a tool call of the goal that
    was asked for and not known to run (level 3). Never added to ``confirmed``."""


class Success(_Out):
    """``per_attempt``: hits over decided attempts. ``within_k``: probes that hit within k target
    queries over probes decided within them. ``queries_to_first_confirmed``: for each probe with a
    confirmed hit, the target queries it took, as their median and largest value."""

    per_attempt_confirmed: Rate
    per_attempt_suspected: Rate
    per_attempt_attempted: Rate | None = None
    """Attempted actions (a goal tool call asked for, not known to run) over decided attempts."""
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
    counted_as: Literal["confirmed", "attempted", "suspected"]
    severity: str | None = None
    status: str = "open"
    hits: int = 0
    attempts: int = 0
    attackers: list[str] = Field(default_factory=list)
    """In a run with a pool of attackers, the attackers whose attempts hit."""
    verification: str = NOT_EVALUATED
    reproductions: str = NOT_EVALUATED
    negative_control: str = NOT_EVALUATED


class Findings(_Out):
    confirmed: int = 0
    attempted: int = 0
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


class AttackerGroup(Group):
    """The attack attempts one attacker of a pool wrote: its success rates, the target queries its
    attempts made, its calls (``attacker_calls``) and the generations it refused or left empty."""

    attacker_calls: int = 0
    refusals: int = 0
    refusal_rate: Rate | None = None


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
    task_with_attack: str = NOT_EVALUATED
    """The probe's benign task: ``"k of n"`` attack attempts in which it completed."""
    task_without_attack: str = NOT_EVALUATED
    """Whether it completed in the negative control: ``completed``, ``not completed`` or
    ``undetermined``."""


class JudgeCalibration(_Out):
    """The judge measured on a labelled sample before the run. ``precision`` is its true positives
    over all it called hits; ``recall`` its true positives over the sample's real hits. ``error`` says
    why it could not be measured."""

    judge: str
    sample: int = 0
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    undetermined: int = 0
    precision: Rate | None = None
    recall: Rate | None = None
    error: str | None = None


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
    attackers: list[ModelInfo] = Field(default_factory=list)
    """A pool of attackers, when the run had one; ``attacker`` is then its first."""
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
    judge_calibration: JudgeCalibration | None = None
    by_method: list[Group] = Field(default_factory=list)
    by_vulnerability: list[Group] = Field(default_factory=list)
    by_attacker: list[AttackerGroup] = Field(default_factory=list)
    """One group per attacker of a pool, in the pool's order; empty with one attacker."""
    probes: list[ProbeRow] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    gate: GateOutcome | None = None


# --- computing it -----------------------------------------------------------------------------


def _queries(attempt: Attempt) -> int:
    return attempt.spend.target_queries if attempt.spend is not None else attempt.target_calls


def _counted(attempt: Attempt) -> str | None:
    return counted_as(attempt.check_results, attempt.level) if attempt.outcome == "hit" else None


def _confirmed_hit(attempt: Attempt) -> bool:
    return _counted(attempt) == "confirmed"


def _attempted_hit(attempt: Attempt) -> bool:
    return _counted(attempt) == "attempted"


def _suspected_hit(attempt: Attempt) -> bool:
    return _counted(attempt) == "suspected"


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
        attempted_hits=sum(_attempted_hit(attempt) for attempt in attempts),
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
        decided = confirmed_hits = attempted_hits = suspected_hits = 0
        for track in tracks.values():
            reached = [attempt for used, attempt in track if used <= k]
            if not any(attempt.outcome in ("hit", "resisted") for attempt in reached):
                continue
            decided += 1
            if any(_confirmed_hit(attempt) for attempt in reached):
                confirmed_hits += 1
                continue
            if any(_attempted_hit(attempt) for attempt in reached):
                attempted_hits += 1
            if any(_suspected_hit(attempt) for attempt in reached):
                suspected_hits += 1
        points.append(
            WithinK(
                k=k,
                probes=decided,
                undecided=len(tracks) - decided,
                confirmed=rate_of(confirmed_hits, decided),
                suspected=rate_of(suspected_hits, decided),
                attempted=rate_of(attempted_hits, decided),
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


def _models(provenance: dict[str, Any]) -> list[ModelInfo]:
    found = provenance.get("attackers")
    if not isinstance(found, list):
        return []
    return [
        ModelInfo(model=str(item["model"]), inference_params=dict(item.get("inference_params") or {}))
        for item in found
        if isinstance(item, dict) and item.get("model")
    ]


def _attacker_group(name: str, run: RedteamRun, attempts: Sequence[Attempt]) -> AttackerGroup:
    probes = {attempt.probe_id for attempt in attempts}
    group = _group(name, (probe for probe in run.probes if probe.id in probes), attempts)
    calls = sum(attempt.spend.attacker_calls if attempt.spend is not None else 0 for attempt in attempts)
    refused = sum(attempt.attacker_refusals for attempt in attempts)
    return AttackerGroup(
        **{
            **group.model_dump(),
            "target_queries": sum(_queries(attempt) for attempt in attempts),
            "attacker_calls": calls,
            "refusals": refused,
            "refusal_rate": rate_of(refused, calls),
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
                counted_as=finding.counted_as,
                severity=finding.severity,
                status=finding.status,
                hits=sum(attempt.outcome == "hit" for attempt in attempts),
                attempts=len(attempts),
                attackers=list(
                    dict.fromkeys(
                        attempt.attacker for attempt in attempts if attempt.outcome == "hit" and attempt.attacker
                    )
                ),
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
        counted_as: Literal["confirmed", "suspected"] = "confirmed" if confirmed(level, executed=True) else "suspected"
        levels.append(
            LevelCount(
                level=int(level),
                label=label(level, executed=True) if level == EvidenceLevel.TOOL_CALL else label(level),
                counted_as=counted_as,
                findings=sum(finding.evidence_level == level for finding in run.findings),
            )
        )
    return Findings(
        confirmed=sum(finding.counted_as == "confirmed" for finding in run.findings),
        attempted=sum(finding.counted_as == "attempted" for finding in run.findings),
        suspected=sum(finding.counted_as == "suspected" for finding in run.findings),
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


def _task(run: RedteamRun, probe: Probe, attempts: Sequence[Attempt]) -> tuple[str, str]:
    if not probe.task_checks:
        return NOT_EVALUATED, NOT_EVALUATED
    decided = [attempt for attempt in attempts if attempt.task_completed is not None]
    done = sum(attempt.task_completed is True for attempt in decided)
    control = next(
        (attempt for attempt in run.attempts if attempt.probe_id == probe.id and attempt.role == "control"), None
    )
    alone = NOT_EVALUATED
    if control is not None:
        alone = {True: "completed", False: "not completed", None: "undetermined"}[control.task_completed]
    return (f"{done} of {len(decided)}" if decided else NOT_EVALUATED), alone


def _calibration(found: Any) -> JudgeCalibration | None:
    if not isinstance(found, dict) or "judge" not in found:
        return None
    if "error" in found:
        return JudgeCalibration(judge=str(found["judge"]), error=str(found["error"]))
    tp, fp, fn = found["true_positive"], found["false_positive"], found["false_negative"]
    return JudgeCalibration(
        judge=str(found["judge"]),
        sample=found["sample"],
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        true_negative=found["true_negative"],
        undetermined=found["undetermined"],
        precision=rate_of(tp, tp + fp),
        recall=rate_of(tp, tp + fn),
    )


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
        levels = [attempt.level for attempt in attempts if attempt.outcome == "hit" and attempt.level is not None]
        with_attack, without_attack = _task(run, probe, attempts)
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
                task_with_attack=with_attack,
                task_without_attack=without_attack,
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
        attackers=_models(provenance),
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
            per_attempt_attempted=rate_of(outcomes.attempted_hits, outcomes.decided),
            within_k=_within_k(by_probe),
            queries_to_first_confirmed_median=float(median(firsts)) if firsts else None,
            queries_to_first_confirmed_max=max(firsts) if firsts else None,
        ),
        refusals=_refusals(run, attack),
        findings=_findings(run, by_probe),
        verification=_verifications(run),
        controls=_controls(run),
        judge_calibration=_calibration(provenance.get("judge_calibration")),
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
        by_attacker=[
            _attacker_group(info.model, run, [attempt for attempt in attack if attempt.attacker == info.model])
            for info in _models(provenance)
        ],
        probes=probe_rows,
        limitations=list(run.limitations),
        gate=gate,
    )


__all__ = [
    "NOT_EVALUATED",
    "AttackerGroup",
    "REPORT_VERSION",
    "Budget",
    "Controls",
    "ErrorCounts",
    "FindingRow",
    "Findings",
    "GateOutcome",
    "Group",
    "JudgeCalibration",
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
