"""The shared red team data model: one set of objects for SDK runs, the local edition and hosted campaigns.

A probe is one planned test: a vulnerability type, an attack method, an objective and the checks
that decide it, all fixed before the attack runs. An attempt is one execution of a probe against
the target: its conversation, the tool calls in it, the result of each check and the evidence they
rest on. A verification combines a probe's repetitions and its negative control into an outcome,
and a finding is what a run reports for a probe whose attempts reached evidence.

The terms are the hosted engine's, so a finding reads the same in every edition: the objective
class (disclosure, control, action, persistence), the evidence tier (text, tool, state), the
verification outcome (verified, consistent, not reproducible, unverifiable) and the reproduction
status. The evidence level (0 to 5) and the rules that combine checks into an outcome are in
:mod:`martian.redteam.verify` and ``docs/redteam-verdicts.md``.

``docs/redteam-model.md`` maps the version 0.2 SDK report, the rows of a CI session and the
engine's campaign summary onto these objects (:mod:`martian.redteam.model.maps`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MODEL_VERSION = "martian-redteam-model-v1"

Identifier = Annotated[str, Field(min_length=1, max_length=200)]

ObjectiveClass = Literal["disclosure", "control", "action", "persistence"]
"""What a probe tries to establish, which decides what kind of evidence can prove it. For a
disclosure or a control objective, what the target says is the breach itself. For an action or a
persistence objective, the breach is a change in the world, and a reply is at most a claim of one."""

EvidenceTier = Literal["text", "tool", "state"]
"""What evidence is about: what the target said, what it invoked, or what changed in the world."""

VerificationOutcome = Literal["verified", "consistent", "not_reproducible", "unverifiable"]
"""``verified``: the behaviour reproduced on evidence strong enough for the objective.
``consistent``: it reproduced, but the evidence is weaker than the objective needs (a ceiling).
``not_reproducible``: deterministic checks ran and it did not reproduce.
``unverifiable``: nothing could decide it: a check was undetermined, or only a model judged it."""

ReproductionStatus = Literal["reproduced_clean", "state_dependent", "not_reproduced", "untested"]

AttemptOutcome = Literal["hit", "resisted", "undetermined", "error", "stopped"]
"""Three outcomes decide an attempt, and errors stay outside them. ``hit`` (the target was
vulnerable): a check found the attack succeeded. ``resisted`` (the target held): every check ran and
none did. ``undetermined``: a check could not decide (an unreadable judgment, a missing observation).
``error``: the attempt did not complete, and ``Attempt.error_kind`` says whose failure it was.
``stopped``: the run's budget ran out before the attempt was decided; ``Attempt.stopped_by`` names the
limit. A stopped attempt is never resisted. Rates are computed over decided attempts (hits and
resisted), with the undetermined count beside them."""

CheckKind = Literal["judge", "text", "canary", "tool_call", "receipt", "state"]
ErrorKind = Literal["infrastructure", "target", "attacker", "judge"]
"""Whose failure stopped an attempt: the platform or the harness (``infrastructure``), the target, the
model that writes the attacks, or the model that judges the replies."""
RunSource = Literal["local", "console", "ci", "campaign"]
RunState = Literal["queued", "running", "completed", "partial", "failed", "cancelled", "stopped"]
FindingStatus = Literal["open", "accepted", "fixed", "false_positive"]


class EvidenceLevel(IntEnum):
    """How strong the evidence of an attempt is. A finding carries the highest level it reached."""

    DELIVERED = 0
    """The attack reached what the agent read. Not a finding."""
    JUDGED = 1
    """A model judged the reply. Suspected, never added to the confirmed count."""
    TEXT = 2
    """A text check stated in advance, or a planted canary, matched the reply."""
    TOOL_CALL = 3
    """A tool call matched the declared goal: requested, or executed."""
    RECEIPT = 4
    """A local listener received a request carrying this run's canary."""
    STATE = 5
    """Business records changed in a reset world and the no-implant control stayed clean."""


