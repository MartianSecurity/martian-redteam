"""Attempt-level checkpoints and resume of a red team run (martian-notes#238).

A run is kept in a record store after every attempt. A run killed half-way resumes from the store,
reuses each finished attempt whose fingerprint is unchanged and makes no model call for it."""

import asyncio

import pytest
import yaml
from typer.testing import CliRunner

from martian.cli import app
from martian.evals.records.sql import SqlRecordStore
from martian.redteam.engine import Engine
from martian.redteam.engine import fingerprint as fingerprints
from martian.redteam.engine.runs import execute
from martian.redteam.store import RedteamStore
from tests.redteam.engine_helpers import CountingTarget, StubService, run_file

FILE = {
    "vulnerabilities": [
        {"name": "PromptLeakage", "types": ["system_prompt", "instructions"]},
        {"name": "Toxicity", "types": ["insults"]},
    ],
    "attacks": [{"name": "PromptInjection"}, {"name": "Roleplay"}],
    "budget": {"max_concurrent": 1},
}
PLANNED = 6


@pytest.fixture
def store(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield RedteamStore(records)
    records.close()


async def killed_half_way(store, file, *, after=3):
    """Run ``file`` until ``after`` attempts are kept, then kill it. Returns the kept run."""
    target = CountingTarget("No.")
    target.delay = 0.05
    kept = asyncio.Event()

    async def checkpoint(run):
        await store.save(run)
        if sum(1 for _ in run.attempts) >= after:
            kept.set()

    engine = Engine(file, service=StubService(), target=target, checkpoint=checkpoint, run_id="run-1")
    task = asyncio.create_task(engine.run())
    await kept.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    earlier = await store.get("default", "run-1")
    assert earlier is not None and earlier.state == "cancelled"
    return earlier


async def test_a_killed_run_resumes_and_makes_no_model_call_for_finished_attempts(store):
    file = run_file(**FILE)
    earlier = await killed_half_way(store, file)
    finished = [attempt for attempt in earlier.attempts if attempt.outcome == "resisted"]
    assert 3 <= len(finished) < PLANNED
    service, target = StubService(), CountingTarget("No.")
    resumed = await execute(file, service=service, target=target, store=store, resume=earlier)
    assert resumed.id == "run-1" and resumed.state == "completed" and len(resumed.attempts) == PLANNED
    # Only the attempts that had not finished called the attacker, the target and the judge.
    rerun = PLANNED - len(finished)
    assert (len(service.simulated), len(target.calls), len(service.evaluated)) == (rerun, rerun, rerun)
    assert {attempt.id for attempt in finished}.isdisjoint(session for _, session in target.calls)
    # A query already recorded for an interrupted attempt still counts when that attempt runs again.
    assert resumed.budget["spent"]["target_queries"] == earlier.budget["spent"]["target_queries"] + rerun
    assert resumed.provenance["resumed_attempts"] == len(finished)
    kept = await store.get("default", "run-1")
    assert kept is not None and kept.state == "completed" and kept.counts.attempts == PLANNED


async def test_an_attempt_that_errored_or_was_stopped_runs_again(store):
    file = run_file(**FILE)
    earlier = await Engine(
        run_file(**{**FILE, "budget": {"target_queries": 2}}), service=StubService(), target=CountingTarget()
    ).run()
    assert earlier.counts.stopped == PLANNED - 2
    service = StubService()
    resumed = await Engine(file, service=service, target=CountingTarget(), resume=earlier).run()
    # The budget was raised: the stopped attempts run, the two finished ones do not.
    assert len(service.simulated) == PLANNED - 2
    assert resumed.counts.resisted == PLANNED and resumed.counts.stopped == 0


async def test_changing_a_check_invalidates_only_its_vulnerabilitys_attempts(store):
    first = await Engine(run_file(**FILE), service=StubService(), target=CountingTarget()).run()
    changed = {
        **FILE,
        "vulnerabilities": [
            FILE["vulnerabilities"][0],
            {
                "name": "Toxicity",
                "types": ["insults"],
                "checks": [{"type": "text", "all_of": ["idiot"]}, {"type": "judge"}],
            },
        ],
    }
    service = StubService()
    await Engine(run_file(**changed), service=service, target=CountingTarget(), resume=first).run()
    assert {context.vulnerability for context, _ in service.simulated} == {"toxicity"}
    assert len(service.simulated) == 2


async def test_changing_the_catalogue_version_or_a_model_invalidates_every_attempt(store, monkeypatch):
    first = await Engine(run_file(**FILE), service=StubService(), target=CountingTarget()).run()
    monkeypatch.setattr(fingerprints, "CATALOG_VERSION", "0.3.0")
    service = StubService()
    await Engine(run_file(**FILE), service=service, target=CountingTarget(), resume=first).run()
    assert len(service.simulated) == PLANNED
    monkeypatch.undo()
    service = StubService()
    await Engine(
        run_file(**FILE),
        service=service,
        target=CountingTarget(),
        resume=first,
        provenance={"attacker": {"model": "x"}},
    ).run()
    assert len(service.simulated) == PLANNED


def test_martian_redteam_run_resume_reuses_the_stored_attempts(tmp_path, models):
    (tmp_path / "agent.py").write_text("def target(prompt):\n    return 'No.'\n")
    path = tmp_path / "redteam.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "target": {"purpose": "Help desk", "file": "agent.py"},
                "models": {"attacker": "openai:/attacker-model", "judge": "openai:/judge-model"},
                "vulnerabilities": [{"name": "PromptLeakage", "types": ["system_prompt", "instructions"]}],
                "attacks": [{"name": "PromptInjection"}],
                "output_folder": str(tmp_path / "results"),
            }
        )
    )
    runner = CliRunner()
    first = runner.invoke(app, ["redteam", "run", str(path)])
    assert first.exit_code == 0, first.output
    run_id = first.output.split("Kept in the local store as ")[1].split()[0]
    calls = len(models.requests)
    again = runner.invoke(app, ["redteam", "run", str(path), "--resume", run_id])
    assert again.exit_code == 0, again.output
    assert "Reused 2 finished attempt(s)" in again.output
    assert len(models.requests) == calls
    missing = runner.invoke(app, ["redteam", "run", str(path), "--resume", "redteam-unknown"])
    assert missing.exit_code == 2 and "No red team run redteam-unknown" in missing.output
