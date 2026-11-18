"""The Evals UI the wheel ships: the build vendored in src/martian/ui/, its pin and the release check."""

import io
import json
import sys
import tarfile
import warnings
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_release  # noqa: E402
import vendor_ui  # noqa: E402

COMMIT = "0123456789abcdef0123456789abcdef01234567"

PYPROJECT = """[project]
name = "martian-sdk"

# The pin.
[tool.martian.ui]
version = "0.0.0"
commit = "none"
digest = "none"

[tool.pytest.ini_options]
testpaths = ["tests"]
"""


APPS = {"@martian/evals": "0.1.0", "@martian/redteam": "0.1.0"}


def write_build(folder: Path, *, commit: str = COMMIT, dirty: bool = False, apps: dict | None = APPS) -> Path:
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    (folder / "assets" / "index-abc.js").write_text("export {};", encoding="utf-8")
    stamp = {"name": "@martian/evals", "version": "0.1.0", "commit": commit, "dirty": dirty}
    if apps is not None:
        stamp["apps"] = apps
    (folder / "ui-version.json").write_text(json.dumps(stamp), encoding="utf-8")
    return folder


def files_in(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


@pytest.fixture
def repo(tmp_path) -> tuple[Path, Path]:
    """A ``src/martian/ui`` holding an older build, and a ``pyproject.toml`` with a pin table."""
    ui = tmp_path / "src" / "martian" / "ui"
    ui.mkdir(parents=True)
    (ui / "stale.js").write_text("old", encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT, encoding="utf-8")
    return ui, pyproject


def wheel(path: Path, files: dict[str, bytes] | list[tuple[str, bytes]]) -> zipfile.ZipFile:
    with zipfile.ZipFile(path, "w") as archive, warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about a repeated name
        for name, data in files.items() if isinstance(files, dict) else files:  # a list may repeat a name
            archive.writestr(name, data)
    return zipfile.ZipFile(path)


def sdist(path: Path, files: dict[str, bytes]) -> tarfile.TarFile:
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return tarfile.open(path)


def test_the_ui_in_the_package_is_the_build_pyproject_pins():
    ui = ROOT / "src" / "martian" / "ui"
    pin = check_release.ui_pin((ROOT / "pyproject.toml").read_bytes())
    check_release.check_ui(files_in(ui), pin, "src/martian/ui")
    assert json.loads((ui / "ui-version.json").read_text(encoding="utf-8"))["dirty"] is False
    assert "<title>Martian Evals</title>" in (ui / "index.html").read_text(encoding="utf-8")


def test_vendoring_replaces_the_ui_and_pins_the_build(tmp_path, repo):
    ui, pyproject = repo
    build = write_build(tmp_path / "dist-evals")
    pin = vendor_ui.vendor(build, ui, pyproject)
    assert files_in(ui) == files_in(build)
    assert pin == {"version": "0.1.0", "commit": COMMIT, "digest": check_release.ui_digest(files_in(build))}
    assert check_release.ui_pin(pyproject.read_bytes()) == pin
    text = pyproject.read_text(encoding="utf-8")
    assert text.startswith('[project]\nname = "martian-sdk"\n\n# The pin.\n[tool.martian.ui]\n')
    assert text.endswith('\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    assert vendor_ui.check(build, ui, pyproject) == pin


def test_a_build_from_a_dirty_tree_is_refused(tmp_path, repo):
    ui, pyproject = repo
    build = write_build(tmp_path / "dist-evals", dirty=True)
    with pytest.raises(ValueError, match="uncommitted changes"):
        vendor_ui.vendor(build, ui, pyproject)
    assert files_in(ui) == {"stale.js": b"old"}
    assert pyproject.read_text(encoding="utf-8") == PYPROJECT


@pytest.mark.parametrize("apps", [None, {"@martian/evals": "0.1.0"}], ids=["no apps", "evals only"])
def test_a_build_without_the_red_team_app_is_refused(tmp_path, repo, apps):
    """The local edition serves the red team routes, so the UI it ships holds the Red Team pages
    (MartianSecurity/martian-notes#290)."""
    ui, pyproject = repo
    build = write_build(tmp_path / "dist-evals", apps=apps)
    with pytest.raises(ValueError, match="does not hold (@martian/evals, )?@martian/redteam"):
        vendor_ui.vendor(build, ui, pyproject)
    assert files_in(ui) == {"stale.js": b"old"}
    files = files_in(build)
    pin = {"version": "0.1.0", "commit": COMMIT, "digest": check_release.ui_digest(files)}
    with pytest.raises(ValueError, match="does not hold (@martian/evals, )?@martian/redteam"):
        check_release.check_ui(files, pin, "the wheel")


def test_the_packaged_ui_holds_the_evals_and_the_red_team_apps():
    stamp = json.loads((ROOT / "src" / "martian" / "ui" / "ui-version.json").read_text(encoding="utf-8"))
    assert set(stamp["apps"]) == {"@martian/evals", "@martian/redteam"}


def test_a_folder_that_is_not_a_build_is_refused(tmp_path, repo):
    ui, pyproject = repo
    build = write_build(tmp_path / "dist-evals", commit="main")
    with pytest.raises(ValueError, match="does not name the Evals app and the commit"):
        vendor_ui.vendor(build, ui, pyproject)
    (build / "ui-version.json").unlink()
    with pytest.raises(ValueError, match="ui-version.json is missing"):
        vendor_ui.vendor(build, ui, pyproject)


def test_check_names_the_files_that_differ_from_a_rebuild(tmp_path, repo):
    ui, pyproject = repo
    build = write_build(tmp_path / "dist-evals")
    vendor_ui.vendor(build, ui, pyproject)
    (ui / "assets" / "index-abc.js").write_text("export {}; // edited", encoding="utf-8")
    (ui / "extra.js").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="differs from .*: assets/index-abc.js, extra.js"):
        vendor_ui.check(build, ui, pyproject)


def pinned(tmp_path) -> tuple[dict[str, bytes], dict[str, str]]:
    files = files_in(write_build(tmp_path / "dist-evals"))
    return files, {"version": "0.1.0", "commit": COMMIT, "digest": check_release.ui_digest(files)}


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda files: files.update({"assets/index-abc.js": b"export {}; // edited"}), "files in the wheel differ"),
        (lambda files: files.update({"assets/extra.js": b""}), "files in the wheel differ"),
        (lambda files: files.pop("assets/index-abc.js"), "files in the wheel differ"),
        (lambda files: files.pop("index.html"), "holds no built Evals UI"),
        (
            lambda files: files.update(
                {"ui-version.json": json.dumps({"version": "0.1.0", "commit": COMMIT}).encode()}
            ),
            "built from a frontend tree with uncommitted changes",
        ),
        (
            lambda files: files.update(
                {
                    "ui-version.json": json.dumps(
                        {"version": "0.1.0", "commit": "f" * 40, "dirty": False, "apps": APPS}
                    ).encode()
                }
            ),
            f"is 0.1.0 at {'f' * 40}, but pyproject.toml pins 0.1.0 at {COMMIT}",
        ),
    ],
    ids=["edited file", "extra file", "missing file", "no index", "dirty build", "other commit"],
)
def test_the_release_check_refuses_a_ui_other_than_the_pinned_build(tmp_path, change, error):
    files, pin = pinned(tmp_path)
    check_release.check_ui(files, pin, "the wheel")
    change(files)
    with pytest.raises(ValueError, match=error):
        check_release.check_ui(files, pin, "the wheel")


