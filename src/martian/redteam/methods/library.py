"""The prompt files of the attack methods, their licence register and their revision.

Every prompt a method sends to the attacker model, and every fixed template, is a file under
``methods/prompts/``, written by Martian Security. ``prompts/register.json`` lists each file with
its licence, who wrote it and what it was modelled on; :func:`register_problems` finds a file the
register does not list, an entry with no file, and a licence the release's licence policy does not
allow. :data:`METHODS_REVISION` is a digest of every file, so a change to one is a new revision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any

FOLDER = Path(__file__).resolve().parent / "prompts"
REGISTER = FOLDER / "register.json"


def prompt_files() -> list[str]:
    """Every prompt file, as a path relative to the folder, the register itself left out."""
    return sorted(
        path.relative_to(FOLDER).as_posix()
        for path in FOLDER.rglob("*")
        if path.is_file() and path != REGISTER and "__pycache__" not in path.parts
    )


@cache
def prompt(name: str) -> str:
    """The text of prompt file ``name``, such as ``rewrites/authority.md``, without its last newline."""
    return (FOLDER / name).read_text(encoding="utf-8").rstrip("\n")


def register() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(REGISTER.read_text(encoding="utf-8"))
    return loaded


def register_problems(allowed: Iterable[str]) -> list[str]:
    """What is wrong with the register: files it does not list, entries without a file, and entries
    without a source or with a licence not in ``allowed``."""
    files, listed, permitted = set(prompt_files()), register()["files"], set(allowed)
    problems = [f"{name}: not in the licence register" for name in sorted(files - set(listed))]
    problems += [f"{name}: listed in the register, but there is no such file" for name in sorted(set(listed) - files)]
    for name, entry in sorted(listed.items()):
        if entry.get("licence") not in permitted:
            problems.append(f"{name}: licence {entry.get('licence')!r} is not allowed")
        if not str(entry.get("source", "")).strip():
            problems.append(f"{name}: the register names no source")
    return problems


def _revision() -> str:
    digest = hashlib.sha256()
    for name in prompt_files():
        digest.update(name.encode() + b"\0" + (FOLDER / name).read_bytes() + b"\0")
    return f"redteam-methods+{digest.hexdigest()[:12]}"


METHODS_REVISION = _revision()
"""A digest of every prompt file: a change to one is a new revision."""

__all__ = ["FOLDER", "METHODS_REVISION", "REGISTER", "prompt", "prompt_files", "register", "register_problems"]
