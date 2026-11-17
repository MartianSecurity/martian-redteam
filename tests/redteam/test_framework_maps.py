"""Framework maps with honest coverage labels, and selecting entries by framework (martian-notes#270)."""

import re

import pytest
from typer.testing import CliRunner

from martian.redteam.catalog import ENTRIES, FRAMEWORKS
from martian.redteam.engine import plan
from martian.redteam.engine.plan import framework_selection, skipped
from martian_redteam.catalog.frameworks import DISCLAIMER, find_item, vulnerabilities_for
from martian_redteam.cli import app
from tests.redteam.engine_helpers import run_file

LOCAL = {"attacker": "openai:/attacker", "judge": "openai:/judge"}
CLAIMS = re.compile(r"\b(complian(t|ce)|certif(ied|ication)|full coverage|fully covers|conform(s|ity))\b", re.I)


def test_the_five_frameworks_are_mapped_item_by_item():
    assert set(FRAMEWORKS) == {
        "owasp_llm_2025",
        "owasp_agentic_2026",
        "mitre_atlas_2026",
        "nist_ai_rmf_1_0",
        "eu_ai_act_2024",
    }
    assert [item.id for item in FRAMEWORKS["owasp_llm_2025"].items] == [f"LLM{n:02d}" for n in range(1, 11)]
    assert [item.id for item in FRAMEWORKS["owasp_agentic_2026"].items] == [f"ASI{n:02d}" for n in range(1, 11)]


def test_every_entry_is_mapped_and_every_tag_names_an_item():
    for entry in ENTRIES.values():
        assert entry.frameworks, f"{entry.name} is unmapped"
        for tag in entry.frameworks:
            framework, item = find_item(tag)
            assert entry.id in framework.vulnerabilities_of(item.id)


def test_every_label_matches_what_the_catalogue_tests():
    for framework in FRAMEWORKS.values():
        for item in framework.items:
            mapped = framework.vulnerabilities_of(item.id)
            assert bool(mapped) == (item.coverage != "not_covered"), f"{framework.id}:{item.id}"
            if item.coverage != "covered":
                continue
            # A covered item is tested by a form code decides, never only by the judge.
            assert any(any(form.deterministic for form in ENTRIES_BY_ID[i].forms) for i in mapped), item.id


ENTRIES_BY_ID = {entry.id: entry for entry in ENTRIES.values()}


def test_no_map_claims_full_coverage_or_compliance():
    assert "not a certification" in DISCLAIMER and "no framework is fully covered" in DISCLAIMER
    for framework in FRAMEWORKS.values():
        assert framework.coverage == "subset"
        assert framework.counts()["not_covered"] + framework.counts()["partly"] > 0
        texts = [framework.label, framework.notes, *(f"{item.title} {item.note}" for item in framework.items)]
        assert not [text for text in texts if CLAIMS.search(text) and "not" not in text.lower()], framework.id


def test_a_run_file_selects_entries_by_framework_item():
    file = run_file(vulnerabilities=[], frameworks=["owasp_agentic_2026:ASI02"], models=LOCAL)
    probes = plan(file)
    chosen = {probe.probe.vulnerability for probe in probes}
    assert chosen == set(FRAMEWORKS["owasp_agentic_2026"].vulnerabilities_of("ASI02"))
    # An entry whose default form needs a goal tool call runs its first form that needs none.
    misuse = next(probe for probe in probes if probe.probe.vulnerability == "tool_misuse")
    assert [check.kind for check in misuse.probe.checks] == ["judge"]
    exfiltration = next(probe for probe in probes if probe.probe.vulnerability == "data_exfiltration")
    assert [check.kind for check in exfiltration.probe.checks] == ["canary", "receipt"]


def test_an_entry_the_run_file_lists_keeps_its_own_settings():
    file = run_file(
        vulnerabilities=[{"name": "ToolMisuse", "types": ["unauthorised_tool"], "goal": {"tool": "send_email"}}],
        frameworks=["owasp_agentic_2026:ASI02"],
        models=LOCAL,
    )
    misuse = [probe for probe in plan(file) if probe.probe.vulnerability == "tool_misuse"]
    assert len(misuse) == 1 and misuse[0].probe.checks[-1].spec["with_canary"] is True


def test_an_entry_a_framework_selects_but_cannot_run_is_skipped_with_a_reason():
    file = run_file(vulnerabilities=[], frameworks=["owasp_agentic_2026:ASI04"], models=LOCAL)
    chosen, left_out = framework_selection(file)
    assert chosen == [] and left_out[0]["vulnerability"] == "tool_poisoning"
    assert "it needs tools.decoy" in left_out[0]["reason"] and left_out in [skipped(file)]
    with_decoy = run_file(
        vulnerabilities=[], frameworks=["owasp_agentic_2026:ASI04"], tools={"decoy": {}}, models=LOCAL
    )
    [probe] = plan(with_decoy)
    assert probe.probe.vulnerability_type == "mcp_tool_description"


@pytest.mark.parametrize(
    ("selection", "error"),
    [("owasp_2099", "unknown framework 'owasp_2099'"), ("owasp_llm_2025:LLM11", "owasp_llm_2025 has no item 'LLM11'")],
)
def test_an_unknown_framework_or_item_is_refused(selection, error):
    with pytest.raises(ValueError, match=error):
        run_file(vulnerabilities=[], frameworks=[selection])
    with pytest.raises(ValueError, match="list vulnerabilities, or select them with frameworks"):
        run_file(vulnerabilities=[])


def test_vulnerabilities_for_takes_a_framework_or_an_item():
    assert {item.id for item in vulnerabilities_for("owasp_llm_2025:LLM07")} == {"prompt_leakage"}
    assert len(vulnerabilities_for("nist_ai_rmf_1_0")) == len(ENTRIES)


def test_the_catalog_command_prints_each_item_and_its_coverage():
    printed = CliRunner().invoke(app, ["catalog"])
    assert printed.exit_code == 0, printed.output
    assert (
        "OWASP Top 10 for Agentic Applications 2026 (owasp_agentic_2026, 2026): 1 covered, 5 partly, 4 not covered"
        in printed.output
    )
    assert "not covered" in printed.output and DISCLAIMER in " ".join(printed.output.split())
