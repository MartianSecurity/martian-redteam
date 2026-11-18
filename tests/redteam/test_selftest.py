"""The red team self-test with a planted vulnerable target (martian-notes#259).

No model is called; the receipt case sends its request to the run's own loopback listener."""

import pytest
from typer.testing import CliRunner

from martian.cli import app
from martian.redteam.verify.judge import ServiceJudge
from martian.redteam.verify.selftest import CASES, ScriptedService, selftest
from martian.redteam.verify.verifiers import Verifiers


async def test_every_evidence_level_detects_its_planted_hit():
    outcomes, run = await selftest()
    assert [outcome.case.name for outcome in outcomes if not outcome.passed] == []
    assert {outcome.case.name for outcome in outcomes} == {case.name for case in CASES}
    assert run.state == "completed" and run.provenance["receipts"]["received"] >= 1


# Each verifier, disabled, must make the self-test fail on the case that needs it.
NEEDS = {"judge": "judged", "text": "text", "canary": "canary", "tool_call": "executed", "receipt": "receipt"}


@pytest.mark.parametrize("kind", sorted(NEEDS))
async def test_the_self_test_fails_when_a_verifier_is_disabled(kind):
    verifiers = Verifiers(ServiceJudge(ScriptedService())).without(kind)
    outcomes, _ = await selftest(verifiers)
    failed = {outcome.case.name for outcome in outcomes if not outcome.passed}
    assert NEEDS[kind] in failed


def test_martian_redteam_selftest_passes_and_fails_loudly():
    runner = CliRunner()
    passed = runner.invoke(app, ["redteam", "selftest"])
    assert passed.exit_code == 0, passed.output
    assert "Every evidence level detected its planted hit." in passed.output
    failed = runner.invoke(app, ["redteam", "selftest", "--disable", "canary"])
    assert failed.exit_code == 1
    assert "Self-test failed: canary" in " ".join(failed.output.split())
    wrong = runner.invoke(app, ["redteam", "selftest", "--disable", "nothing"])
    assert wrong.exit_code == 2
