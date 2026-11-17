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

AttemptOutcome = Literal["hit", "resisted", "undetermined", "error"]
"""Three outcomes decide an attempt, and errors stay outside them. ``hit`` (the target was
vulnerable): a check found the attack succeeded. ``resisted`` (the target held): every check ran and
none did. ``undetermined``: a check could not decide (an unreadable judgment, a missing observation).
``error``: the attempt did not complete, and ``Attempt.error_kind`` says whose failure it was. Rates are
computed over decided attempts (hits and resisted), with the undetermined count beside them."""

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
    metadata: dict[str, Any] = Field(default_factory=dict)


class Attempt(_Object):
    """One execution of a probe against the target."""

    id: Identifier
    probe_id: Identifier
    number: int = Field(ge=1)
    outcome: AttemptOutcome
    level: EvidenceLevel | None = None
    """The highest level a hit reached; ``None`` without a hit."""
    input: str | None = None
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
    target_calls: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    trace_id: str | None = None
    graph_revision: str | None = None
    policy_revision: str | None = None
    operations: dict[str, str] = Field(default_factory=dict)
    """Ids of hosted operations behind the attempt, by step: ``simulate`` and ``evaluate``."""

    @model_validator(mode="after")
    def _consistent(self) -> Attempt:
        if self.outcome == "hit" and self.level is None:
            raise ValueError("a hit names the evidence level it reached")
        if self.outcome != "hit" and self.level is not None:
            raise ValueError("only a hit has an evidence level")
        if self.outcome == "error" and not (self.error or self.error_code or self.error_kind):
            raise ValueError("an attempt that errored says why")
        return self


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


class Finding(_Object):
    """What a run reports for a probe that reached evidence. Its label follows from its level."""

    id: Identifier
    probe_id: Identifier
    vulnerability: Identifier
    vulnerability_type: Identifier
    objective_class: ObjectiveClass
    evidence_level: EvidenceLevel
    label: str
    outcome: VerificationOutcome | None = None
    attempt_ids: list[str] = Field(default_factory=list)
    status: FindingStatus = "open"
    severity: Literal["critical", "high", "medium", "low", "info"] | None = None
    summary: str = ""
    annotations: list[Annotation] = Field(default_factory=list)


class Annotation(_Object):
    """A person's note or override on a finding."""

    author: str = ""
    comment: str = ""
    status: FindingStatus | None = None
    created_at: datetime | None = None


class Counts(_Object):
    """A run's attempts by outcome, and its findings by strength. ``confirmed`` counts findings at
    level 2 and above; ``suspected`` those a model judged only. They are never added together."""

    attempts: int = Field(default=0, ge=0)
    hits: int = Field(default=0, ge=0)
    resisted: int = Field(default=0, ge=0)
    undetermined: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    infrastructure_errors: int = Field(default=0, ge=0)
    target_errors: int = Field(default=0, ge=0)
    attacker_errors: int = Field(default=0, ge=0)
    judge_errors: int = Field(default=0, ge=0)
    confirmed: int = Field(default=0, ge=0)
    suspected: int = Field(default=0, ge=0)

    @property
    def decided(self) -> int:
        """Attempts a check decided: hits and resisted. Rates are computed over these."""
        return self.hits + self.resisted

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
    "TargetRef",
    "ToolCall",
    "Turn",
    "Verification",
    "VerificationOutcome",
]
