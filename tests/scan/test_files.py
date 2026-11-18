"""Which files `martian scan` sends and how it packs them (MartianSecurity/martian-notes#469)."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from martian.scan.files import DEFAULT_INCLUDE, SelectionError, SymlinkOutside, collect, pack
from tests.scan.fake_backend import content_digest

FIXTURE = Path(__file__).resolve().parents[2] / "examples" / "scan" / "fixture"
GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="needs git")


def _write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


AGENT_FILES = {
    "AGENTS.md": "# Instructions\n",
    ".mcp.json": "{}\n",
    ".claude/settings.json": "{}\n",
    ".claude/agents/reviewer.md": "---\nname: reviewer\n---\n",
    ".github/copilot-instructions.md": "Be brief.\n",
    "skills/notes/SKILL.md": "---\nname: notes\n---\n",
    "skills/notes/scripts/run.py": "print(1)\n",
    "skills/notes/data/table.csv": "a,b\n",
    "plugins/tidy/.claude-plugin/plugin.json": "{}\n",
    "plugins/tidy/commands/tidy.md": "Tidy up.\n",
    "packages/web/CLAUDE.md": "Use pnpm.\n",
    "src/app.py": "print('app')\n",
    "README.md": "# Repo\n",
    "docs/guide.md": "Guide\n",
}
SENT = sorted(path for path in AGENT_FILES if path not in ("src/app.py", "README.md", "docs/guide.md"))


def test_the_fixture_repository_sends_its_agent_files_and_not_its_code():
    selection = collect(FIXTURE)
    assert selection.git
    assert selection.files == (".mcp.json", "AGENTS.md", "skills/release-notes/SKILL.md",
                               "skills/release-notes/scripts/collect.sh")  # fmt: skip


def test_the_default_paths_send_agent_files_and_whole_skill_and_plugin_folders(tmp_path):
    _write(tmp_path, AGENT_FILES)
    selection = collect(tmp_path)
    assert not selection.git
    assert list(selection.files) == SENT


@needs_git
def test_files_gitignore_ignores_are_not_sent_and_untracked_ones_are(tmp_path):
    _write(tmp_path, {**AGENT_FILES, ".gitignore": "vendor/\n*.local.md\n", "vendor/x/AGENTS.md": "ignored\n",
                      "CLAUDE.local.md": "ignored\n"})  # fmt: skip
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "AGENTS.md")
    selection = collect(tmp_path)
    assert selection.git
    assert list(selection.files) == SENT


@needs_git
def test_a_subfolder_of_a_work_tree_is_scanned_relative_to_itself(tmp_path):
    _write(tmp_path, AGENT_FILES)
    _git(tmp_path, "init", "-q")
    assert collect(tmp_path / "skills").files == ("notes/SKILL.md", "notes/data/table.csv", "notes/scripts/run.py")


@needs_git
def test_a_folders_git_config_cannot_start_a_program(tmp_path):
    _write(tmp_path, AGENT_FILES)
    _git(tmp_path, "init", "-q")
    marker = tmp_path.parent / f"{tmp_path.name}-fsmonitor-ran"
    hook = tmp_path.parent / f"{tmp_path.name}-fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf '/\\0'\n")
    hook.chmod(0o755)
    _git(tmp_path, "config", "core.fsmonitor", str(hook))
    _git(tmp_path, "add", ".")
    marker.unlink(missing_ok=True)
    collect(tmp_path)
    assert not marker.exists()


def test_include_patterns_replace_the_defaults(tmp_path):
    _write(tmp_path, AGENT_FILES)
    assert collect(tmp_path, ["src/"]).files == ("src/app.py",)
    assert collect(tmp_path, ["*.md"]).files == tuple(sorted(p for p in AGENT_FILES if p.endswith(".md")))
    assert collect(tmp_path, ["skills/*/SKILL.md", "docs/**"]).files == ("docs/guide.md", "skills/notes/SKILL.md")
    with pytest.raises(SelectionError, match="leaves the folder"):
        collect(tmp_path, ["../secrets/**"])


def test_a_symlink_out_of_the_folder_refuses_the_scan(tmp_path):
    root, outside = tmp_path / "repo", tmp_path / "outside"
    _write(root, AGENT_FILES)
    _write(outside, {"AGENTS.md": "elsewhere\n", "settings.json": "{}\n"})
    (root / "packages" / "api").mkdir(parents=True)
    (root / "packages" / "api" / "AGENTS.md").symlink_to(outside / "AGENTS.md")
    with pytest.raises(SymlinkOutside) as refused:
        collect(root)
    assert refused.value.paths == ["packages/api/AGENTS.md"]


def test_a_linked_folder_out_of_the_folder_refuses_the_scan(tmp_path):
    root, outside = tmp_path / "repo", tmp_path / "home-claude"
    _write(root, {"AGENTS.md": "x\n"})
    _write(outside, {"settings.json": "{}\n"})
    (root / ".claude").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SymlinkOutside) as refused:
        collect(root)
    assert refused.value.paths == [".claude"]
    # A linked folder whose own name no pattern matches, but an AGENTS.md under it would.
    (root / ".claude").unlink()
    (root / "vendor").mkdir()
    (root / "vendor" / "shared").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SymlinkOutside) as refused:
        collect(root)
    assert refused.value.paths == ["vendor/shared"]
    # Explicit patterns that cannot reach below it leave it alone.
    assert collect(root, ["/AGENTS.md"]).files == ("AGENTS.md",)


def test_a_link_inside_the_folder_is_not_sent_and_is_listed(tmp_path):
    _write(tmp_path, AGENT_FILES)
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "AGENTS.md")
    selection = collect(tmp_path)
    assert "CLAUDE.md" not in selection.files
    assert [(item.path, item.reason) for item in selection.skipped] == [("CLAUDE.md", "symlink")]


def test_a_link_no_pattern_reaches_is_ignored(tmp_path):
    _write(tmp_path, AGENT_FILES)
    (tmp_path / "docs" / "hosts.txt").symlink_to("/etc/hosts")
    assert list(collect(tmp_path).files) == SENT


def test_the_archive_is_deterministic_and_carries_the_backends_content_digest(tmp_path):
    _write(tmp_path, AGENT_FILES)
    first, second = pack(collect(tmp_path)), pack(collect(tmp_path))
    assert first.archive == second.archive
    with zipfile.ZipFile(io.BytesIO(first.archive)) as archive:
        infos = archive.infolist()
        files = {info.filename: archive.read(info) for info in infos}
    assert [info.filename for info in infos] == SENT
    assert all(info.external_attr >> 16 == 0o100644 and not info.is_dir() for info in infos)
    assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in infos)
    assert first.content_digest == content_digest(files)
    assert first.files == len(SENT)


def test_nothing_to_send_is_an_error(tmp_path):
    _write(tmp_path, {"src/app.py": "x\n"})
    with pytest.raises(SelectionError, match="no file to scan"):
        pack(collect(tmp_path))
    with pytest.raises(SelectionError, match="does not exist"):
        collect(tmp_path / "missing")


def test_the_default_paths_are_paths_only():
    assert all(isinstance(pattern, str) and "\n" not in pattern for pattern in DEFAULT_INCLUDE)