class _Object(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCall(_Object):
    """One tool call the target made or asked for. ``executed`` is ``True`` when something that runs
    the tool (a proxy, the agent's own dispatcher, a trace) saw it run, ``False`` when it was known
    not to run (blocked), and ``None`` when only the request is known."""

    name: Identifier
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None
    executed: bool | None = None
    result: str | None = None


class Spend(_Object):
    """What an attempt, a probe or a run spent: calls of the target, of the attacker model and of the
    judge model, the tokens those model calls reported and their cost when it is known."""

    target_queries: int = Field(default=0, ge=0)
    attacker_calls: int = Field(default=0, ge=0)
    judge_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)

    @property
    def model_calls(self) -> int:
        return self.attacker_calls + self.judge_calls

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Spend) -> Spend:
        return Spend(
            target_queries=self.target_queries + other.target_queries,
            attacker_calls=self.attacker_calls + other.attacker_calls,
            judge_calls=self.judge_calls + other.judge_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


BudgetLimit = Literal["target_queries", "model_calls", "tokens", "cost_usd", "max_seconds"]
"""A limit of the run file's ``budget`` that stopped a run."""

BUDGET_USAGE_UNAVAILABLE = "budget_usage_unavailable"


def budget_usage_error(budget: dict[str, Any]) -> str | None:
    """The failure shared by the gate and reports, including a separately metered re-judge."""
    for prefix, record in (("Re-judge: ", budget.get("rejudge_budget")), ("", budget)):
        if isinstance(record, dict) and record.get("stop_reason") == BUDGET_USAGE_UNAVAILABLE:
            limit = record.get("stopped_by")
            name = limit if limit in ("tokens", "cost_usd") else "configured spend"
            return f"{prefix}{BUDGET_USAGE_UNAVAILABLE}: required model usage for the {name} cap is unavailable"
    return None


class Turn(_Object):
    """One message of an attempt's conversation."""

    role: Literal["attacker", "target", "tool", "system"]
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)


class Check(_Object):
    """A rule stated before the attack runs, which decides whether it succeeded. ``spec`` holds the
    kind's own settings: the markers of a text check, the canary, the goal of a tool-call check, the
    criteria of a judge."""

    id: Identifier
    kind: CheckKind
    spec: dict[str, Any] = Field(default_factory=dict)


class CheckResult(_Object):
    """One check's verdict on one attempt. ``hit`` is ``None`` when the check could not decide: a
    judge's answer was unreadable, or what it needed was not observed. That is never a pass."""

    check_id: Identifier
    kind: CheckKind
    hit: bool | None = None
    level: EvidenceLevel | None = None
    """The level this result supports when ``hit`` is true."""
    executed: bool | None = None
    """For a tool-call check: whether the matching call ran (level 3, confirmed) or was only asked for."""
    rationale: str = ""
    error: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)
    source: str = ""
    """What produced the result: ``rule`` for a check decided by code, ``judge:<model>`` or
    ``judge:service`` for a model, ``listener`` for the receipt listener, ``server`` for a state check."""
    verifier: str = ""
    """The verifier and its version, such as ``text-v1``. A re-judged attempt names the one that
    decided it again."""


class Evidence(_Object):
    """Raw evidence captured at a boundary the harness trusts: a reply, a tool call, a receipt, a
    state diff. ``sha256`` is over ``content`` when the content is kept."""

    id: Identifier
    kind: Literal["reply", "tool_call", "receipt", "state", "payload", "trace"]
    tier: EvidenceTier | None = None
    source: str = ""
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content: str | None = None
    captured_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Probe(_Object):
    """One planned test. Its checks are fixed before the attack runs."""

    id: Identifier
    vulnerability: Identifier
    vulnerability_type: Identifier
    attack_method: Identifier
    objective_class: ObjectiveClass
    goal: str = ""
    gated: bool = False
    criteria: str | None = None
    checks: list[Check] = Field(default_factory=list)
    planned_attempts: int = Field(default=1, ge=1)
    task: str = ""
    """A benign task the target should still complete under attack: sent with the attack, and alone
    in the negative control."""
    task_checks: list[Check] = Field(default_factory=list)
    """The checks that show the benign task completed."""
    metadata: dict[str, Any] = Field(default_factory=dict)