def test_the_release_check_reads_the_ui_from_both_distributions(tmp_path):
    files, pin = pinned(tmp_path)
    table = "[tool.martian.ui]\n" + "".join(f'{key} = "{value}"\n' for key, value in pin.items())
    packaged = wheel(
        tmp_path / "x.whl", {"martian/__init__.py": b""} | {f"martian/ui/{p}": d for p, d in files.items()}
    )
    assert check_release.wheel_ui(packaged) == files
    source = sdist(
        tmp_path / "x.tar.gz",
        {"martian_sdk-1/pyproject.toml": table.encode()}
        | {f"martian_sdk-1/src/martian/ui/{p}": d for p, d in files.items()},
    )
    source_ui, pyproject = check_release.sdist_ui(source)
    assert source_ui == files
    assert check_release.ui_pin(pyproject) == pin


def test_the_release_check_refuses_a_ui_packaged_twice_or_not_at_all(tmp_path):
    files, _ = pinned(tmp_path)
    ui = {f"martian/ui/{path}": data for path, data in files.items()}
    twice = wheel(tmp_path / "twice.whl", ui | {f"ui/{path}": data for path, data in files.items()})
    with pytest.raises(ValueError, match="the wheel holds 2 built Evals UIs; it must hold one"):
        check_release.wheel_ui(twice)
    duplicate = wheel(tmp_path / "duplicate.whl", [*ui.items(), ("martian/ui/index.html", files["index.html"])])
    with pytest.raises(ValueError, match="the wheel holds a file more than once"):
        check_release.wheel_ui(duplicate)
    missing = wheel(tmp_path / "missing.whl", {"martian/__init__.py": b""})
    with pytest.raises(ValueError, match="the wheel holds 0 built Evals UIs"):
        check_release.wheel_ui(missing)
    source = {"martian_sdk-1/pyproject.toml": b""} | {f"martian_sdk-1/src/martian/ui/{p}": d for p, d in files.items()}
    copied = sdist(tmp_path / "twice.tar.gz", source | {"martian_sdk-1/ui/ui-version.json": files["ui-version.json"]})
    with pytest.raises(ValueError, match="the source distribution holds 2 built Evals UIs"):
        check_release.sdist_ui(copied)
