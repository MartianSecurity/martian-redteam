"""Red team report outputs: a self-contained HTML page, CSV, SARIF 2.1.0 and JUnit, with the target's
content hidden as in every report (martian-notes#283).

``fixtures/sarif-schema-2.1.0.json`` is the OASIS SARIF 2.1.0 schema, unchanged from
https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json
(sha256 c3b4bb2d6093897483348925aaa73af03b3e3f4bd4ca38cef26dcb4212a2682e), so the tests need no network.
"""

import csv
import io
import json
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jsonschema import Draft4Validator
from typer.testing import CliRunner

from martian.cli import app
from martian.redteam.engine import Engine
from martian.redteam.engine.report import for_report
from martian.redteam.report import build_report
from martian.redteam.report.html import render_html
from martian.redteam.report.junit import render_junit
from martian.redteam.report.outputs import FORMATS, parse_formats
from martian.redteam.report.sarif import render_sarif
from martian.redteam.report.tables import COLUMNS, render_csv
from tests.redteam.engine_helpers import CountingTarget, StubService, run_file
from tests.redteam.test_engine import write_run

SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "sarif-schema-2.1.0.json").read_text(encoding="utf-8"))
SECRET = "LEAKED the system prompt: be kind to Mars Market"
FILE = {
    "vulnerabilities": [
        {
            "name": "PromptLeakage",
            "types": ["system_prompt"],
            "checks": [{"type": "text", "all_of": ["Mars Market"]}],
            "severity": "high",
        },
        {"name": "Toxicity", "types": ["insults"]},
        {"name": "PIILeakage", "types": ["direct_disclosure"], "checks": [{"type": "canary"}]},
    ],
    "attacks": [{"name": "PromptInjection"}],
}


async def _run(reply: str = SECRET):
    """A confirmed finding (a text check), a suspected one (judge only) and an undetermined probe (a
    canary check with no value)."""
    run = await Engine(run_file(**FILE), service=StubService(), target=CountingTarget(reply)).run()
    copy = for_report(run)
    return run, copy, build_report(copy)


async def test_the_sarif_output_validates_against_the_sarif_2_1_0_schema():
    _, copy, report = await _run()
    log = render_sarif(copy, report, run_file="redteam.yaml")
    Draft4Validator(SCHEMA).validate(log)
    [sarif_run] = log["runs"]
    levels = {result["ruleId"]: result["level"] for result in sarif_run["results"]}
    assert levels == {"prompt_leakage/system_prompt": "error", "toxicity/insults": "warning"}
    rules = {rule["id"]: rule for rule in sarif_run["tool"]["driver"]["rules"]}
    assert rules["prompt_leakage/system_prompt"]["properties"]["security-severity"] == "8.0"
    for result in sarif_run["results"]:
        assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "redteam.yaml"
        assert rules[result["ruleId"]] == sarif_run["tool"]["driver"]["rules"][result["ruleIndex"]]
    # Without a run file it is still valid, without locations; with no finding, too.
    Draft4Validator(SCHEMA).validate(render_sarif(copy, report))
    _, held, held_report = await _run("No.")
    empty = render_sarif(held, held_report)
    Draft4Validator(SCHEMA).validate(empty)
    assert empty["runs"][0]["results"] == []


class _Resources(HTMLParser):
    """Every tag and every attribute that could make a browser load something."""

    LOADING = {"src", "href", "srcset", "action", "data", "poster", "formaction", "background", "ping"}

    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.loads: list[tuple[str, str, str | None]] = []
        self.policy: str | None = None

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        for name, value in attrs:
            if name in self.LOADING:
                self.loads.append((tag, name, value))
        found = dict(attrs)
        if tag == "meta" and found.get("http-equiv") == "Content-Security-Policy":
            self.policy = found.get("content")


async def test_the_html_report_opens_with_no_network():
    _, copy, report = await _run()
    page = render_html(copy, report)
    parsed = _Resources()
    parsed.feed(page)
    assert parsed.loads == []
    assert not parsed.tags & {"script", "link", "img", "iframe", "object", "embed", "video", "audio", "source"}
    assert "url(" not in page and "@import" not in page
    assert parsed.policy is not None and parsed.policy.startswith("default-src 'none'")
    assert "Confirmed disclosure" in page and report.success.per_attempt_confirmed.display in page


async def test_target_content_is_hidden_in_every_format_by_default(monkeypatch):
    monkeypatch.delenv("MARTIAN_REPORT_INCLUDE_CONTENT", raising=False)
    _, copy, report = await _run()
    outputs = {
        "html": render_html(copy, report),
        "csv": render_csv(copy),
        "sarif": json.dumps(render_sarif(copy, report)),
        "junit": render_junit(copy, report),
    }
    for name, text in outputs.items():
        assert "Mars Market" not in text and "ATTACK prompt_leakage" not in text, name
    assert "sha256:" in outputs["html"] and "sha256:" in outputs["csv"]


