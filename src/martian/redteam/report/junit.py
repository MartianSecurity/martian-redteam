"""``redteam-junit.xml``: one JUnit test case per probe, for CI test views.

A test suite per vulnerability, a test case per probe (``<type> via <method>``):

- a ``failure`` when the probe has a finding, of type ``confirmed`` or ``suspected``;
- otherwise an ``error`` when an attack attempt was undetermined, stopped or errored, or none ran,
  because none of these is a pass;
- otherwise it passed: every attack attempt was decided and the target resisted.

Messages name the label, the level and the counts; they never hold the target's content.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from martian.redteam.model import BUDGET_USAGE_UNAVAILABLE, RedteamRun, budget_usage_error
from martian.redteam.report.standard import ProbeRow, RedteamReport, build_report


def _verdict(row: ProbeRow, finding: dict[str, str] | None) -> tuple[str, str, str] | None:
    """``(element, type, message)`` for a probe that did not pass, else ``None``."""
    if finding is not None:
        return "failure", finding["counted_as"], f"{finding['label']} (evidence level {row.evidence_level})"
    if row.attempts == 0:
        return "error", "not_run", "no attack attempt ran"
    for count, kind, words in (
        (row.errors, "error", "errored"),
        (row.stopped, "stopped", "were stopped by the budget"),
        (row.undetermined, "undetermined", "were undetermined"),
    ):
        if count:
            return "error", kind, f"{count} of {row.attempts} attempts {words}"
    return None


def render_junit(run: RedteamRun, report: RedteamReport | None = None) -> str:
    report = report or build_report(run)
    findings = {item.probe_id: {"counted_as": item.counted_as, "label": item.label} for item in report.findings.items}
    root = ET.Element("testsuites", name="martian-redteam")
    totals = {"tests": 0, "failures": 0, "errors": 0}
    suites: dict[str, ET.Element] = {}
    counts: dict[str, dict[str, int]] = {}
    for row in report.probes:
        suite = suites.get(row.vulnerability)
        if suite is None:
            suite = suites[row.vulnerability] = ET.SubElement(root, "testsuite", name=row.vulnerability)
            counts[row.vulnerability] = {"tests": 0, "failures": 0, "errors": 0}
        case = ET.SubElement(
            suite,
            "testcase",
            classname=f"redteam.{row.vulnerability}",
            name=f"{row.vulnerability_type} via {row.attack_method}",
        )
        counts[row.vulnerability]["tests"] += 1
        verdict = _verdict(row, findings.get(row.id))
        if verdict is not None:
            element, kind, message = verdict
            ET.SubElement(case, element, type=kind, message=message).text = (
                f"probe {row.id}: {row.hits} hit, {row.resisted} held, {row.undetermined} undetermined, "
                f"{row.stopped} stopped, {row.errors} errors"
            )
            counts[row.vulnerability]["failures" if element == "failure" else "errors"] += 1
    for name, suite in suites.items():
        for key, value in counts[name].items():
            suite.set(key, str(value))
            totals[key] += value
        suite.set("skipped", "0")
    if budget_error := budget_usage_error(run.budget):
        suite = ET.SubElement(root, "testsuite", name="budget", tests="1", errors="1", failures="0", skipped="0")
        case = ET.SubElement(suite, "testcase", classname="redteam.budget", name="verify model usage")
        ET.SubElement(case, "error", type=BUDGET_USAGE_UNAVAILABLE, message=budget_error)
        totals["tests"] += 1
        totals["errors"] += 1
    for key, value in totals.items():
        root.set(key, str(value))
    ET.indent(root)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


__all__ = ["render_junit"]
