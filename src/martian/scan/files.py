"""Which files `martian scan` sends, and the ZIP archive it sends them in.

The client reads no file's content beyond packing it: it chooses files by path, hashes and zips them.
What the files mean is the server's business (decisions A9 and S11 of MartianSecurity/martian-notes#387).

Selection, in order:

1. The candidate list. Inside a Git work tree it is ``git ls-files --cached --others --exclude-standard``:
   tracked and untracked files, without what ``.gitignore`` (and Git's other exclude files) ignore.
   Outside one, every file under the folder except ``.git/``, and no ignore file is read.
2. The include patterns: ``DEFAULT_INCLUDE`` (the files coding agents load) unless the caller gives its
   own. With the defaults, every folder that holds a ``SKILL.md`` or a ``.claude-plugin/`` folder is sent
   whole, because a skill or a plugin is its folder.
3. Symbolic links are never followed. A link that the include patterns could reach and that points out
   of the folder refuses the whole scan (:class:`SymlinkOutside`); a link that stays inside is not sent
   and is listed as skipped, since its target is sent in its own right when it is selected.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import subprocess
import zipfile
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

# The files coding agents and their harnesses load: skills, plugins, sub-agents, hooks, settings, rules
# files and MCP configuration. A pattern without a slash matches a name at any depth; ``**`` matches any
# number of folders. This is a list of paths, not a judgement about any file.
DEFAULT_INCLUDE: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "GEMINI.md",
    ".mcp.json",
    "mcp.json",
    "mcp_config.json",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    "**/.clinerules/**",
    "**/.claude/**",
    "**/.agents/**",
    "**/.cursor/**",
    "**/.windsurf/**",
    "**/.gemini/**",
    "**/.codex/**",
    "**/.continue/**",
    "**/.roo/**",
    "**/.kiro/**",
    "**/.amazonq/**",
    ".github/copilot-instructions.md",
    ".github/instructions/**",
    ".github/prompts/**",
    ".github/chatmodes/**",
    ".github/agents/**",
)

# With the default patterns, a folder holding one of these is sent whole.
FOLDER_MARKERS = ("SKILL.md", ".claude-plugin")


class SelectionError(Exception):
    """The folder cannot be packed as asked; the message names no file content."""


class SymlinkOutside(SelectionError):
    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        shown = ", ".join(paths[:10]) + (f" and {len(paths) - 10} more" if len(paths) > 10 else "")
        super().__init__(
            f"Refusing to scan: {len(paths)} symbolic link(s) point out of the folder: {shown}. "
            "An agent would read the file it points at, which this scan cannot send. "
            "Replace the link with the file, or leave it out with --include."
        )


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: str  # "symlink" (a link inside the folder) or "not_regular" (a submodule, socket, device)


@dataclass(frozen=True)
class Selection:
    root: Path
    files: tuple[str, ...]
    skipped: tuple[Skipped, ...]
    git: bool
    """Whether the candidates came from Git, so ``.gitignore`` applied."""


@dataclass(frozen=True)
class Packed:
    archive: bytes
    content_digest: str
    files: int
    unpacked_bytes: int
    hashes: dict[str, str] = field(repr=False)


# --- patterns ------------------------------------------------------------------------------------


def _segments(pattern: str) -> list[str]:
    cleaned = pattern.strip().replace("\\", "/")
    folder = cleaned.endswith("/")
    cleaned = cleaned.strip("/")
    if not cleaned:
        raise SelectionError("an include pattern is empty")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise SelectionError(f"include pattern {pattern!r} leaves the folder")
    if "/" not in pattern.strip().rstrip("/"):  # a name, at any depth; a leading slash anchors it
        parts = ["**", *parts]
    if folder:
        parts.append("**")
    return parts


def _match(pattern: list[str], path: list[str], *, below: bool = False) -> bool:
    """Whether ``path`` matches ``pattern``. With ``below``, whether some path under the folder
    ``path`` could match it."""
    if not pattern:
        return not path
    head = pattern[0]
    if head == "**":
        if len(pattern) == 1:
            return True
        return any(_match(pattern[1:], path[index:], below=below) for index in range(len(path) + 1))
    if not path:
        return below
    return fnmatchcase(path[0], head) and _match(pattern[1:], path[1:], below=below)


class Patterns:
    def __init__(self, patterns: tuple[str, ...] | list[str]) -> None:
        if not patterns:
            raise SelectionError("give at least one include pattern")
        self.patterns = tuple(patterns)
        self._compiled = [_segments(pattern) for pattern in patterns]

    def matches(self, path: str) -> bool:
        parts = path.split("/")
        return any(_match(pattern, parts) for pattern in self._compiled)

    def could_reach_below(self, folder: str) -> bool:
        parts = folder.split("/")
        return any(_match(pattern, parts, below=True) for pattern in self._compiled)


# --- candidates ----------------------------------------------------------------------------------


def _git_candidates(root: Path) -> list[str] | None:
    """The files Git would list, relative to ``root``; None outside a Git work tree or without Git."""
    # core.fsmonitor names a program Git runs; a folder's own Git config must not start one.
    base = ["git", "-c", "core.fsmonitor=false", "-C", str(root)]
    try:
        inside = subprocess.run(
            [*base, "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    try:
        listed = subprocess.run(
            [*base, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SelectionError(f"git ls-files could not list the folder ({type(error).__name__})") from None
    if listed.returncode != 0:
        raise SelectionError("git ls-files could not list the folder")
    names = listed.stdout.decode("utf-8", "surrogateescape").split("\0")
    return sorted({name for name in names if name})


def _walked_candidates(root: Path) -> list[str]:
    found: list[str] = []
    for folder, folders, files in os.walk(root, followlinks=False):
        here = Path(folder)
        kept = []
        for name in folders:
            if name == ".git":
                continue
            if (here / name).is_symlink():
                found.append((here / name).relative_to(root).as_posix())
            else:
                kept.append(name)
        folders[:] = sorted(kept)
        found.extend((here / name).relative_to(root).as_posix() for name in files)
    return sorted(found)


def _inside(root: Path, link: Path) -> bool:
    try:
        target = link.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return target == root or root in target.parents


def collect(folder: Path, include: tuple[str, ...] | list[str] | None = None) -> Selection:
    """Choose the files to send from ``folder`` (see the module docstring)."""
    try:
        root = folder.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SelectionError(f"{folder} does not exist") from None
    if not root.is_dir():
        raise SelectionError(f"{folder} is not a folder")
    patterns = Patterns(include if include else DEFAULT_INCLUDE)
    listed = _git_candidates(root)
    git = listed is not None
    candidates = listed if listed is not None else _walked_candidates(root)

    whole: list[str] = []
    if not include:
        for path in candidates:
            parts = path.split("/")
            if parts[-1] == FOLDER_MARKERS[0]:
                whole.append("/".join(parts[:-1]))
            elif FOLDER_MARKERS[1] in parts[:-1]:
                whole.append("/".join(parts[: parts.index(FOLDER_MARKERS[1])]))

    def selected(path: str) -> bool:
        if patterns.matches(path):
            return True
        return any(not prefix or path.startswith(prefix + "/") for prefix in whole)

    def reachable_below(path: str) -> bool:
        if patterns.could_reach_below(path):
            return True
        return any(not prefix or path.startswith(prefix + "/") or prefix.startswith(path + "/") for prefix in whole)

    files: list[str] = []
    skipped: list[Skipped] = []
    outside: list[str] = []
    for path in candidates:
        full = root / path
        try:
            mode = full.lstat().st_mode
        except FileNotFoundError:
            continue  # tracked by Git but deleted in the work tree: there is nothing to send
        except OSError:
            raise SelectionError(f"cannot read {path}") from None
        if stat.S_ISLNK(mode):
            pointed_dir = full.is_dir()
            if not (selected(path) or (pointed_dir and reachable_below(path))):
                continue
            if _inside(root, full):
                skipped.append(Skipped(path, "symlink"))
            else:
                outside.append(path)
            continue
        if not selected(path):
            continue
        if stat.S_ISREG(mode):
            files.append(path)
        else:
            skipped.append(Skipped(path, "not_regular"))
    if outside:
        raise SymlinkOutside(outside)
    return Selection(root=root, files=tuple(files), skipped=tuple(skipped), git=git)


# --- the archive ---------------------------------------------------------------------------------


def content_digest(hashes: dict[str, str]) -> str:
    """The Scanner's content digest: SHA-256 over each file's path, a NUL, its SHA-256 in hex and a
    newline, in path order. The backend computes the same over the archive it receives."""
    hasher = hashlib.sha256()
    for path, sha in sorted(hashes.items()):
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(sha.encode("ascii"))
        hasher.update(b"\n")
    return f"sha256:{hasher.hexdigest()}"


def pack(selection: Selection) -> Packed:
    """A deterministic ZIP of the selected files: sorted, regular file entries only, a fixed time and
    mode, deflate. The same files give the same bytes."""
    if not selection.files:
        raise SelectionError("no file to scan: nothing in the folder matches the include patterns")
    buffer = io.BytesIO()
    hashes: dict[str, str] = {}
    total = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False) as archive:
        for path in sorted(selection.files):
            try:
                path.encode("utf-8")
            except UnicodeEncodeError:
                raise SelectionError(f"{path!r} is not a UTF-8 file name") from None
            full = selection.root / path
            # Opened without following a link swapped in after selection.
            try:
                descriptor = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                raise SelectionError(f"cannot read {path}") from None
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise SelectionError(f"{path} is no longer a regular file")
                data = handle.read()
            hashes[path] = hashlib.sha256(data).hexdigest()
            total += len(data)
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            try:
                archive.writestr(info, data)
            except zipfile.LargeZipFile:
                raise SelectionError("the archive would need ZIP64, which the server does not accept") from None
    return Packed(
        archive=buffer.getvalue(),
        content_digest=content_digest(hashes),
        files=len(hashes),
        unpacked_bytes=total,
        hashes=hashes,
    )
