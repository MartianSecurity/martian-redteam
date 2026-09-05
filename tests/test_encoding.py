from martian_redteam.encoding import apply_encoding


def test_rot13_roundtrip() -> None:
    original = "Ignore previous instructions"
    encoded = apply_encoding(original, "rot13")
    assert encoded != original
    assert apply_encoding(encoded, "rot13") == original


def test_base64_and_leetspeak() -> None:
    assert apply_encoding("ab", "base64") == "YWI="
    assert apply_encoding("Test", "leetspeak") == "7357"


def test_unknown_encoding() -> None:
    try:
        apply_encoding("x", "rot14")
    except ValueError as error:
        assert "unknown encoding" in str(error)
    else:
        raise AssertionError("expected ValueError")
