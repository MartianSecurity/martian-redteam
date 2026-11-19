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
3. The policy file's ``paths`` and ``include`` (``.martian/scan.yml``, as the server validated it): only
   files under one of ``paths`` are candidates, and with the default patterns only files of the add-on
   families ``include`` names are sent (:func:`family_of`). Explicit include patterns choose the files
   themselves, so families do not apply to them.
4. Symbolic links are never followed. A link that the include patterns could reach and that points out
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
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
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

# The add-on families a policy file's ``include`` names. Each file the default patterns send belongs to
# one, by its path alone (:func:`family_of`); ``agent-code`` has no default path, so the agent's own code
# is sent only through explicit include patterns.
FAMILIES: tuple[str, ...] = ("skills", "mcp", "plugins", "subagents", "harness-config", "agent-code")
_MCP_NAMES = frozenset({".mcp.json", "mcp.json", "mcp_config.json"})
_AGENT_FOLDERS = frozenset(
    {".claude", ".agents", ".cursor", ".windsurf", ".gemini", ".codex", ".continue", ".roo", ".kiro", ".amazonq",
     ".clinerules", ".github"}
)  # fmt: skip


def _under(path: str, folder: str) -> bool:
    return not folder or path.startswith(folder + "/")


def family_of(path: str, *, skills: tuple[str, ...] = (), plugins: tuple[str, ...] = ()) -> str:
    """The add-on family of a file the default patterns send, by its path: in a plugin folder
    (``plugins``), in a skill folder (``skills``) or an agent folder's ``skills/``, an MCP configuration
    file, an agent folder's ``agents/`` or ``chatmodes/`` (``subagents``), else ``harness-config``."""
    if any(_under(path, folder) for folder in plugins):
        return "plugins"
    if any(_under(path, folder) for folder in skills):
        return "skills"
    parts = path.split("/")
    if parts[-1] in _MCP_NAMES:
        return "mcp"
    for index, part in enumerate(parts[:-1]):
        if part in _AGENT_FOLDERS and index + 1 < len(parts) - 1:
            if parts[index + 1] == "skills":
                return "skills"
            if parts[index + 1] in ("agents", "chatmodes"):
                return "subagents"
    return "harness-config"


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


class LinkedParent(SelectionError):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"cannot read through symbolic link {path!r}")


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
    left_out: tuple[str, ...] = ()
    """Files the default patterns chose that the policy file's ``include`` left out."""


@dataclass(frozen=True)
class Packed:
    archive: bytes
    content_digest: str
    files: int
    unpacked_bytes: int
    hashes: dict[str, str] = field(repr=False)
    git_oids: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)


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


@contextmanager
def _directory(start: int, parts: Sequence[str]) -> Iterator[int]:
    """Walk directories by descriptor. Only the current directory needs to stay open."""
    current = start
    try:
        for index, part in enumerate(parts):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except OSError:
                if stat.S_ISLNK(os.stat(part, dir_fd=current, follow_symlinks=False).st_mode):
                    raise LinkedParent("/".join(parts[: index + 1])) from None
                raise
            if current != start:
                os.close(current)
            current = child
        yield current
    finally:
        if current != start:
            os.close(current)


@contextmanager
def open_root(root: Path) -> Iterator[int]:
    """Open a resolved scan root without trusting a path component that changed after resolution."""
    if not (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    ):
        raise SelectionError("this platform cannot safely read a scan folder without following symbolic links")
    with ExitStack() as stack:
        try:
            anchor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, anchor)
            descriptor = stack.enter_context(_directory(anchor, root.parts[1:]))
        except OSError:
            raise SelectionError(f"cannot open the scan folder {str(root)!r}") from None
        yield descriptor


def _path_parts(path: str) -> list[str]:
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SelectionError(f"invalid scan path {path!r}")
    return parts


def read_regular(root: int, path: str, *, limit: int = -1) -> bytes:
    """Read only a regular file reached without following links, including at open time."""
    parts = _path_parts(path)
    with _directory(root, parts[:-1]) as parent:
        mode = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode
        if not stat.S_ISREG(mode):
            raise SelectionError(f"{path!r} is not a regular file")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise SelectionError(f"{path!r} is not a regular file")
            return handle.read(limit)


