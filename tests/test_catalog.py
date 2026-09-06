import pytest

from martian_redteam.catalog import (
    CATALOG_VERSION,
    FRAMEWORKS,
    CustomVulnerability,
    PromptLeakage,
    get_attack,
    get_vulnerability,
    vulnerabilities_for,
)


def test_catalog_version_is_bumped_for_new_ids() -> None:
    assert CATALOG_VERSION == "0.2.0"


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValueError, match="not_a_type"):
        PromptLeakage(types=["not_a_type"])
    with pytest.raises(ValueError, match="not_a_type"):
        get_vulnerability("bias", ["not_a_type"])


def test_yaml_name_lookup() -> None:
    vuln = get_vulnerability("ExcessiveAgency", ["functionality"])
    assert vuln.id == "excessive_agency"
    assert vuln.gated is True
    attack = get_attack("Base64")
    assert attack.kind == "encoding"


def test_snake_case_aliases() -> None:
    assert get_vulnerability("prompt_leakage").id == "prompt_leakage"
    assert get_attack("prompt_injection").id == "prompt_injection"
    assert get_attack("rot13").kind == "encoding"
    assert get_vulnerability("bias").id == "bias"
    assert get_vulnerability("misinformation").id == "misinformation"
    assert get_vulnerability("rbac").gated is True
    assert get_vulnerability("ssrf").id == "ssrf"
    assert get_vulnerability("sql_injection").id == "sql_injection"
    assert get_vulnerability("shell_injection").id == "shell_injection"
    assert get_vulnerability("robustness").id == "robustness"
    assert get_attack("roleplay").kind == "llm"
    assert get_attack("prompt_probing").id == "prompt_probing"


def test_pascal_case_aliases_for_new_entries() -> None:
    assert get_vulnerability("Bias").id == "bias"
    assert get_vulnerability("SQLInjection").id == "sql_injection"
    assert get_vulnerability("ShellInjection").id == "shell_injection"
    assert get_attack("Roleplay").id == "roleplay"
    assert get_attack("PromptProbing").kind == "llm"


def test_custom_vulnerability_requires_criteria() -> None:
    with pytest.raises(ValueError, match="criteria"):
        CustomVulnerability(id="x", criteria=" ")
    custom = CustomVulnerability(id="secrets", criteria="Do not leak keys")
    assert custom.criteria == "Do not leak keys"
    assert get_vulnerability("custom", id="secrets", criteria="Do not leak keys").id == "secrets"


def test_framework_maps_are_labelled_subsets() -> None:
    assert FRAMEWORKS["owasp_llm_2025"].coverage == "subset"
    assert "prompt_leakage" in FRAMEWORKS["owasp_llm_2025"].vulnerability_ids
    assert FRAMEWORKS["mitre_atlas_2026"].coverage == "subset"
    assert "ssrf" in FRAMEWORKS["mitre_atlas_2026"].vulnerability_ids


def test_vulnerabilities_for_owasp_includes_gated_items() -> None:
    vulns = vulnerabilities_for("owasp_llm_2025")
    assert vulns
    gated = [item for item in vulns if item.gated]
    assert gated
    ids = {item.id for item in vulns}
    assert {"prompt_leakage", "ssrf", "sql_injection", "shell_injection", "rbac"} <= ids


def test_vulnerabilities_for_unknown_framework() -> None:
    with pytest.raises(KeyError, match="unknown framework"):
        vulnerabilities_for("not_a_framework")


def test_expertize_spelling_is_preserved() -> None:
    vuln = get_vulnerability("misinformation")
    assert "expertize_misrepresentation" in vuln.allowed_types
    assert "expertise_misrepresentation" not in vuln.allowed_types
