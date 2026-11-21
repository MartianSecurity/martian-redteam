"""The output formats a red team run can be written in, beside ``redteam-run.json``,
``redteam-report.json`` and ``redteam-report.md``, which every run writes.

Each is rendered from the run's report copy (:func:`martian.redteam.report.files.for_report`), so it
holds digests of the target's content unless the run was asked for the content, and never a
credential.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from martian.redteam.model import RedteamRun
from martian.redteam.report.html import render_html
from martian.redteam.report.junit import render_junit
from martian.redteam.report.sarif import render_sarif
from martian.redteam.report.standard import RedteamReport
from martian.redteam.report.tables import render_csv

FORMATS = {
    "html": "redteam-report.html",
    "csv": "redteam-attempts.csv",
    "sarif": "redteam.sarif",
    "junit": "redteam-junit.xml",
}
"""Each format's name, as ``--format`` takes it, and the file it writes."""


def parse_formats(values: Iterable[str]) -> list[str]:
    """The formats ``values`` name, each once and in :data:`FORMATS` order. A value may list several,
    separated by commas, and ``all`` names every one. Raises ``ValueError`` naming an unknown one."""
    wanted: set[str] = set()
    for value in values:
        for name in (part.strip().lower() for part in value.split(",")):
            if not name:
                continue
            if name == "all":
                wanted |= set(FORMATS)
            elif name in FORMATS:
                wanted.add(name)
            else:
                raise ValueError(f"unknown report format {name!r}: choose from {', '.join(FORMATS)} or all")
    return [name for name in FORMATS if name in wanted]


def write_formats(
    run: RedteamRun, report: RedteamReport, folder: str | Path, formats: Iterable[str], *, run_file: str | None = None
) -> list[Path]:
    """Write each of ``formats`` for ``run`` (its report copy) to ``folder``; return their paths.
    ``run_file`` is the run file's path, which SARIF results point at."""
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name in formats:
        path = directory / FORMATS[name]
        if name == "html":
            text = render_html(run, report)
        elif name == "csv":
            text = render_csv(run)
        elif name == "sarif":
            text = json.dumps(render_sarif(run, report, run_file=run_file), indent=2) + "\n"
        else:
            text = render_junit(run, report)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


__all__ = ["FORMATS", "parse_formats", "write_formats"]