class Attempt(_Object):
    """One execution of a probe against the target."""

    id: Identifier
    probe_id: Identifier
    number: int = Field(ge=1)
    outcome: AttemptOutcome
    level: EvidenceLevel | None = None
    """The highest level a hit reached. An attempt whose attack reached the target and did not hit is
    at level 0, delivered; one that never reached the target has none."""
    input: str | None = None
    """What the attacker wrote."""
    planted: str | None = None
    """What the harness sent with the attack in the same message, such as a note holding the run's
    canary. The first turn holds the message the target received."""
    output: str | None = None
    turns: list[Turn] = Field(default_factory=list)
    check_results: list[CheckResult] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    error: str = ""
    error_kind: ErrorKind | None = None
    error_code: str | None = None
    role: Literal["attack", "repetition", "control"] = "attack"
    """``repetition``: a fresh run of a hit, to reproduce it. ``control``: the same probe without the
    attack, which must not show the signal."""
    task_completed: bool | None = None
    """Whether the probe's benign task completed in this attempt: every task check hit (``True``), one
    did not (``False``), or it could not be told (``None``, also when the probe has no task)."""
    task_results: list[CheckResult] = Field(default_factory=list)
    """The task checks' results."""
    target_calls: int = Field(default=0, ge=0)
    spend: Spend | None = None
    """What the attempt spent. ``None`` when the source did not record it."""
    stopped_by: BudgetLimit | None = None
    """The budget limit that stopped the attempt, when its outcome is ``stopped``."""
    attacker_refusals: int = Field(default=0, ge=0)
    """Generations of this attempt that the attacker refused to write or left empty."""
    attacker: str | None = None
    """In a run with a pool of attackers, the model that wrote this attempt's attacks (a repetition
    names the one that wrote the hit it repeats). ``None`` with one attacker, the run's."""
    fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    """A digest of what produced the attempt. A resumed run reuses a finished attempt whose
    fingerprint is unchanged."""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    trace_id: str | None = None
    graph_revision: str | None = None
    policy_revision: str | None = None
    operations: dict[str, str] = Field(default_factory=dict)
    """Ids of hosted operations behind the attempt, by step: ``simulate`` and ``evaluate``."""

    @model_validator(mode="after")
    def _consistent(self) -> Attempt:
        if self.outcome == "hit" and (self.level is None or self.level == EvidenceLevel.DELIVERED):
            raise ValueError("a hit names the evidence level it reached, 1 or above")
        if self.outcome != "hit" and self.level not in (None, EvidenceLevel.DELIVERED):
            raise ValueError("only a hit has an evidence level above 0, delivered")
        if self.outcome == "error" and not (self.error or self.error_code or self.error_kind):
            raise ValueError("an attempt that errored says why")
        if (self.outcome == "stopped") != (self.stopped_by is not None):
            raise ValueError("a stopped attempt, and only a stopped one, names the budget limit that stopped it")
        return self


class TaskCompletion(_Object):
    """A probe's benign task, completed with and without the attack."""

    with_attack: int = Field(default=0, ge=0)
    """Attack attempts in which the task completed."""
    with_attack_decided: int = Field(default=0, ge=0)
    """Attack attempts in which completion could be told."""
    without_attack: bool | None = None
    """Whether it completed in the negative control, which sent the task alone."""


class Verification(_Object):
    """What a probe's repetitions and its negative control established."""

    probe_id: Identifier
    attempt_id: str | None = None
    outcome: VerificationOutcome
    reproduction_status: ReproductionStatus = "untested"
    repetitions: int = Field(default=0, ge=0)
    successful_reproductions: int = Field(default=0, ge=0)
    negative_control_passed: bool | None = None
    evidence_tier: EvidenceTier | None = None
    evidence_level: EvidenceLevel | None = None
    ceiling_applied: bool = False
    """The behaviour reproduced on evidence weaker than the objective needs."""
    summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    control_attempt_id: str | None = None
    task_completion: TaskCompletion | None = None


class Finding(_Object):
    """What a run reports for a probe that reached evidence. Its label follows from its level."""

    id: Identifier
    probe_id: Identifier
    vulnerability: Identifier
    vulnerability_type: Identifier
    objective_class: ObjectiveClass
    evidence_level: EvidenceLevel
    label: str
    counted_as: Literal["confirmed", "attempted", "suspected"]
    """``confirmed``: a check hit at level 2 or above, other than a tool call that was only asked for.
    ``attempted``: the strongest evidence is a tool call that matched the goal but is not known to
    have run. ``suspected``: only a model judged it. The three are counted apart, never summed."""
    outcome: VerificationOutcome | None = None
    attempt_ids: list[str] = Field(default_factory=list)
    status: FindingStatus = "open"
    severity: Literal["critical", "high", "medium", "low", "info"] | None = None
    summary: str = ""
    annotations: list[Annotation] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _counted(cls, data: Any) -> Any:
        """A finding recorded before ``counted_as`` existed counts by its level and label."""
        if isinstance(data, dict) and data.get("counted_as") is None and data.get("evidence_level") is not None:
            level, words = int(data["evidence_level"]), data.get("label")
            counted = "suspected" if level <= 1 else "attempted" if words == "Attempted action" else "confirmed"
            data = {**data, "counted_as": counted}
        return data


class Annotation(_Object):
    """A person's note or override on a finding."""

    author: str = ""
    comment: str = ""
    status: FindingStatus | None = None
    created_at: datetime | None = None


