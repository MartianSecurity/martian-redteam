"""Compare two red team runs: new, fixed and unchanged findings, a paired test, and a warning when
the runs are not comparable (martian-notes#287)."""

import json

from typer.testing import CliRunner

from martian.cli import app
from martian.redteam.engine import Engine
from martian.redteam.report.compare import compare_runs, render_markdown
from tests.redteam.engine_helpers import CountingTarget, StubService, run_file
from tests.redteam.test_engine import write_run

FILE = {
    "vulnerabilities": [
        {"name": "PromptLeakage", "types": ["system_prompt", "instructions"]},
        {"name": "Toxicity", "types": ["insults"]},
    ],
    "attacks": [{"name": "PromptInjection"}],
    "budget": {"target_queries": 100, "attempts_per_probe": 3},
}


def replies(leaks: set[str]):
    """A target that leaks to the attacks of the given vulnerability types."""

    def reply(message: str) -> str:
        return "LEAKED" if any(kind in message for kind in leaks) else "No."

    return reply


async def run_of(leaks: set[str], **fields):
    file = run_file(**{**FILE, **fields})
    provenance = {"service": "local", "attacker": {"model": "openai:/a", "inference_params": {}}}
    return await Engine(file, service=StubService(), target=CountingTarget(replies(leaks)), provenance=provenance).run()


async def test_a_run_compared_with_itself_shows_no_change():
    run = await run_of({"system_prompt"})
    comparison = compare_runs(run, run)
    assert comparison.comparable and comparison.warnings == []
    assert not comparison.changed
    assert comparison.new == [] and comparison.fixed == [] and comparison.undecided == []
    assert [item.probe_id for item in comparison.unchanged] == ["prompt_leakage:system_prompt:prompt_injection"]
    assert (comparison.paired.compared, comparison.paired.improved, comparison.paired.regressed) == (9, 0, 0)
    assert comparison.paired.label == "within_noise"
    assert "No change." in render_markdown(comparison)


async def test_new_fixed_and_unchanged_findings_on_the_same_probes():
    before = await run_of({"system_prompt", "insults"})
    after = await run_of({"system_prompt", "instructions"})
    comparison = compare_runs(before, after)
    assert comparison.comparable and comparison.changed
    assert [item.vulnerability_type for item in comparison.new] == ["instructions"]
    assert [item.vulnerability_type for item in comparison.fixed] == ["insults"]
    assert [item.vulnerability_type for item in comparison.unchanged] == ["system_prompt"]
    # Judge hits are level 1: suspected, and the paired test counts confirmed hits only.
    assert comparison.new[0].counted_as == "suspected"
    assert comparison.paired.improved == 0 and comparison.paired.regressed == 0


async def test_a_paired_test_labels_a_change_worse_than_noise():
    before = await run_of(set())
    after = await run_of(
        {"system_prompt", "instructions", "insults"},
        vulnerabilities=[
            {
                "name": "PromptLeakage",
                "types": ["system_prompt", "instructions"],
                "checks": [{"type": "text", "all_of": ["LEAKED"]}],
            },
            {"name": "Toxicity", "types": ["insults"], "checks": [{"type": "text", "all_of": ["LEAKED"]}]},
        ],
    )
    comparison = compare_runs(before, after)
    # The checks changed, so the runs are not comparable, and the label still reads the attempts.
    assert not comparison.comparable and any("different checks" in item for item in comparison.warnings)
    assert comparison.paired.regressed == 9 and comparison.paired.label == "worse"


async def test_a_comparison_across_budgets_or_attackers_carries_the_warning():
    before = await run_of({"system_prompt"})
    after = await run_of({"system_prompt"}, budget={"target_queries": 50, "attempts_per_probe": 3})
    comparison = compare_runs(before, after)
    assert not comparison.comparable
    assert comparison.warnings == [
        "different budgets: target_queries 100 in the baseline, target_queries 50 in the candidate"
    ]
    assert "**Not comparable.**" in render_markdown(comparison)
    other = after.model_copy(update={"provenance": {**before.provenance, "attacker": {"model": "openai:/b"}}})
    found = compare_runs(before, other).warnings
    assert any(item.startswith("different attackers: openai:/a in the baseline, openai:/b") for item in found)


async def test_a_finding_the_candidate_did_not_decide_is_not_called_fixed():
    before = await run_of({"system_prompt"})

    class Down(CountingTarget):
        async def send(self, message, *, session=None):
            raise ConnectionError("down")

    after = await Engine(run_file(**FILE), service=StubService(), target=Down(), provenance=before.provenance).run()
    comparison = compare_runs(before, after)
    assert comparison.fixed == [] and [item.vulnerability_type for item in comparison.undecided] == ["system_prompt"]
    assert comparison.changed and comparison.paired.compared == 0


def test_martian_redteam_compare_reads_two_saved_runs(tmp_path, models):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = write_run(tmp_path / "a")
    second = write_run(tmp_path / "b", budget={"target_queries": 10})
    runner = CliRunner()
    runner.invoke(app, ["redteam", "run", str(first), "--no-store"])
    runner.invoke(app, ["redteam", "run", str(second), "--no-store"])
    a, b = tmp_path / "a" / "results" / "redteam-run.json", tmp_path / "b" / "results" / "redteam-run.json"
    same = runner.invoke(app, ["redteam", "compare", str(a), str(a), "-o", str(tmp_path / "out")])
    assert same.exit_code == 0 and "No change." in same.output and "Not comparable" not in same.output
    saved = json.loads((tmp_path / "out" / "redteam-comparison.json").read_text())
    assert saved["changed"] is False and saved["comparable"] is True
    across = runner.invoke(app, ["redteam", "compare", str(a), str(b)])
    assert across.exit_code == 0 and "Not comparable: different budgets" in across.output
