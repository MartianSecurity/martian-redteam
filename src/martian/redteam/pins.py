"""Pinned red team probes: a confirmed finding kept as a regression test.

:func:`pin_findings` turns confirmed findings of a run into :class:`Pin` objects, one per finding. A
pin holds the exact attack that hit, the checks that hit it (its evidence predicate) and the reply
they hit on (the recorded vulnerable transcript). Only a deterministic check can be pinned: a text
check, a canary with a value, or a tool-call check. A judge, a receipt or a state check needs a
model, a listener or an environment, so a finding whose only evidence is one of them cannot be
pinned. A pin is made only when its checks still hit its recorded reply.

:func:`replay` re-runs pins against a target with no attacker and no judge: it sends each pin's
attack once, in a session of its own, and applies its checks to the reply. Each pin then
``still_reproduces`` (a check hit), is ``not_reproduced`` (every check decided, none hit), is
``undetermined`` (a check could not decide), an ``error`` (the target failed) or ``invalid`` (its
checks no longer hit its own recorded reply, so the pin proves nothing). Only ``not_reproduced``
passes.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from martian.redteam.model import Attempt, Check, CheckResult, Finding, RedteamRun, ToolCall
from martian.redteam.targets import Target
from martian.redteam.verify.rules import (
    LEVEL_OF_KIND,
    attempt_outcome,
    canary_check,
    confirmed,
    text_check,
    tool_call_check,
)

PIN_VERSION: Literal["martian-redteam-pin-v1"] = "martian-redteam-pin-v1"
PINS_REPORT_VERSION: Literal["martian-redteam-pins-v1"] = "martian-redteam-pins-v1"
PINNABLE = ("text", "canary", "tool_call")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{16}$")

PinStatus = Literal["still_reproduces", "not_reproduced", "undetermined", "error", "invalid"]


class PinError(ValueError):
    """A finding that cannot be pinned, and why."""


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecordedReply(_Out):
    """The reply the pinned checks hit on, as the run recorded it."""

    attempt_id: str
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_calls_observed: bool = False


class Pin(_Out):
    """A confirmed finding kept as a regression test: the attack, its checks and the reply they hit."""

    version: Literal["martian-redteam-pin-v1"] = PIN_VERSION
    id: str
    finding_id: str
    probe_id: str
    vulnerability: str
    vulnerability_type: str
    attack_method: str
    objective_class: str
    severity: str | None = None
    evidence_level: int = Field(ge=2, le=5)
    label: str
    source_run: str
    created_at: datetime
    attack: str
    checks: list[Check] = Field(min_length=1)
    recorded: RecordedReply


def _digest(value: str | None) -> bool:
    return value is None or bool(_DIGEST.match(value))


def file_name(pin: Pin) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", pin.id)[:150] + ".json"


def decide(check: Check, attack: str, text: str, tool_calls: list[ToolCall], observed: bool) -> CheckResult:
    """One pinned check's result on a reply, by the verdict rules."""
    spec, executed = check.spec, None
    hit: bool | None
    if check.kind == "text":
        hit, detail = text_check(
            text,
            all_of=spec.get("all_of", []),
            none_of=spec.get("none_of", []),
            probe=attack,
            ignore_case=spec.get("ignore_case", True),
        )
    elif check.kind == "canary":
        hit, detail = canary_check(text, canary=spec["value"], probe=attack)
    else:
        hit, detail = tool_call_check(
            tool_calls,
            tool=spec["tool"],
            where=spec.get("where", {}),
            executed=spec.get("executed", False),
            observed=observed,
        )
        executed = detail["executed"]
    return CheckResult(
        check_id=check.id,
        kind=check.kind,
        hit=hit,
        level=LEVEL_OF_KIND[check.kind] if hit else None,
        executed=executed,
        rationale=str(detail.get("reason", "")),
    )


def check_pin(pin: Pin, text: str, tool_calls: list[ToolCall], observed: bool) -> tuple[str, list[CheckResult]]:
    """``hit``, ``resisted`` or ``undetermined`` for a reply, and each check's result."""
    results = [decide(check, pin.attack, text, tool_calls, observed) for check in pin.checks]
    outcome, _ = attempt_outcome(results)
    return outcome, results


