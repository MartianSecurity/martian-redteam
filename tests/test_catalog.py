import pytest

from martian_redteam.catalog import FRAMEWORKS, CustomVulnerability, PromptLeakage, get_attack, get_vulnerability


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValueError, match="not_a_type"):
        PromptLeakage(types=["not_a_type"])


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


def test_custom_vulnerability_requires_criteria() -> None:
    with pytest.raises(ValueError, match="criteria"):
        CustomVulnerability(id="x", criteria=" ")
    custom = CustomVulnerability(id="secrets", criteria="Do not leak keys")
    assert custom.criteria == "Do not leak keys"
    assert get_vulnerability("custom", id="secrets", criteria="Do not leak keys").id == "secrets"


def test_framework_maps_are_labelled_subsets() -> None:
    assert FRAMEWORKS["owasp_llm_2025"].coverage == "subset"
    assert "prompt_leakage" in FRAMEWORKS["owasp_llm_2025"].vulnerability_ids
