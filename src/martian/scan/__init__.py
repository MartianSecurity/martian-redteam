"""`martian scan`: a thin client for the Skills / MCP / Plugin Scanner (MartianSecurity/martian-notes#469, #470).

It sends the repository's ``.martian/scan.yml`` to the server to be checked, packs the agent files of a
folder as the file says, uploads them with the commit they are of under a request id it can retry with,
waits for the scan, prints the findings the server reports, asks the server for the gate's verdict on
the file and exits with it: 0 passed, 1 failed, 2 anything else. It holds no rule and no detection logic,
never reads the policy file's fields itself and never judges a file (decisions A9 and S11 of
MartianSecurity/martian-notes#387): the scan and the gate run on the server, which needs a
Scanner-enabled Cloud or Enterprise organisation and a key with ``scan:run`` and ``scan:read``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args
from uuid import UUID

from martian._privacy import safe_text
from martian.config import ConfigError, MartianConfig
from martian.scan.client import Finding, Gate, Policy, PolicyRefused, Scan, ScanApiError, ScanClient
from martian.scan.files import (
    DEFAULT_INCLUDE,
    FAMILIES,
    Packed,
    Selection,
    SelectionError,
    collect,
    open_root,
    pack,
    read_regular,
)

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

POLICY_FILE = Path(".martian") / "scan.yml"
MAX_POLICY_BYTES = 64 * 1024
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REF = re.compile(r"^[\x20-\x7e]{1,255}$")

__all__ = [
    "DEFAULT_INCLUDE",
    "EXIT_ERROR",
    "EXIT_GATE_FAILED",
    "EXIT_PASSED",
    "POLICY_FILE",
    "ScanOptions",
    "ScanOutcome",
    "run_scan",
    "scan",
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
    policy_path: Path | None = None
    """The gate policy file; by default ``.martian/scan.yml`` in the folder, and none when it is absent."""
    default_branch: str | None = None
    """The repository's default branch; by default GitHub's event, else ``origin/HEAD``."""
    expect_sha: str | None = None
    """A commit the scan must be of (``martian ci``: the session's); another, or none, is an error."""


@dataclass
class ScanOutcome:
    """What a run of the command ended with: its exit code, and the scan and gate evaluation it made."""

    exit_code: int
    scan_id: UUID | None = None
    gate_id: UUID | None = None
    error: str = ""
    """Why there is no verdict, when there is none."""
    reasons: list[str] = field(default_factory=list)


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


def write_export(path: Path, data: bytes) -> None:
    """Atomically write an output through a private temporary sibling."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".martian-scan-", delete=False) as handle:
            partial = Path(handle.name)
            handle.write(data)
        partial.replace(path)
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)


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
        echo(f"Not sent: {_line(skipped.path)} ({reason})")


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
            f"{_line(finding.status, 12)}  {_line(finding.title)}" + ("" if finding.gating else "  (hunt: never gates)")
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


# --- the policy file -------------------------------------------------------------------------------


def _read_policy(options: ScanOptions) -> tuple[str, Path | None]:
    """The policy file's text and where it was read; ``("", None)`` when the folder has none."""
    path = options.policy_path or options.folder / POLICY_FILE
    try:
        directory = options.policy_path.parent if options.policy_path is not None else options.folder
        root = directory.resolve(strict=True)
        relative = path.name if options.policy_path is not None else POLICY_FILE.as_posix()
        with open_root(root) as descriptor:
            data = read_regular(descriptor, relative, limit=MAX_POLICY_BYTES + 1)
    except FileNotFoundError:
        if options.policy_path is not None:
            raise SelectionError(f"the policy file {path} does not exist") from None
        return "", None
    except (OSError, RuntimeError):
        raise SelectionError(f"cannot read the policy file {path}") from None
    if len(data) > MAX_POLICY_BYTES:
        raise SelectionError(f"the policy file {path} is larger than {MAX_POLICY_BYTES // 1024} KiB")
    try:
        return data.decode("utf-8"), path
    except UnicodeDecodeError:
        raise SelectionError(f"the policy file {path} is not UTF-8") from None


def _print_policy(echo: Echo, where: Path | None, policy: Policy, options: ScanOptions) -> None:
    found = policy.policy.get("gate")
    gate: dict[str, Any] = found if isinstance(found, dict) else {}
    source = str(where) if where is not None else f"no {POLICY_FILE} (the strictest gate: every open finding blocks)"
    echo(
        f"Policy: {_line(source)}; sha256 {_line(policy.policy_sha256, 64)[:12]}; "
        f"new findings only: {'yes' if gate.get('new_findings_only') is True else 'no'}."
    )
    families = _families(policy)
    if options.include and families != FAMILIES:
        echo("Policy include not applied: --include chooses the files itself.")


def _families(policy: Policy) -> tuple[str, ...]:
    include = policy.policy.get("include")
    if not isinstance(include, list) or not all(isinstance(name, str) for name in include):
        raise ScanApiError("invalid_response", status=200)
    return tuple(include)


def _paths(policy: Policy) -> tuple[str, ...]:
    paths = policy.policy.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths):
        raise ScanApiError("invalid_response", status=200)
    return tuple(paths)


