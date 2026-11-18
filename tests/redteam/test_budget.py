"""Model-call accounting fixtures. None of these opens a target or model connection."""

import asyncio
import xml.etree.ElementTree as ET
from contextlib import AsyncExitStack

import pytest

from martian.redteam.engine.budget import BudgetExhausted, Ledger
from martian.redteam.engine.local import LocalService
from martian.redteam.engine.run import _Spending
from martian.redteam.engine.runs import build_service
from martian.redteam.engine.service import ServiceSelectionError
from martian.redteam.gate import evaluate
from martian.redteam.model import BUDGET_USAGE_UNAVAILABLE, Attempt, Probe, RedteamRun, Spend
from martian.redteam.report.html import render_html
from martian.redteam.report.junit import render_junit
from martian.redteam.report.markdown import render_markdown
from martian.redteam.report.sarif import render_sarif
from martian.redteam.runfile import Budget, Gate, ModelConfig, Models, RunFile
from martian.redteam.verify.judge import judge_for
from martian.redteam.verify.verifiers import meter_model_call
from martian_redteam.types import ModelUsage

PROBE = Probe(
    id="fixture",
    vulnerability="fixture",
    vulnerability_type="fixture",
    attack_method="fixture",
    objective_class="disclosure",
)


@pytest.mark.parametrize(("first_cost", "sent"), [(0.006, [0, 1]), (None, [0])])
async def test_spend_cap_settles_each_model_call_before_the_next_is_admitted(first_cost, sent):
    ledger = Ledger(Budget(cost_usd=0.01, model_calls=3))
    calls = []

    async def call(index):
        meter = _Spending(ledger, f"fixture-{index}")
        try:
            async with meter_model_call(meter, "judge") as reported:
                calls.append(index)
                await asyncio.sleep(0)  # All other callers try to reserve while this one is in flight.
                reported(ModelUsage(cost_usd=first_cost if index == 0 else 0.006))
        except BudgetExhausted as exc:
            assert exc.limit == "cost_usd"

    await asyncio.gather(*(call(index) for index in range(3)))

    assert calls == sent
    assert ledger.spent.model_calls == len(sent)
    assert ledger.spent.cost_usd == pytest.approx(0.012 if first_cost is not None else 0)
    assert ledger.stop_reason == (BUDGET_USAGE_UNAVAILABLE if first_cost is None else None)


def test_only_required_usage_can_stop_a_run_and_explicit_zero_is_a_measurement():
    dollars = Ledger(Budget(cost_usd=0.01))
    dollars.reserve("judge", PROBE.id)
    dollars.record(PROBE.id, ModelUsage(cost_usd=0))
    assert dollars.usage == {"calls_without_token_usage": 1, "calls_without_cost": 0}
    assert dollars.stopped_by is None

    tokens = Ledger(Budget(tokens=10))
    tokens.reserve("judge", PROBE.id)
    tokens.record(PROBE.id, ModelUsage(input_tokens=0, output_tokens=0))
    assert tokens.stopped_by is None
    tokens.reserve("judge", PROBE.id)
    tokens.record(PROBE.id, ModelUsage(input_tokens=4))
    assert tokens.spent.tokens == 4
    assert tokens.usage == {"calls_without_token_usage": 1, "calls_without_cost": 2}
    assert tokens.stopped_by == "tokens"
    with pytest.raises(BudgetExhausted, match=BUDGET_USAGE_UNAVAILABLE):
        tokens.reserve("judge", PROBE.id)


async def test_local_model_setup_requires_pricing_for_dollar_caps_and_disables_sdk_retries(monkeypatch):
    model = ModelConfig(model="openai:/fixture")
    run_file = RunFile.model_construct(version=2, attacks=[], models=Models(attacker=model, judge=model))
    monkeypatch.delenv("MLFLOW_MODEL_CATALOG_URI", raising=False)
    async with AsyncExitStack() as stack:
        ordinary, _ = build_service(run_file, stack, {})
        assert isinstance(ordinary, LocalService)
        assert ordinary.max_retries > 0

        capped = run_file.model_copy(update={"budget": Budget(cost_usd=0.01)})
        with pytest.raises(ServiceSelectionError, match="MLFLOW_MODEL_CATALOG_URI"):
            build_service(capped, stack, {})

        monkeypatch.setenv("MLFLOW_MODEL_CATALOG_URI", "file:///unused-fixture-prices")
        service, _ = build_service(capped, stack, {})
        assert isinstance(service, LocalService)
        judge = judge_for(capped, service)
        assert service.max_retries == service.rate_limit_wait_seconds == 0
        assert judge.max_retries == judge.rate_limit_wait_seconds == 0


