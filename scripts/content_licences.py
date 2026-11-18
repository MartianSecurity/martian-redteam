"""Check the red team content register: every content file the package ships has a permitted licence.

The register is ``src/martian/redteam/catalog/licences.json``. A content file is a file Git tracks that
matches one of ``content.globs`` and none of ``content.not_content``. The check fails when:

- a content file has no entry, here or in a register ``content.delegated`` names for its folder;
- an entry names a file that is not tracked, or names one twice;
- an entry's licence is not in ``permitted``, or it does not allow commercial use, or names no source;
- a source marked ``shipped`` has a licence outside ``permitted`` or does not allow commercial use.

    python scripts/content_licences.py          # exit 0 when the register covers every content file

``tests/redteam/test_content_licences.py`` runs it in CI.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "src/martian/redteam/catalog/licences.json"


def tracked(root: Path = ROOT) -> list[str]:
    """The files Git tracks under ``src/``, as paths relative to ``root``."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "src"], cwd=root, check=True, capture_output=True
    ).stdout.decode("utf-8")
    return sorted(path for path in listed.split("\0") if path)


def content_files(files: Iterable[str], register: Mapping[str, Any]) -> list[str]:
    """The files of ``files`` that the register counts as content."""
    globs = register["content"]["globs"]
    excluded = [item["glob"] for item in register["content"]["not_content"]]
    return [
        path
        for path in files
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in globs)
        and not any(fnmatch.fnmatchcase(path, pattern) for pattern in excluded)
    ]


def _delegated(register: Mapping[str, Any], root: Path) -> dict[str, Mapping[str, Any]]:
    """The entries of the registers ``content.delegated`` names, by path, for the files their glob matches."""
    found: dict[str, Mapping[str, Any]] = {}
    for item in register["content"].get("delegated", []):
        other = Path(item["register"])
        listed = json.loads((root / other).read_text(encoding="utf-8"))["files"]
        for name, entry in listed.items():
            path = (other.parent / name).as_posix()
            if fnmatch.fnmatchcase(path, item["glob"]):
                found[path] = entry
    return found


def check(files: Iterable[str], register: Mapping[str, Any], root: Path = ROOT) -> list[str]:
    """One line per problem; empty when the register covers every content file of ``files``. A file a
    delegated register lists is covered by its entry there."""
    files = list(files)
    permitted = set(register["permitted"])
    problems: list[str] = []
    entries: dict[str, Mapping[str, Any]] = {}
    for entry in register["entries"]:
        path = entry["path"]
        if path in entries:
            problems.append(f"{path}: registered twice")
        entries[path] = entry
        if path not in files:
            problems.append(f"{path}: registered, but no such file is tracked; remove its entry")
        if entry.get("licence") not in permitted:
            problems.append(
                f"{path}: licence {entry.get('licence')!r} is not permitted ({', '.join(sorted(permitted))})"
            )
        if entry.get("commercial_use") is not True:
            problems.append(f"{path}: its licence must allow commercial use to ship")
        if not str(entry.get("source") or "").strip():
            problems.append(f"{path}: names no source")
    delegated = _delegated(register, root)
    for path in content_files(files, register):
        if path in delegated:
            entry = delegated[path]
            if entry.get("licence") not in permitted:
                problems.append(f"{path}: licence {entry.get('licence')!r} in its delegated register is not permitted")
            if not str(entry.get("source") or "").strip():
                problems.append(f"{path}: its delegated register names no source")
            continue
        if path not in entries:
            problems.append(
                f"{path}: content with no entry in {REGISTER.relative_to(ROOT)}; add its licence, source and "
                "commercial use"
            )
    for source in register["sources"]:
        if source.get("shipped") and (
            source.get("licence") not in permitted or source.get("commercial_use") is not True
        ):
            problems.append(f"source {source['name']}: shipped under {source.get('licence')!r}, which is not permitted")
    return problems


def main(argv: list[str] | None = None) -> int:
    register = json.loads(REGISTER.read_text(encoding="utf-8"))
    problems = check(tracked(), register)
    for line in problems:
        print(line, file=sys.stderr)
    if problems:
        print(f"{len(problems)} problem(s) in the red team content register", file=sys.stderr)
        return 1
    print(f"The red team content register covers {len(register['entries'])} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