# --- the commit ------------------------------------------------------------------------------------


def _git(root: Path, *args: str, raw: bool = False) -> str | None:
    """A Git command in ``root`` with no program of the folder's own configuration; its output, or None."""
    git = ["git", "--no-replace-objects", "--literal-pathspecs", "-c", "core.fsmonitor=false", "-C", str(root)]
    try:
        done = subprocess.run(
            [*git, *args],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    text = done.stdout.decode("utf-8", "surrogateescape")
    return text if raw else text.strip()


def _github_default_branch() -> str | None:
    event = os.environ.get("GITHUB_EVENT_PATH", "")
    if not os.environ.get("GITHUB_ACTIONS") or not event:
        return None
    try:
        with open(event, encoding="utf-8") as handle:
            branch = json.load(handle).get("repository", {}).get("default_branch")
    except (OSError, ValueError, AttributeError):
        return None
    return branch if isinstance(branch, str) else None


def _commit(selection: Selection, packed: Packed, options: ScanOptions) -> tuple[dict[str, str] | None, str]:
    """Attribute the archive only when every packed file matches a regular blob in HEAD."""
    if not selection.git:
        return None, "not a Git work tree"
    if packed.git_oids.keys() != set(selection.files):
        return None, "the archive does not have a Git blob identity for every file"
    sha = _git(selection.root, "rev-parse", "HEAD")
    if not sha or not _SHA.fullmatch(sha):
        return None, "the work tree has no commit"
    prefix = _git(selection.root, "rev-parse", "--show-prefix", raw=True)
    if prefix is None:
        return None, "Git could not locate this folder in the work tree"
    prefix = prefix.removesuffix("\n")
    narrowed = ["--", prefix] if prefix else []
    tree = _git(selection.root, "ls-tree", "-rz", "--full-tree", sha, *narrowed, raw=True)
    if tree is None:
        return None, "Git could not read the commit's tree"
    regular = {}
    for entry in tree.split("\0"):
        metadata, _, path = entry.partition("\t")
        fields = metadata.split(" ")
        if len(fields) == 3 and fields[0] in ("100644", "100755") and fields[1] == "blob":
            regular[path] = fields[2]
    algorithm = 0 if len(sha) == 40 else 1
    differ = sum(regular.get(prefix + path) != oids[algorithm] for path, oids in packed.git_oids.items())
    if differ:
        return None, f"{differ} of the files sent differ from commit {sha[:12]} or are not committed"
    found: dict[str, str] = {"sha": sha}
    ref = None
    if os.environ.get("GITHUB_ACTIONS") and os.environ.get("GITHUB_SHA", "").lower() == sha:
        ref = os.environ.get("GITHUB_REF") or None
    ref = ref or _git(selection.root, "symbolic-ref", "-q", "HEAD")
    default = options.default_branch or _github_default_branch()
    if default is None:
        origin = _git(selection.root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
        default = origin.removeprefix("origin/") if origin else None
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    run_id, server = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    for key, value in (
        ("ref", ref),
        ("default_branch", default),
        ("repository", repository[:200] or None),
        ("run_url", f"{server}/{repository}/actions/runs/{run_id}" if repository and run_id else None),
    ):
        if value and _REF.fullmatch(value) and (key != "run_url" or value.startswith("https://")):
            found[key] = value
    return found, ""


# --- the run ---------------------------------------------------------------------------------------


def _print_gate(echo: Echo, gate: Gate) -> None:
    failed = gate.verdict != "passed"
    echo(f"Gate: {gate.verdict} (exit {gate.exit_code}); evaluation {gate.id}.", err=failed)
    for reason in gate.reasons[:60]:
        echo(f"  {_line(reason, 300)}", err=failed)
    if len(gate.reasons) > 60:
        echo(f"  and {len(gate.reasons) - 60} more reasons", err=failed)
    if gate.new_findings_only:
        if gate.baseline is None:
            echo("Baseline: none: no completed scan of the default branch precedes this one.", err=failed)
        else:
            git = gate.baseline.git or {}
            commit = f"{_line(git.get('ref') or 'a ref not stated', 80)} at {_line(git.get('sha') or '?', 64)[:12]}"
            echo(f"Baseline: scan {gate.baseline.scan_id} of the default branch ({commit}).", err=failed)


def run_scan(
    options: ScanOptions,
    *,
    echo: Echo,
    config: MartianConfig | None = None,
    client_factory: Callable[[MartianConfig], ScanClient] = ScanClient,
) -> int:
    """Check the policy file, pack, upload, wait, print, ask for the gate and return its exit code: 0 the
    gate passed, 1 it failed, 2 no verdict (a setup, transport or server error, a refused policy file, a
    failed scan, a timeout, an export that could not be written)."""
    return scan(options, echo=echo, config=config, client_factory=client_factory).exit_code


def scan(
    options: ScanOptions,
    *,
    echo: Echo,
    config: MartianConfig | None = None,
    client_factory: Callable[[MartianConfig], ScanClient] = ScanClient,
) -> ScanOutcome:
    """:func:`run_scan`, returning the scan and the gate evaluation it made as well (for ``martian ci``)."""
    outcome = ScanOutcome(EXIT_ERROR)

    def fail(error: object, *, hint: str | None = None) -> ScanOutcome:
        outcome.exit_code, outcome.error = EXIT_ERROR, _line(error, 500)
        echo(f"Scan failed: {outcome.error}", err=True)
        if hint:
            echo(hint, err=True)
        return outcome

    if options.kind not in SUBJECT_KINDS:
        return fail(ValueError(f"--kind must be one of {', '.join(SUBJECT_KINDS)}"))
    try:
        text, where = _read_policy(options)
    except SelectionError as error:
        return fail(error)
    if options.dry_run and config is None:
        try:
            config = MartianConfig.from_env()
        except ConfigError:
            config = None
    elif config is None:
        try:
            config = MartianConfig.from_env()
        except ConfigError as error:
            return fail(error, hint=_ACCESS_HINT)

    with client_factory(config) if config is not None else _NoClient() as client:
        policy: Policy | None = None
        if config is not None:
            try:
                policy = client.validate_policy(text)
            except PolicyRefused as refused:
                for loc, why in refused.errors:
                    echo(f"  {_line(loc or 'the file', 100)}: {_line(why, 300)}", err=True)
                return fail(f"the server refused the policy file {where or '(none: an empty policy)'}")
            except ScanApiError as error:
                return fail(error, hint=_ACCESS_HINT if error.status in (401, 403) else None)
            if policy is None:
                return fail("this server does not evaluate a gate policy file (no /v1/scanner/gate-policy route)")
        try:
            selection = (
                collect(options.folder, options.include or None, paths=_paths(policy), families=_families(policy))
                if policy is not None
                else collect(options.folder, options.include or None)
            )
            packed = pack(selection)
        except (SelectionError, ScanApiError) as error:
            return fail(error)
        _print_selection(echo, options, selection, packed)
        if policy is not None:
            _print_policy(echo, where, policy, options)
        if selection.left_out:
            echo(f"Left out by the policy file's include: {len(selection.left_out)} file(s).")
        if options.dry_run:
            for path in selection.files:
                echo(f"  {_line(path)}")
            if policy is None and where is not None:
                echo(f"Not applied: {where} (set MARTIAN_API_URL and MARTIAN_API_TOKEN to have the server read it).")
            echo("Dry run: nothing was uploaded.")
            outcome.exit_code = EXIT_PASSED
            return outcome
        assert config is not None and policy is not None

        name = (options.name or _default_name(selection)).strip()
        subject: dict[str, object] = {"kind": options.kind, "name": name}
        if options.agent_id is not None:
            subject["agent_id"] = str(options.agent_id)
        manifest: dict[str, Any] = {
            "subject": subject,
            "source": _source(),
            "options": {"model_analysis": options.model_analysis},
        }
        git, why = _commit(selection, packed, options)
        if git is not None:
            manifest["git"] = git
            branch = git.get("ref", "no ref")
            default = git.get("default_branch")
            echo(
                f"Commit: {git['sha'][:12]} ({_line(branch, 80)}"
                + (f"; default branch {_line(default, 80)}" if default else "; default branch not known")
                + ")."
            )
        else:
            echo(f"Commit: not stated ({why}); this scan is no baseline for later ones.")
        if options.expect_sha is not None and (git is None or git["sha"] != options.expect_sha.lower()):
            return fail(f"the files sent are not commit {options.expect_sha[:12]}, the commit this run is of")

        request_id = uuid.uuid4()
        try:
            made, replayed = client.upload(manifest, packed.archive, request_id=request_id)
        except ScanApiError as error:
            hint = _ACCESS_HINT if error.status in (401, 403) else None
            if error.code == "connection_error" or error.status >= 500 or error.status == 429:
                hint = f"Look for a scan of {packed.content_digest} in the console before running it again."
            return fail(error, hint=hint)
        outcome.scan_id = made.id
        if made.content_digest != packed.content_digest:
            return fail(
                RuntimeError(
                    f"the server reports content {made.content_digest}, not the {packed.content_digest} that was "
                    f"packed (scan {made.id})"
                )
            )
        again = " (the server already had this upload under its request id)" if replayed else ""
        echo(f"Scan {made.id} {made.state}: {_line(made.subject.kind, 20)} {_line(made.subject.name)}{again}.")

        def changed(now: Scan) -> None:
            echo(f"Scan {now.state}" + (f" ({_line(now.stage, 20)})" if now.stage else "") + ".")

        try:
            ended = client.wait(made, timeout_seconds=options.timeout_seconds, on_change=changed)
        except ScanApiError as error:
            return fail(error)
        exports: list[tuple[Path, bytes | None, str]] = []
        findings: list[Finding] | None = []
        try:
            if ended.state == "completed":
                findings = client.findings(ended.id)
                if options.json_path is not None:
                    exports.append((options.json_path, client.export(ended.id, "json"), "json"))
                if options.sarif_path is not None:
                    exports.append((options.sarif_path, client.export(ended.id, "sarif"), "sarif"))
            gate = client.gate(ended.id, text)
        except PolicyRefused:
            return fail("the server refused the policy file it had accepted")
        except ScanApiError as error:
            return fail(error)

    if ended.state == "failed":
        failure = ended.error
        reason = f"{_line(failure.code, 64)}: {_line(failure.message or '', 300)}" if failure else "no reason given"
        echo(f"Scan {ended.id} failed ({reason}). It is not a result.", err=True)
    else:
        _print_findings(echo, findings)
        _print_coverage(echo, ended)
    status = EXIT_PASSED
    for target, data, form in exports:
        if data is None:
            echo(f"Not written: {target}. This server does not export a scan as {form.upper()}.", err=True)
            status = EXIT_ERROR
            continue
        try:
            write_export(target, data)
        except OSError as error:
            echo(f"Not written: {target} ({type(error).__name__}).", err=True)
            status = EXIT_ERROR
            continue
        echo(f"Wrote {target}.")
    if gate is None:
        return fail("this server does not evaluate a gate policy file (no gate route)")
    outcome.gate_id = gate.id
    outcome.reasons = [_line(reason, 300) for reason in gate.reasons]
    _print_gate(echo, gate)
    outcome.exit_code = EXIT_ERROR if status == EXIT_ERROR else gate.exit_code
    if gate.exit_code == EXIT_ERROR:
        outcome.error = _line(gate.reasons[0] if gate.reasons else "the scan could not be judged", 500)
    return outcome


class _NoClient:
    """Stands in for a client in a dry run without credentials: nothing is sent."""

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *exc: object) -> None:
        return None