def self_test(pin: Pin) -> str:
    """The pin's checks on its own recorded reply: ``hit`` when the pin is sound."""
    outcome, _ = check_pin(pin, pin.recorded.text, pin.recorded.tool_calls, pin.recorded.tool_calls_observed)
    return outcome


def _evidence_attempt(run: RedteamRun, finding: Finding) -> tuple[Attempt, list[Check]]:
    """The finding's first attack attempt whose pinnable checks hit, and those checks."""
    probe = run.probe(finding.probe_id)
    checks = {check.id: check for check in probe.checks}
    for attempt in run.attempts:
        if attempt.id not in finding.attempt_ids or attempt.outcome != "hit":
            continue
        hits = [
            checks[result.check_id]
            for result in attempt.check_results
            if result.hit and result.kind in PINNABLE and result.check_id in checks
        ]
        if hits:
            return attempt, hits
    raise PinError(
        f"{finding.id}: no attempt hit on a text, canary or tool-call check. A judge, receipt or state check "
        "needs a model, a listener or an environment, so a pin cannot re-run it."
    )


def pin_finding(run: RedteamRun, finding: Finding, *, now: datetime | None = None) -> Pin:
    """``finding`` of ``run`` as a pin. Raises :class:`PinError` when it cannot be pinned."""
    if not confirmed(finding.evidence_level):
        raise PinError(f"{finding.id}: a suspected finding rests on a judge, so it cannot be pinned")
    attempt, checks = _evidence_attempt(run, finding)
    if _digest(attempt.input) or _digest(attempt.output):
        raise PinError(
            f"{finding.id}: the run holds digests of the attack and the reply, not their text. Run again with "
            "MARTIAN_REPORT_INCLUDE_CONTENT=1 to pin a finding from it."
        )
    target_turns = [turn for turn in attempt.turns if turn.role == "target"]
    calls = target_turns[-1].tool_calls if target_turns else []
    evidence = {item.id: item for item in run.evidence}
    observed = any(
        evidence[item].metadata.get("tool_calls_observed") for item in attempt.evidence_ids if item in evidence
    )
    probe = run.probe(finding.probe_id)
    pin = Pin(
        id=f"pin-{finding.probe_id}",
        finding_id=finding.id,
        probe_id=finding.probe_id,
        vulnerability=finding.vulnerability,
        vulnerability_type=finding.vulnerability_type,
        attack_method=probe.attack_method,
        objective_class=finding.objective_class,
        severity=finding.severity,
        evidence_level=int(finding.evidence_level),
        label=finding.label,
        source_run=run.id,
        created_at=now or datetime.now(UTC),
        attack=str(attempt.input),
        checks=checks,
        recorded=RecordedReply(
            attempt_id=attempt.id,
            text=str(attempt.output),
            tool_calls=calls,
            tool_calls_observed=observed,
        ),
    )
    if self_test(pin) != "hit":
        raise PinError(f"{finding.id}: its checks do not hit its recorded reply, so a pin would prove nothing")
    return pin


def pin_findings(run: RedteamRun, finding_ids: list[str] | None = None) -> list[Pin]:
    """Pins of the named findings of ``run``, or of every confirmed finding when none is named."""
    findings = {finding.id: finding for finding in run.findings}
    if finding_ids:
        unknown = [item for item in finding_ids if item not in findings]
        if unknown:
            raise PinError(f"the run has no finding {', '.join(unknown)}")
        chosen = [findings[item] for item in finding_ids]
    else:
        chosen = [finding for finding in run.findings if confirmed(finding.evidence_level)]
        if not chosen:
            raise PinError("the run has no confirmed finding to pin")
    return [pin_finding(run, finding) for finding in chosen]


