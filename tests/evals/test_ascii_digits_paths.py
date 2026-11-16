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

from martian.evals import _digits, audit
from martian.evals._json_schema import check_schema
from martian.evals._numeric import at
from martian.evals.agreement import judge_agreement
from martian.evals.mlflow_import import _Reader
from martian.evals.model import Score
from martian.evals.panel import member_sources
from martian.evals.server import otlp
from martian.evals.trace_conventions import _listed, map_attributes

BAD = ["²", "١", "١٢"]
SRC = Path(__file__).resolve().parents[2] / "src"
# Where a str digit test may be used, and why. Every other file uses martian.evals._digits.ascii_digits.
ALLOWED = {
    "martian/evals/_digits.py": "the helper itself",
    "martian/evals/runner.py": "finds a response_path step of non-ASCII digits in order to refuse it by name",
    "martian/evals/fidelity.py": "keeps a one-character digit of any script as a content word; reads no number",
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
def test_a_content_length_of_other_digits_is_not_a_declared_size(length):
    # Not a size: the body is counted as it arrives, as for a missing or non-numeric Content-Length.
    assert asyncio.run(otlp._body(request(length), 64)) == b"{}"
    with pytest.raises(otlp.HTTPException) as refused:
        asyncio.run(otlp._body(request(length, b" " * 65), 64))
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


@pytest.mark.parametrize("bad", BAD)
def test_a_judge_version_of_other_digits_is_reported_after_the_numbered_ones_and_does_not_raise(bad):
    # A score's scorer_version is whatever the application that recorded it wrote, as on live traces.
    verdicts = [
        Score(trace_id=f"tr-{n}", scorer="polite_tone", scorer_version=version, source="llm_judge", value=True)
        for n, version in enumerate(["10", bad, "2"])
    ]
    reports = judge_agreement(verdicts, [])
    assert [report.version for report in reports] == ["2", "10", bad]


@pytest.mark.parametrize("bad", BAD)
def test_a_panel_size_of_other_digits_names_no_members_and_does_not_raise(bad):
    members = {"martian.panel.1.model": "openai:/a", "martian.panel.2.model": "openai:/b"}
    assert member_sources({"martian.panel.size": "2", **members}) == ["openai:/a", "openai:/b"]
    assert member_sources({"martian.panel.size": bad, **members}) == []
    # As a live trace's judge score, which an application writes, in the judge agreement report.
    score = Score(
        trace_id="tr-1",
        scorer="polite_tone",
        scorer_version="1",
        source="llm_judge",
        value=True,
        metadata={"martian.panel.size": bad, **members},
    )
    assert [report.models for report in judge_agreement([score], [])] == [()]


@pytest.mark.parametrize("step", BAD)
def test_a_suite_audit_builds_a_path_step_of_other_digits_as_a_key_as_a_check_reads_it(step):
    # A suite audit's cheating replies put a check's expected value at its path, and its hollow
    # replies zeros at its paths (martian.evals.audit); a check reads a list position only for 0-9.
    assert audit.value_at("items.1", 3) == {"items": [None, 3]}
    placed = audit.value_at(f"items.{step}", 3)
    assert placed == {"items": {step: 3}}
    assert at(placed, ["items", step]) == [(f"items.{step}", 3)]
    assert audit.zeros_at([f"totals.{step}.amount", f"{step}.amount"]) == {
        "totals": {step: {"amount": 0}},
        step: {"amount": 0},
    }


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
