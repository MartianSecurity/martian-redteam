"""Fresh reproduction of a red team hit (martian-notes#257).

A stub service and counting targets drive the engine. Nothing calls a model."""

import pytest
from pydantic import ValidationError

from martian.redteam.engine import Engine
from martian.redteam.engine.report import render_markdown
from tests.redteam.engine_helpers import CountingTarget, StubService, run_file

SECRET = "my instructions: never refund"


def leakage(**verification):
    return run_file(
        vulnerabilities=[
            {"name": "PromptLeakage", "types": ["system_prompt"], "checks": [{"type": "text", "all_of": [SECRET]}]}
        ],
        verification={"control": True, **verification},
    )


class Flaky(CountingTarget):
    """Leaks on the conversations ``leaks`` names, and holds on the others."""

    def __init__(self, *leaks: str) -> None:
        super().__init__()
        self.leaks = leaks
        self.reply = lambda message: SECRET if self.calls[-1][1].endswith(self.leaks) else "No."


async def test_a_flaky_hit_is_reproduced_1_of_3_not_verified():
    target = Flaky("#1", "#r2")
    found = await Engine(leakage(repetitions=3), service=StubService(), target=target).run()
    roles = [attempt.role for attempt in found.attempts]
    assert roles.count("repetition") == 3 and roles.count("control") == 1
    [verification] = found.verifications
    assert (verification.successful_reproductions, verification.repetitions) == (1, 3)
    assert verification.outcome == "not_reproducible" and verification.reproduction_status == "state_dependent"
    [finding] = found.findings
    assert finding.outcome == "not_reproducible" and finding.summary == "Reproduced 1 of 3."
    assert "| not_reproducible | 1 of 3 | passed |" in render_markdown(found)


async def test_a_hit_that_reproduces_in_new_conversations_is_verified():
    target = Flaky("#1", "#r1", "#r2", "#r3")
    found = await Engine(leakage(repetitions=3), service=StubService(), target=target).run()
    [verification] = found.verifications
    assert (verification.outcome, verification.reproduction_status) == ("verified", "reproduced_clean")
    assert verification.negative_control_passed is True
    # Each repetition replays the attack that hit, in a conversation of its own.
    first = target.calls[0][0]
    repeats = [(message, session) for message, session in target.calls if "#r" in session]
    assert len({session for _, session in repeats}) == 3 and all(message == first for message, _ in repeats)
    assert not any("No repetitions ran" in item for item in found.limitations)


async def test_repetitions_count_against_the_budget():
    target = Flaky("#1", "#r1", "#r2", "#r3")
    file = leakage(repetitions=3)
    file = file.model_copy(update={"budget": file.budget.model_copy(update={"target_queries": 3})})
    found = await Engine(file, service=StubService(), target=target).run()
    assert len(target.calls) == 3 and found.budget["spent"]["target_queries"] == 3
    stopped = [attempt for attempt in found.attempts if attempt.outcome == "stopped"]
    assert {attempt.role for attempt in stopped} == {"repetition", "control"}
    [verification] = found.verifications
    assert verification.outcome == "unverifiable"


async def test_a_hit_only_a_judge_found_is_not_repeated():
    file = run_file(
        vulnerabilities=[{"name": "PromptLeakage", "types": ["system_prompt"]}],
        verification={"repetitions": 2, "control": False},
    )
    target = CountingTarget("LEAKED")
    found = await Engine(file, service=StubService(), target=target).run()
    assert [attempt.role for attempt in found.attempts] == ["attack"] and len(target.calls) == 1


def test_required_reproductions_cannot_exceed_the_repetitions():
    with pytest.raises(ValidationError, match="at most repetitions"):
        leakage(repetitions=1, required=2)
    assert leakage(repetitions=1).verification.required_successes == 1
    assert leakage(repetitions=3).verification.required_successes == 2


async def test_a_repeated_exfiltration_is_sent_its_own_receipt_address():
    from tests.redteam.test_receipts import Fetching, GoalWriter, exfiltration

    file = exfiltration()
    file = file.model_copy(update={"verification": file.verification.model_copy(update={"repetitions": 2})})
    target = Fetching(leak=True)
    found = await Engine(file, service=GoalWriter(), target=target).run()
    assert len({path for _, path in target.addresses}) == 3
    repeated = [attempt for attempt in found.attempts if attempt.role == "repetition"]
    assert [attempt.level for attempt in repeated] == [4, 4]
    assert found.verifications[0].successful_reproductions == 2