def save(pins: list[Pin], folder: str | Path) -> list[Path]:
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for pin in pins:
        path = directory / file_name(pin)
        path.write_text(pin.model_dump_json(indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def load(folder: str | Path) -> list[Pin]:
    """Every pin in ``folder`` (its ``*.json`` files), by name. Raises ``ValueError`` on a file that
    is not a pin."""
    found = []
    for path in sorted(Path(folder).glob("*.json")):
        try:
            found.append(Pin.model_validate_json(path.read_text(encoding="utf-8")))
        except ValueError as error:
            raise ValueError(f"{path.name} is not a red team pin: {error}") from None
    return found


# --- re-running pins -----------------------------------------------------------------------------


class PinResult(_Out):
    pin_id: str
    finding_id: str
    vulnerability: str
    vulnerability_type: str
    attack_method: str
    label: str
    evidence_level: int
    severity: str | None = None
    status: PinStatus
    check_results: list[CheckResult] = Field(default_factory=list)
    error: str = ""


class PinCounts(_Out):
    still_reproduces: int = 0
    not_reproduced: int = 0
    undetermined: int = 0
    error: int = 0
    invalid: int = 0


class PinsReport(_Out):
    """``redteam-pins.json``: each pin's status. ``passed`` only when every pin was not reproduced."""

    version: Literal["martian-redteam-pins-v1"] = PINS_REPORT_VERSION
    started_at: datetime
    finished_at: datetime
    target: dict[str, str] = Field(default_factory=dict)
    passed: bool
    counts: PinCounts
    results: list[PinResult]


STATUS_WORDS = {
    "still_reproduces": "still reproduces",
    "not_reproduced": "not reproduced",
    "undetermined": "undetermined",
    "error": "error",
    "invalid": "invalid pin",
}


async def _one(pin: Pin, target: Target, timeout: float) -> PinResult:
    fields: dict[str, Any] = {
        "pin_id": pin.id,
        "finding_id": pin.finding_id,
        "vulnerability": pin.vulnerability,
        "vulnerability_type": pin.vulnerability_type,
        "attack_method": pin.attack_method,
        "label": pin.label,
        "evidence_level": pin.evidence_level,
        "severity": pin.severity,
    }
    if self_test(pin) != "hit":
        return PinResult(**fields, status="invalid", error="its checks do not hit its own recorded reply")
    try:
        async with asyncio.timeout(timeout):
            reply = await target.send(pin.attack, session=f"{pin.id}#replay")
    except TimeoutError:
        return PinResult(**fields, status="error", error=f"the target did not answer within {timeout:g} seconds")
    except Exception as error:  # noqa: BLE001 - every failure of the target is the target's
        return PinResult(**fields, status="error", error=f"the target failed: {type(error).__name__}")
    outcome, results = check_pin(pin, reply.text, reply.tool_calls, reply.tool_calls_observed)
    status: PinStatus = {"hit": "still_reproduces", "resisted": "not_reproduced"}.get(outcome, "undetermined")  # type: ignore[assignment]
    return PinResult(**fields, status=status, check_results=results)


async def replay(pins: list[Pin], target: Target, *, timeout: float = 120.0) -> PinsReport:
    """Re-run ``pins`` against ``target``, one after another. No model is called."""
    started = datetime.now(UTC)
    results = [await _one(pin, target, timeout) for pin in pins]
    counts = PinCounts(**{status: sum(item.status == status for item in results) for status in STATUS_WORDS})
    return PinsReport(
        started_at=started,
        finished_at=datetime.now(UTC),
        target={"kind": str(target.kind), "name": str(getattr(target, "name", ""))},
        passed=bool(results) and all(item.status == "not_reproduced" for item in results),
        counts=counts,
        results=results,
    )


def render_markdown(report: PinsReport) -> str:
    lines = ["# Pinned red team findings", ""]
    counts = report.counts
    lines.append(
        f"{len(report.results)} pins: {counts.still_reproduces} still reproduce, {counts.not_reproduced} not "
        f"reproduced, {counts.undetermined} undetermined, {counts.error} errors, {counts.invalid} invalid. "
        "No attacker or judge model was called."
    )
    lines += [
        "",
        "| Pin | Vulnerability | Type | Attack | Label | Level | Status |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.results:
        lines.append(
            f"| {item.pin_id} | {item.vulnerability} | {item.vulnerability_type} | {item.attack_method} | "
            f"{item.label} | {item.evidence_level} | {STATUS_WORDS[item.status]} |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "PINNABLE",
    "PIN_VERSION",
    "STATUS_WORDS",
    "Pin",
    "PinError",
    "PinResult",
    "PinsReport",
    "check_pin",
    "load",
    "pin_finding",
    "pin_findings",
    "render_markdown",
    "replay",
    "save",
    "self_test",
]