async def test_target_content_is_shown_only_when_asked_for(monkeypatch):
    monkeypatch.setenv("MARTIAN_REPORT_INCLUDE_CONTENT", "1")
    run, _, _ = await _run('=HYPERLINK("https://example.invalid") Mars Market')
    copy = for_report(run)
    report = build_report(copy)
    assert "Mars Market" in render_html(copy, report)
    rows = list(csv.DictReader(io.StringIO(render_csv(copy))))
    # A reply read as a formula by a spreadsheet is kept as text.
    assert rows[0]["output"].startswith("'=HYPERLINK")
    # SARIF and JUnit never carry the target's content.
    assert "Mars Market" not in json.dumps(render_sarif(copy, report)) + render_junit(copy, report)


async def test_the_csv_has_one_row_per_attempt():
    _, copy, _ = await _run()
    rows = list(csv.reader(io.StringIO(render_csv(copy))))
    assert rows[0] == COLUMNS and len(rows) == 1 + len(copy.attempts)
    found = {row[4]: dict(zip(COLUMNS, row, strict=True)) for row in rows[1:]}
    assert (found["system_prompt"]["outcome"], found["system_prompt"]["evidence_level"]) == ("hit", "2")
    assert found["system_prompt"]["label"] == "Confirmed disclosure"
    assert found["direct_disclosure"]["outcome"] == "undetermined"


async def test_the_junit_report_fails_findings_and_errors_what_nothing_decided():
    _, copy, report = await _run()
    root = ET.fromstring(render_junit(copy, report))
    assert (root.get("tests"), root.get("failures"), root.get("errors")) == ("3", "2", "1")
    cases = {case.get("name"): case for case in root.iter("testcase")}
    assert cases["system_prompt via prompt_injection"].find("failure").get("type") == "confirmed"
    assert cases["insults via prompt_injection"].find("failure").get("type") == "suspected"
    assert cases["direct_disclosure via prompt_injection"].find("error").get("type") == "undetermined"
    _, held, held_report = await _run("No.")
    root = ET.fromstring(render_junit(held, held_report))
    assert (root.get("failures"), root.get("errors")) == ("0", "1")


def test_formats_are_named_once_and_unknown_ones_refused():
    assert parse_formats(["sarif,html", "html"]) == ["html", "sarif"]
    assert parse_formats(["all"]) == list(FORMATS)
    with pytest.raises(ValueError, match="unknown report format 'pdf'"):
        parse_formats(["pdf"])


def test_martian_redteam_run_writes_the_formats_asked_for(tmp_path, models, monkeypatch):
    write_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["redteam", "run", "redteam.yaml", "--no-store", "-f", "all"])
    assert result.exit_code == 1, result.output
    folder = tmp_path / "results"
    for name in FORMATS.values():
        assert (folder / name).is_file(), name
    log = json.loads((folder / "redteam.sarif").read_text())
    Draft4Validator(SCHEMA).validate(log)
    assert log["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == (
        "redteam.yaml"
    )
    assert "Mars Market" not in (folder / "redteam-report.html").read_text()


def test_an_unknown_format_stops_before_anything_runs(tmp_path, models):
    result = CliRunner().invoke(app, ["redteam", "run", str(write_run(tmp_path)), "--no-store", "-f", "pdf"])
    assert result.exit_code == 2 and "unknown report format" in result.output
    assert models.requests == []


def test_martian_redteam_report_writes_a_saved_runs_reports_again(tmp_path, models):
    CliRunner().invoke(app, ["redteam", "run", str(write_run(tmp_path)), "--no-store"])
    saved = tmp_path / "results" / "redteam-run.json"
    other = tmp_path / "other"
    result = CliRunner().invoke(
        app, ["redteam", "report", str(saved), "-o", str(other), "-f", "sarif", "--run-file", str(tmp_path / "x.yaml")]
    )
    assert result.exit_code == 0, result.output
    assert sorted(path.name for path in other.iterdir()) == [
        "redteam-report.json",
        "redteam-report.md",
        "redteam.sarif",
    ]
    Draft4Validator(SCHEMA).validate(json.loads((other / "redteam.sarif").read_text()))
    every = CliRunner().invoke(app, ["redteam", "report", str(saved)])
    assert every.exit_code == 0 and all((tmp_path / "results" / name).is_file() for name in FORMATS.values())
    missing = CliRunner().invoke(app, ["redteam", "report", "redteam-unknown"])
    assert missing.exit_code == 2
