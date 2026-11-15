"""A number read from outside input counts only the digits 0-9: ``²`` (which ``int()`` refuses) and
``١`` (which ``int()`` reads as 1) are bad input in every place that reads one, and a scan of
``src/`` keeps ``isdigit()``, ``isnumeric()`` and ``isdecimal()`` out of new code."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from martian.evals import _digits
from martian.evals._json_schema import check_schema
from martian.evals.mlflow_import import _Reader
from martian.evals.server import otlp
from martian.evals.trace_conventions import _listed, map_attributes

BAD = ["²", "١", "١٢"]
SRC = Path(__file__).resolve().parents[2] / "src"
# Where a str digit test may be used, and why. Every other file uses martian.evals._digits.ascii_digits.
ALLOWED = {
    "martian/evals/_digits.py": "the helper itself",
    "martian/evals/runner.py": "finds a response_path step of non-ASCII digits in order to refuse it by name",
}
CHECKS = {"isdigit", "isnumeric", "isdecimal"}


def request(length: str, body: bytes = b"{}") -> Request:
    scope = {"type": "http", "method": "POST", "headers": [(b"content-length", length.encode("latin-1"))]}
    sent = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, Any]:
        return sent.pop(0)

    return Request(scope, receive)


# A header is read as Latin-1, so the digits that can arrive in one are ¹, ² and ³.
@pytest.mark.parametrize("length", ["²", "¹", "³"])
def test_a_content_length_of_other_digits_is_not_a_declared_size(monkeypatch, length):
    # Not a size: the body is counted as it arrives, as for a missing or non-numeric Content-Length.
    monkeypatch.setattr(otlp, "MAX_BODY_BYTES", 64)
    assert asyncio.run(otlp._body(request(length))) == b"{}"
    with pytest.raises(otlp.HTTPException) as refused:
        asyncio.run(otlp._body(request(length, b" " * 65)))
    assert refused.value.status_code == 413


@pytest.mark.parametrize("count", BAD)
def test_a_token_count_of_other_digits_is_not_a_count(count):
    assert map_attributes({"gen_ai.usage.input_tokens": "12"}).input_tokens == 12
    assert map_attributes({"gen_ai.usage.input_tokens": count}).input_tokens is None


@pytest.mark.parametrize("key", BAD)
def test_a_numbered_level_of_other_digits_is_a_name_not_a_list_position(key):
    assert _listed({"0": "a", "1": "b"}) == ["a", "b"]
    assert _listed({key: "a"}) == {key: "a"}


@pytest.mark.parametrize("step", ["²", "١", "٠"])
def test_a_schema_reference_through_a_list_step_of_other_digits_points_to_nothing(step):
    schema = {"allOf": [{"type": "string"}], "properties": {"a": {"$ref": "#/allOf/0"}}}
    check_schema(schema)
    schema["properties"]["a"]["$ref"] = f"#/allOf/{step}"
    with pytest.raises(ValueError, match="points to nothing in this schema"):
        check_schema(schema)


@pytest.mark.parametrize("bad", ["²", "١"])
def test_an_mlflow_experiment_id_of_other_digits_sorts_first_and_does_not_raise(bad):
    found = [SimpleNamespace(experiment_id=i) for i in ("2", bad, "1")]
    page = type("Page", (list,), {"token": None})(found)
    store = SimpleNamespace(search_experiments=lambda **_: page)
    ordered = _Reader(store, None, None)._experiments()  # type: ignore[arg-type]
    assert [e.experiment_id for e in ordered] == [bad, "1", "2"]


def test_only_the_digits_0_to_9_count():
    assert _digits.ascii_digits("0123456789")
    assert not any(_digits.ascii_digits(text) for text in ["", "²", "١", "1²", "1.5", "-1"])


def digit_tests(path: Path) -> list[int]:
    """The lines of ``path`` that call ``.isdigit()``, ``.isnumeric()`` or ``.isdecimal()``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in CHECKS]


def test_no_source_file_tests_digits_with_isdigit_isnumeric_or_isdecimal_except_where_allowed():
    found = {
        path.relative_to(SRC).as_posix(): lines for path in sorted(SRC.rglob("*.py")) if (lines := digit_tests(path))
    }
    assert {name: lines for name, lines in found.items() if name not in ALLOWED} == {}, (
        "use martian.evals._digits.ascii_digits, or allow the file in ALLOWED with the reason"
    )
    assert set(ALLOWED) <= set(found), "an ALLOWED file no longer needs its place on the list"
