"""Catalogue 1.0: every entry states its objective, forms and checks, severity, framework tags, source
and licence, and every 0.2 name still resolves as it did (martian-notes#261)."""

import re
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

from martian.redteam.catalog import CATALOG_VERSION, ENTRIES, entry
from martian.redteam.engine import plan
from martian.redteam.runfile import CheckDecl, load
from martian_redteam.catalog import VULNERABILITIES, get_vulnerability
from martian_redteam.cli import app
from tests.redteam.engine_helpers import run_file

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

CATALOGUE_0_2 = {
    "PromptLeakage": ("prompt_leakage", True, ("system_prompt", "instructions", "credentials")),
    "ExcessiveAgency": ("excessive_agency", True, ("functionality", "permissions", "autonomy")),
    "Toxicity": ("toxicity", False, ("insults", "profanity", "hate")),
    "IllegalActivity": ("illegal_activity", False, ("violent_crimes", "cybercrime", "fraud")),
    "PIILeakage": ("pii_leakage", False, ("direct_disclosure", "session_leak", "database_access")),
    "Bias": ("bias", False, ("religion", "politics", "gender", "race")),
    "Misinformation": (
        "misinformation",
        False,
        ("factual_errors", "unsupported_claims", "expertize_misrepresentation"),
    ),
    "RBAC": ("rbac", True, ("role_bypass", "privilege_escalation", "unauthorized_role_assumption")),
    "SSRF": ("ssrf", True, ("internal_service_access", "cloud_metadata_access", "port_scanning")),
    "SQLInjection": ("sql_injection", True, ("blind_sql_injection", "union_based_injection", "error_based_injection")),
    "ShellInjection": (
        "shell_injection",
        True,
        ("command_injection", "system_command_execution", "shell_escape_sequences"),
    ),
    "Robustness": ("robustness", False, ("input_overreliance", "hijacking")),
}
"""Catalogue 0.2 as it shipped: each YAML name's id, gate flag and types."""


@pytest.mark.parametrize("name", sorted(CATALOGUE_0_2))
def test_every_0_2_name_and_id_resolves_unchanged(name):
    identifier, gated, types = CATALOGUE_0_2[name]
    for key in (name, identifier, identifier.replace("_", "-"), name.lower()):
        found = get_vulnerability(key)
        assert (found.id, found.gated, found.allowed_types, found.types) == (identifier, gated, types, types)
    assert VULNERABILITIES[name]().id == identifier


def test_version_0_2_yaml_files_still_load():
    assert type(load(EXAMPLES / "config.yaml")).__name__ == "RunConfig"


def test_every_entry_states_the_1_0_fields():
    assert CATALOG_VERSION.startswith("1.")
    adapter: TypeAdapter[CheckDecl] = TypeAdapter(CheckDecl)
    for name, item in ENTRIES.items():
        assert item.name == name and entry(item.id) is item
        assert item.objective in ("disclosure", "control", "action", "persistence")
        assert item.severity in ("critical", "high", "medium", "low", "info")
        assert item.source and item.licence
        assert item.forms, name
        for form in item.forms:
            assert form.proves
            for check in form.checks:
                adapter.validate_python(dict(check))
        for tag in item.frameworks:
            framework, _, item_id = tag.partition(":")
            assert framework and item_id


def test_a_vulnerability_without_checks_is_decided_by_its_default_form_as_in_0_2():
    [planned] = plan(run_file(vulnerabilities=[{"name": "PromptLeakage", "types": ["system_prompt"]}]))
    assert [(check.id, check.kind, check.spec) for check in planned.probe.checks] == [("judge", "judge", {})]
    assert planned.probe.objective_class == "disclosure"


def test_the_entry_gives_the_default_severity_and_the_run_file_overrides_it():
    [planned] = plan(run_file(vulnerabilities=[{"name": "ShellInjection", "types": ["command_injection"]}]))
    assert planned.severity == "critical" and planned.probe.metadata["severity"] == "critical"
    [chosen] = plan(
        run_file(vulnerabilities=[{"name": "ShellInjection", "types": ["command_injection"], "severity": "low"}])
    )
    assert chosen.severity == "low"
    [custom] = plan(run_file(vulnerabilities=[{"name": "custom", "id": "mine", "criteria": "No secrets."}]))
    assert custom.severity is None


def test_the_catalog_command_prints_the_new_fields():
    printed = CliRunner().invoke(app, ["catalog"])
    assert printed.exit_code == 0, printed.output
    assert f"catalogue {CATALOG_VERSION}" in printed.output
    for words in ("Objective", "Severity", "Forms", "Frameworks", "Source and licence"):
        assert words in printed.output
    assert re.search(r"PromptLeakage\s+│\s+reply \(default\)\s+│\s+judge\s+│\s+1", printed.output)
    assert "owasp_llm_2025:LLM07" in printed.output and "Apache-2.0" in printed.output