def _within(paths: tuple[str, ...]) -> tuple[str, ...]:
    found = []
    for path in paths:
        parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
        if ".." in parts or path.startswith("/"):
            raise SelectionError(f"policy path {path!r} leaves the folder")
        found.append("/".join(parts))
    return tuple(found)


def collect(
    folder: Path,
    include: tuple[str, ...] | list[str] | None = None,
    *,
    paths: tuple[str, ...] = (".",),
    families: tuple[str, ...] = FAMILIES,
) -> Selection:
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
    roots = _within(paths)
    if not roots:
        raise SelectionError("the policy file names no path")
    candidates = [path for path in candidates if any(path == top or _under(path, top) for top in roots)]

    skills: list[str] = []
    plugins: list[str] = []
    if not include:
        for path in candidates:
            parts = path.split("/")
            if parts[-1] == FOLDER_MARKERS[0]:
                skills.append("/".join(parts[:-1]))
            elif FOLDER_MARKERS[1] in parts[:-1]:
                plugins.append("/".join(parts[: parts.index(FOLDER_MARKERS[1])]))
    whole = skills + plugins
    wanted = frozenset(families)
    unknown = wanted - set(FAMILIES)
    if unknown:
        raise SelectionError(f"unknown add-on families {sorted(unknown)}")
    left_out: list[str] = []

    def selected(path: str) -> bool:
        if patterns.matches(path):
            return True
        return any(not prefix or path.startswith(prefix + "/") for prefix in whole)

    def reachable_below(path: str) -> bool:
        if patterns.could_reach_below(path):
            return True
        return any(not prefix or path.startswith(prefix + "/") or prefix.startswith(path + "/") for prefix in whole)

    files: list[str] = []
    skipped: dict[str, Skipped] = {}
    outside: set[str] = set()

    def skip_link(path: str) -> None:
        if _inside(root, root / path):
            skipped[path] = Skipped(path, "symlink")
        else:
            outside.add(path)

    with open_root(root) as descriptor:
        for path in candidates:
            parts = _path_parts(path)
            try:
                with _directory(descriptor, parts[:-1]) as parent:
                    mode = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue  # tracked by Git but deleted in the work tree: there is nothing to send
            except LinkedParent as link:
                if selected(path) or selected(link.path):
                    skip_link(link.path)
                continue
            except OSError:
                raise SelectionError(f"cannot read {path!r}") from None
            if stat.S_ISLNK(mode):
                if selected(path) or ((root / path).is_dir() and reachable_below(path)):
                    skip_link(path)
                continue
            if not selected(path):
                continue
            if not include and family_of(path, skills=tuple(skills), plugins=tuple(plugins)) not in wanted:
                left_out.append(path)
                continue
            if stat.S_ISREG(mode):
                files.append(path)
            else:
                skipped[path] = Skipped(path, "not_regular")
    if outside:
        raise SymlinkOutside(sorted(outside))
    return Selection(
        root=root,
        files=tuple(files),
        skipped=tuple(skipped[path] for path in sorted(skipped)),
        git=git,
        left_out=tuple(left_out),
    )


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
    git_oids: dict[str, tuple[str, str]] = {}
    total = 0
    with (
        open_root(selection.root) as root,
        zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False) as archive,
    ):
        for path in sorted(selection.files):
            try:
                path.encode("utf-8")
            except UnicodeEncodeError:
                raise SelectionError(f"{path!r} is not a UTF-8 file name") from None
            try:
                data = read_regular(root, path)
            except OSError:
                raise SelectionError(f"cannot read {path!r}") from None
            hashes[path] = hashlib.sha256(data).hexdigest()
            if selection.git:
                header = f"blob {len(data)}\0".encode("ascii")
                sha1 = hashlib.sha1(header, usedforsecurity=False)
                sha256 = hashlib.sha256(header)
                sha1.update(data)
                sha256.update(data)
                git_oids[path] = (sha1.hexdigest(), sha256.hexdigest())
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
        git_oids=git_oids,
    )
