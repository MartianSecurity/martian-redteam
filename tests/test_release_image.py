"""The release's image: built from the checked wheel, checked to hold its SDK and Evals UI, then pushed."""

import base64
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

import martian

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_image  # noqa: E402
import check_release  # noqa: E402

COMMIT = "0123456789abcdef0123456789abcdef01234567"
STAMP = {"name": "@martian/evals", "version": "0.1.0", "commit": COMMIT, "dirty": False}
UI = {"index.html": b"<title>Martian Evals</title>", "assets/index-abc.js": b"export {};"}


def build(stamp: dict[str, object] = STAMP, **changes: bytes) -> dict[str, bytes]:
    return UI | {"ui-version.json": json.dumps(stamp).encode()} | changes


def write_wheel(folder: Path, version: str, ui: dict[str, bytes]) -> Path:
    folder.mkdir()
    with zipfile.ZipFile(folder / f"martian_sdk-{version}-py3-none-any.whl", "w") as archive:
        archive.writestr("martian/__init__.py", b"")
        archive.writestr(f"martian_sdk-{version}.dist-info/METADATA", f"Name: martian-sdk\nVersion: {version}\n")
        for path, data in ui.items():
            archive.writestr(f"martian/ui/{path}", data)
    return folder


def probed(version: str, ui: dict[str, bytes]) -> bytes:
    """What the image's Python prints for an install of ``version`` with ``ui``."""
    files = {path: base64.b64encode(data).decode() for path, data in ui.items()}
    return json.dumps({"version": version, "files": files}).encode()


def test_the_probe_reads_the_ui_martian_ui_serves():
    output = subprocess.run([sys.executable, "-I", "-c", check_image.PROBE], check=True, capture_output=True).stdout
    found = check_image.probe_identity(output)
    pin = check_release.ui_pin((ROOT / "pyproject.toml").read_bytes())
    assert found == {
        "sdk_version": martian.__version__,
        "ui_version": pin["version"],
        "ui_commit": pin["commit"],
        "ui_dirty": False,
        "ui_digest": pin["digest"],
    }


def test_the_check_passes_an_image_that_holds_the_wheels_sdk_and_ui(tmp_path):
    wheel = check_image.wheel_identity(write_wheel(tmp_path / "dist", "0.1.4", build()))
    assert wheel["sdk_version"] == "0.1.4"
    assert wheel["ui_digest"] == check_release.ui_digest(build())
    check_image.compare(wheel, check_image.probe_identity(probed("0.1.4", build())))


@pytest.mark.parametrize(
    ("version", "ui", "differs"),
    [
        ("0.1.4", build(**{"assets/index-abc.js": b"export {}; // edited"}), "ui_digest"),
        ("0.1.4", build(**{"assets/extra.js": b""}), "ui_digest"),
        ("0.1.4", build(STAMP | {"version": "0.2.0"}), "ui_version"),
        ("0.1.4", build(STAMP | {"commit": "f" * 40}), "ui_commit"),
        ("0.1.4", build(STAMP | {"dirty": True}), "ui_dirty"),
        ("0.1.3", build(), "sdk_version"),
    ],
    ids=["edited file", "extra file", "other ui version", "other commit", "dirty build", "other sdk"],
)
def test_the_check_refuses_an_image_that_holds_another_sdk_or_ui(tmp_path, version, ui, differs):
    wheel = check_image.wheel_identity(write_wheel(tmp_path / "dist", "0.1.4", build()))
    with pytest.raises(ValueError, match=f"does not hold the wheel's SDK and Evals UI: {differs} is "):
        check_image.compare(wheel, check_image.probe_identity(probed(version, ui)))


def test_the_check_refuses_an_image_without_a_ui(tmp_path):
    with pytest.raises(ValueError, match="the image holds no built Evals UI"):
        check_image.probe_identity(probed("0.1.4", UI))


def steps(job: dict) -> list[str]:
    """Each step of ``job`` as one string: its name, action and script."""
    return [json.dumps(step) for step in job["steps"]]


def index(lines: list[str], text: str) -> int:
    found = [number for number, line in enumerate(lines) if text in line]
    assert len(found) == 1, (text, found)
    return found[0]


def test_the_release_builds_the_image_from_the_checked_wheel_and_checks_it():
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]
    lines = steps(jobs["build"])
    order = [
        index(lines, "uv build"),
        index(lines, "scripts/check_release.py --install"),
        index(lines, "docker build"),
        index(lines, "scripts/check_image.py"),
        index(lines, "docker save"),
        index(lines, '"name": "evals-image"'),
    ]
    assert order == sorted(order), order


def test_the_image_is_published_only_with_the_package_and_without_a_rebuild():
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]
    publish = jobs["publish-image"]
    assert publish["if"] == jobs["publish"]["if"]
    assert publish["needs"] == ["build", "publish"]
    assert publish["permissions"] == {"packages": "write"}
    assert publish["environment"]["name"] == "ghcr"
    script = "\n".join(steps(publish))
    assert "docker push" in script and "docker load" in script
    assert "docker build" not in script and "uv build" not in script
    assert jobs["build"]["env"]["IMAGE"] == publish["env"]["IMAGE"] == "ghcr.io/morninghq/martian-evals"