async def test_interrupted_provider_call_is_settled_as_unknown():
    ledger = Ledger(Budget(cost_usd=0.01))
    started = asyncio.Event()
    meter = _Spending(ledger, PROBE.id)

    async def call():
        async with meter_model_call(meter, "judge"):
            started.set()
            await asyncio.Future()

    running = asyncio.create_task(call())
    await asyncio.wait_for(started.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert ledger.usage == {"calls_without_token_usage": 1, "calls_without_cost": 1}
    assert meter.spend.judge_calls == 1
    with pytest.raises(BudgetExhausted, match=BUDGET_USAGE_UNAVAILABLE):
        async with meter_model_call(_Spending(ledger, PROBE.id), "judge"):
            pytest.fail("the second call was admitted")


def test_resume_uses_cumulative_spend_even_when_failed_attempts_are_retried():
    ledger = Ledger(Budget(cost_usd=0.01))
    ledger.reserve("judge", PROBE.id)
    ledger.record(PROBE.id, ModelUsage(input_tokens=3, output_tokens=2, cost_usd=0.006))
    ledger.reserve("judge", PROBE.id)
    ledger.record(PROBE.id, ModelUsage(input_tokens=4, output_tokens=1, cost_usd=0.006))
    earlier = RedteamRun(
        id="fixture-resume",
        catalog_version="fixture",
        state="failed",
        probes=[PROBE],
        attempts=[
            Attempt(
                id="fixture#1",
                probe_id=PROBE.id,
                number=1,
                outcome="error",
                error="fixture",
                spend=Spend(judge_calls=1, cost_usd=0.006),
            )
        ],
        budget=ledger.summary(),
    )
    resumed = Ledger(Budget(cost_usd=0.01))
    resumed.restore(RedteamRun.model_validate_json(earlier.model_dump_json()))
    assert resumed.spent == ledger.spent
    assert resumed.by_probe == ledger.by_probe
    with pytest.raises(BudgetExhausted, match="cost_usd ran out"):
        resumed.reserve("judge", PROBE.id)


@pytest.mark.parametrize("history", ["legacy", "running", "orphan-cancelled"])
def test_spend_capped_resume_cannot_certify_incomplete_history(history):
    ledger = Ledger(Budget())
    ledger.reserve("judge", PROBE.id)
    ledger.record(PROBE.id, ModelUsage(input_tokens=0, output_tokens=0, cost_usd=0))
    budget = ledger.summary()
    if history == "legacy":
        del budget["usage"]
    elif history == "orphan-cancelled":
        budget["history_incomplete"] = True
    earlier = RedteamRun(
        id="fixture-legacy",
        catalog_version="fixture",
        state="running" if history == "running" else "cancelled",
        budget=budget,
    )
    resumed = Ledger(Budget(cost_usd=0.01))
    resumed.restore(earlier)
    assert resumed.spent.model_calls == 1
    assert resumed.usage["calls_without_cost"] is None
    with pytest.raises(BudgetExhausted, match=BUDGET_USAGE_UNAVAILABLE):
        resumed.reserve("judge", PROBE.id)


def test_unverifiable_budget_fails_the_gate_and_ci_reports_even_with_a_decided_attempt():
    ledger = Ledger(Budget(cost_usd=0.01))
    ledger.reserve("judge", PROBE.id)
    ledger.record(PROBE.id, ModelUsage(input_tokens=0, output_tokens=0))
    run = RedteamRun(
        id="fixture-report",
        catalog_version="fixture",
        state="stopped",
        probes=[PROBE],
        attempts=[Attempt(id="fixture#1", probe_id=PROBE.id, number=1, outcome="resisted")],
        budget=ledger.summary(),
        limitations=ledger.limitations(),
    )
    gate = evaluate(run, Gate(undetermined="ignore"))
    assert gate.exit_code == 1
    assert len(gate.failures) == 1 and BUDGET_USAGE_UNAVAILABLE in gate.failures[0]
    assert "Recorded spend" in render_markdown(run)
    assert BUDGET_USAGE_UNAVAILABLE in render_html(run)
    junit = ET.fromstring(render_junit(run))
    assert junit.find(f".//error[@type='{BUDGET_USAGE_UNAVAILABLE}']") is not None
    assert junit.attrib["errors"] == "1"
    invocation = render_sarif(run)["runs"][0]["invocations"][0]
    assert invocation["executionSuccessful"] is False
    assert BUDGET_USAGE_UNAVAILABLE in invocation["properties"]["budget_error"]
