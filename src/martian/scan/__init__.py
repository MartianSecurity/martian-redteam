"""`martian scan`: a thin client for the Skills / MCP / Plugin Scanner (MartianSecurity/martian-notes#469).

It packs the agent files of a folder, uploads them to a Martian server, waits for the scan, prints the
findings the server reports and exits on the server's gate: 0 passed, 1 failed, 2 anything else. It
holds no rule and no detection logic and never judges a file itself (decisions A9 and S11 of
MartianSecurity/martian-notes#387): the scan runs on the server, which needs a Scanner-enabled Cloud or
Enterprise organisation and a key with ``scan:run`` and ``scan:read``.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args
from uuid import UUID

from martian._privacy import safe_text
from martian.config import ConfigError, MartianConfig
from martian.scan.client import Check, Finding, Scan, ScanApiError, ScanClient
from martian.scan.files import DEFAULT_INCLUDE, Packed, Selection, SelectionError, collect, pack

EXIT_PASSED = 0
EXIT_GATE_FAILED = 1
EXIT_ERROR = 2

SubjectKind = Literal[
    "skill", "mcp_server", "plugin", "subagent", "harness_config", "agent_code", "repository", "registry_entry"
]
SUBJECT_KINDS: tuple[str, ...] = get_args(SubjectKind)

_ACCESS_HINT = (
    "martian scan needs a Scanner-enabled Martian Cloud or Enterprise organisation and an API key with "
    "scan:run and scan:read."
)

__all__ = [
    "DEFAULT_INCLUDE",
    "EXIT_ERROR",
    "EXIT_GATE_FAILED",
    "EXIT_PASSED",
    "ScanOptions",
    "run_scan",
]


@dataclass(frozen=True)
class ScanOptions:
    folder: Path = Path(".")
    include: tuple[str, ...] = ()
    kind: str = "repository"
    name: str | None = None
    agent_id: UUID | None = None
    model_analysis: bool = True
    timeout_seconds: float = 600.0
    json_path: Path | None = None
    sarif_path: Path | None = None
    dry_run: bool = False


Echo = Callable[..., None]


def _line(value: object, limit: int = 200) -> str:
    """Server text on one terminal line: no control characters or escape sequences, no credentials."""
    text = "".join(" " if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") else char for char in str(value))
    text = re.sub(r"\s+", " ", safe_text(text)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _size(count: int) -> str:
    return (
        f"{count} B" if count < 1024 else f"{count / 1024:.1f} KiB" if count < 1024**2 else f"{count / 1024**2:.1f} MiB"
    )


def _default_name(selection: Selection) -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    return repository or selection.root.name or "scan"


def _source() -> Literal["ci", "cli"]:
    return "ci" if os.environ.get("CI", "").lower() in ("1", "true") or os.environ.get("GITHUB_ACTIONS") else "cli"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(data)
    partial.replace(path)


def _print_selection(echo: Echo, options: ScanOptions, selection: Selection, packed: Packed) -> None:
    where = "Git work tree: .gitignore applied" if selection.git else "not a Git work tree: no ignore file read"
    patterns = "your include patterns" if options.include else "the default agent paths"
    echo(
        f"Packed {packed.files} file(s), {_size(packed.unpacked_bytes)} ({_size(len(packed.archive))} zipped), "
        f"from {selection.root} ({where}; {patterns})."
    )
    echo(f"Content digest: {packed.content_digest}")
    for skipped in selection.skipped:
        reason = "a symbolic link inside the folder" if skipped.reason == "symlink" else "not a regular file"
        echo(f"Not sent: {skipped.path} ({reason})")


def _location(finding: Finding) -> str:
    if finding.line_start is None:
        return _line(finding.path)
    if finding.line_end is not None and finding.line_end != finding.line_start:
        return f"{_line(finding.path)}:{finding.line_start}-{finding.line_end}"
    return f"{_line(finding.path)}:{finding.line_start}"


def _print_findings(echo: Echo, findings: list[Finding] | None) -> None:
    if findings is None:
        echo("Findings: this server does not list a scan's findings.")
        return
    if not findings:
        echo("Findings: none reported.")
        return
    echo(f"Findings: {len(findings)}")
    for finding in findings:
        level = f"level {finding.evidence_level}"
        if finding.evidence_level_name:
            level += f" ({_line(finding.evidence_level_name, 20)})"
        echo(
            f"  {_line(finding.severity, 4)}  {level}  {_location(finding)}  {_line(finding.rule_id, 64)}  "
            f"{_line(finding.status, 12)}  {_line(finding.title)}"
        )


def _print_coverage(echo: Echo, scan: Scan) -> None:
    coverage = scan.coverage or {}
    parts = []
    for key, label in (
        ("files_read", "files read"),
        ("files_skipped", "skipped"),
        ("files_not_inspected", "not inspected"),
    ):
        if type(coverage.get(key)) is int:
            parts.append(f"{coverage[key]} {label}")
    if coverage.get("complete") is False:
        parts.append("unpacking stopped early")
    if type(coverage.get("levels_reached")) is int:
        parts.append(f"evidence levels reached: 0 to {coverage['levels_reached']}")
    stages = coverage.get("stages_run")
    if isinstance(stages, list) and all(isinstance(stage, str) for stage in stages):
        parts.append("stages: " + ", ".join(_line(stage, 20) for stage in stages))
    if parts:
        echo("Coverage: " + "; ".join(parts) + ".")


def _print_gate(echo: Echo, check: Check) -> None:
    if check.passed:
        echo("Gate: passed.")
        return
    reasons = []
    if check.blocking:
        reasons.append(f"{len(check.blocking)} blocking finding(s)")
    reasons.extend(_line(shortfall) for shortfall in check.coverage_shortfalls[:10])
    echo("Gate: failed" + (": " + "; ".join(reasons) if reasons else "") + ".", err=True)


def _fail(echo: Echo, error: Exception, *, hint: str | None = None) -> int:
    echo(f"Scan failed: {error}", err=True)
    if hint:
        echo(hint, err=True)
    return EXIT_ERROR


def run_scan(
    options: ScanOptions,
    *,
    echo: Echo,
    config: MartianConfig | None = None,
    client_factory: Callable[[MartianConfig], ScanClient] = ScanClient,
) -> int:
    """Pack, upload, wait, print and return the exit code: 0 the server's gate passed, 1 it failed,
    2 the scan did not give a result (a setup, transport or server error, a failed scan, a timeout)."""
    if options.kind not in SUBJECT_KINDS:
        return _fail(echo, ValueError(f"--kind must be one of {', '.join(SUBJECT_KINDS)}"))
    try:
        selection = collect(options.folder, options.include or None)
        packed = pack(selection)
    except SelectionError as error:
        return _fail(echo, error)
    _print_selection(echo, options, selection, packed)
    if options.dry_run:
        for path in selection.files:
            echo(f"  {path}")
        echo("Dry run: nothing was uploaded.")
        return EXIT_PASSED

    try:
        config = config or MartianConfig.from_env()
    except ConfigError as error:
        return _fail(echo, error, hint=_ACCESS_HINT)
    name = (options.name or _default_name(selection)).strip()
    subject: dict[str, object] = {"kind": options.kind, "name": name}
    if options.agent_id is not None:
        subject["agent_id"] = str(options.agent_id)
    manifest = {"subject": subject, "source": _source(), "options": {"model_analysis": options.model_analysis}}

    with client_factory(config) as client:
        try:
            scan = client.upload(manifest, packed.archive)
        except ScanApiError as error:
            hint = _ACCESS_HINT if error.status in (401, 403) else None
            if error.code == "connection_error" or error.status >= 500:
                hint = f"Look for a scan of {packed.content_digest} in the console before running it again."
            return _fail(echo, error, hint=hint)
        if scan.content_digest != packed.content_digest:
            return _fail(
                echo,
                RuntimeError(
                    f"the server reports content {scan.content_digest}, not the {packed.content_digest} that was "
                    f"packed (scan {scan.id})"
                ),
            )
        echo(f"Scan {scan.id} {scan.state}: {_line(scan.subject.kind, 20)} {_line(scan.subject.name)}.")

        def changed(now: Scan) -> None:
            echo(f"Scan {now.state}" + (f" ({_line(now.stage, 20)})" if now.stage else "") + ".")

        try:
            scan = client.wait(scan, timeout_seconds=options.timeout_seconds, on_change=changed)
        except ScanApiError as error:
            return _fail(echo, error)
        if scan.state == "failed":
            failure = scan.error
            reason = f"{_line(failure.code, 64)}: {_line(failure.message or '', 300)}" if failure else "no reason given"
            echo(f"Scan {scan.id} failed ({reason}). It is not a result.", err=True)
            return EXIT_ERROR

        try:
            findings = client.findings(scan.id)
            check = client.check(scan.id)
            exports: list[tuple[Path, bytes | None, str]] = []
            if options.json_path is not None:
                exports.append((options.json_path, client.export(scan.id, "json"), "json"))
            if options.sarif_path is not None:
                exports.append((options.sarif_path, client.export(scan.id, "sarif"), "sarif"))
        except ScanApiError as error:
            return _fail(echo, error)

    _print_findings(echo, findings)
    _print_coverage(echo, scan)
    status = EXIT_PASSED
    for target, data, form in exports:
        if data is None:
            echo(f"Not written: {target}. This server does not export a scan as {form.upper()}.", err=True)
            status = EXIT_ERROR
            continue
        try:
            _write(target, data)
        except OSError as error:
            echo(f"Not written: {target} ({type(error).__name__}).", err=True)
            status = EXIT_ERROR
            continue
        echo(f"Wrote {target}.")
    if check is None:
        echo(
            "Gate: this server does not evaluate a scan gate, so the exit code is the scan's state: completed.",
            err=True,
        )
    else:
        _print_gate(echo, check)
    if status == EXIT_ERROR:
        return EXIT_ERROR
    return EXIT_GATE_FAILED if check is not None and not check.passed else EXIT_PASSED
