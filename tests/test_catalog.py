from martian_redteam.catalog import PromptLeakage, get_attack, get_vulnerability


def test_unknown_type_rejected() -> None:
    try:
        PromptLeakage(types=["not_a_type"])
    except ValueError as error:
        assert "not_a_type" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_yaml_name_lookup() -> None:
    vuln = get_vulnerability("ExcessiveAgency", ["functionality"])
    assert vuln.id == "excessive_agency"
    assert vuln.gated is True
    attack = get_attack("Base64")
    assert attack.kind == "encoding"