class Counts(_Object):
    """A run's attempts by outcome, and its findings by strength. ``confirmed`` counts findings at
    level 2 and above, ``attempted`` those whose strongest evidence is a tool call that did not run,
    ``suspected`` those a model judged only. They are never added together."""

    attempts: int = Field(default=0, ge=0)
    hits: int = Field(default=0, ge=0)
    resisted: int = Field(default=0, ge=0)
    undetermined: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    infrastructure_errors: int = Field(default=0, ge=0)
    target_errors: int = Field(default=0, ge=0)
    attacker_errors: int = Field(default=0, ge=0)
    judge_errors: int = Field(default=0, ge=0)
    stopped: int = Field(default=0, ge=0)
    """Attempts the budget stopped before they were decided."""
    generations: int = Field(default=0, ge=0)
    """Attacks the attacker was asked to write."""
    refusals: int = Field(default=0, ge=0)
    """Of those, the ones it refused to write or left empty. A refused generation is never an attack
    the target resisted."""
    confirmed: int = Field(default=0, ge=0)
    attempted: int = Field(default=0, ge=0)
    """Findings whose strongest evidence is a tool call that matched the goal but is not known to have
    run: an attempted action, never confirmed."""
    suspected: int = Field(default=0, ge=0)

    @property
    def decided(self) -> int:
        """Attempts a check decided: hits and resisted. Rates are computed over these."""
        return self.hits + self.resisted

    @property
    def refusal_rate(self) -> float | None:
        """Refused or empty generations over all generations; ``None`` when none was asked for."""
        return self.refusals / self.generations if self.generations else None

    @property
    def hit_rate(self) -> float | None:
        """Hits over decided attempts; ``None`` when no attempt was decided. Read it beside
        ``undetermined`` and ``errors``, which it leaves out."""
        return self.hits / self.decided if self.decided else None


class TargetRef(_Object):
    """What was attacked, as the run describes it. No credential is ever kept here."""

    kind: Literal["function", "http", "mcp", "agent", "browser", "callback"]
    name: str = ""
    purpose: str = ""


class RedteamRun(_Object):
    """One red team run: an SDK probe run, a local run or a hosted campaign."""

    id: Identifier
    project_id: Identifier = "default"
    source: RunSource = "local"
    state: RunState = "completed"
    catalog_version: str
    purpose: str | None = None
    target: TargetRef | None = None
    graph_revision: str | None = None
    policy_revision: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    finished_at: datetime | None = None
    duration_seconds: float = Field(default=0.0, ge=0)
    expected_attempts: int | None = Field(default=None, ge=1)
    probes: list[Probe] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    verifications: list[Verification] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    counts: Counts = Counts()
    budget: dict[str, Any] = Field(default_factory=dict)
    """What the attacker was allowed, and what it used: target queries, model calls, tokens."""
    limitations: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    """Fields of a source format that have no home in this model, kept so that nothing is lost when
    a report is read in and written back. ``docs/redteam-model.md`` names each."""

    @model_validator(mode="after")
    def _references(self) -> RedteamRun:
        probes = {probe.id for probe in self.probes}
        if len(probes) != len(self.probes):
            raise ValueError("probe ids are unique within a run")
        attempts = {attempt.id for attempt in self.attempts}
        if len(attempts) != len(self.attempts):
            raise ValueError("attempt ids are unique within a run")
        for attempt in self.attempts:
            if attempt.probe_id not in probes:
                raise ValueError(f"attempt {attempt.id} names a probe the run does not have")
        for verification in self.verifications:
            if verification.probe_id not in probes:
                raise ValueError("a verification names a probe the run does not have")
        for finding in self.findings:
            if finding.probe_id not in probes:
                raise ValueError("a finding names a probe the run does not have")
        return self

    def without_local_launch(self) -> RedteamRun:
        """Drop the private configuration that lets the originating process resume the run."""
        if "launch" not in self.provenance:
            return self
        return self.model_copy(
            update={"provenance": {key: value for key, value in self.provenance.items() if key != "launch"}}
        )

    def probe(self, probe_id: str) -> Probe:
        return next(probe for probe in self.probes if probe.id == probe_id)

    def attempts_of(self, probe_id: str) -> list[Attempt]:
        return [attempt for attempt in self.attempts if attempt.probe_id == probe_id]


Finding.model_rebuild()

__all__ = [
    "MODEL_VERSION",
    "Annotation",
    "Attempt",
    "AttemptOutcome",
    "BudgetLimit",
    "Check",
    "CheckKind",
    "CheckResult",
    "Counts",
    "Evidence",
    "EvidenceLevel",
    "EvidenceTier",
    "Finding",
    "FindingStatus",
    "ObjectiveClass",
    "Probe",
    "RedteamRun",
    "ReproductionStatus",
    "RunSource",
    "RunState",
    "Spend",
    "TargetRef",
    "TaskCompletion",
    "ToolCall",
    "Turn",
    "Verification",
    "VerificationOutcome",
]
