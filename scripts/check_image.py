"""Require the local edition's image to hold the SDK version and the Evals UI of the release's wheel."""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import subprocess
import time
import urllib.request
import zipfile
from collections.abc import Mapping
from email.parser import BytesParser
from pathlib import Path

from check_release import STAMP, ui_digest, wheel_ui

PROBE = """
import base64, json, importlib.metadata
from martian.evals.local import packaged_ui
ui = packaged_ui()
files = {p.relative_to(ui).as_posix(): base64.b64encode(p.read_bytes()).decode() for p in ui.rglob("*") if p.is_file()}
print(json.dumps({"version": importlib.metadata.version("martian-sdk"), "files": files}))
"""
"""Run by the image's Python: the installed SDK version and every file of the UI ``martian ui`` serves."""


def identity(version: str, files: Mapping[str, bytes], where: str) -> dict[str, object]:
    """What the version check compares: the SDK version and the UI's stamp and digest."""
    if STAMP not in files:
        raise ValueError(f"{where} holds no built Evals UI: {STAMP} is missing")
    stamp = json.loads(files[STAMP])
    return {
        "sdk_version": version,
        "ui_version": stamp.get("version"),
        "ui_commit": stamp.get("commit"),
        "ui_dirty": stamp.get("dirty"),
        "ui_digest": ui_digest(files),
    }


def wheel_identity(directory: Path) -> dict[str, object]:
    """The identity of the one wheel in ``directory``."""
    wheels = list(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"expected exactly one wheel in {directory}, found {len(wheels)}")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        metadata = BytesParser().parsebytes(archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))))
        return identity(metadata["Version"], wheel_ui(archive), "the wheel")


def probe_identity(output: bytes) -> dict[str, object]:
    """The identity in what :data:`PROBE` printed."""
    probe = json.loads(output)
    files = {path: base64.b64decode(data) for path, data in probe["files"].items()}
    return identity(probe["version"], files, "the image")


def compare(wheel: Mapping[str, object], image: Mapping[str, object]) -> None:
    """Require the image to hold what the wheel holds."""
    differ = [
        f"{key} is {image.get(key)!r} in the image, {value!r} in the wheel"
        for key, value in wheel.items()
        if image.get(key) != value
    ]
    if differ:
        raise ValueError("the image does not hold the wheel's SDK and Evals UI: " + "; ".join(differ))


def docker(*args: str) -> bytes:
    return subprocess.run(["docker", *args], check=True, stdout=subprocess.PIPE).stdout


def image_identity(image: str) -> dict[str, object]:
    """Run :data:`PROBE` in ``image``, with no network, as the image's user."""
    return probe_identity(
        docker("run", "--rm", "--network", "none", "--entrypoint", "python", image, "-I", "-c", PROBE)
    )


def serve(image: str, wheel: Mapping[str, object]) -> None:
    """Start ``image`` as it ships, with its port on 127.0.0.1, and require ``/`` to be the Evals UI
    and ``/ui-version.json`` the stamp of the wheel's UI."""
    name = f"martian-image-check-{secrets.token_hex(4)}"
    docker("run", "--detach", "--name", name, "--publish", "127.0.0.1::7600", image)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        base = "http://" + docker("port", name, "7600/tcp").decode().split()[0]
        deadline = time.monotonic() + 60
        while True:
            try:
                page = opener.open(base + "/", timeout=5).read().decode()
                break
            except OSError:
                running = docker("inspect", "--format", "{{.State.Running}}", name).strip() == b"true"
                if not running or time.monotonic() > deadline:
                    logs = subprocess.run(["docker", "logs", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    raise ValueError(f"the image did not serve the Evals UI: {logs.stdout.decode()}") from None
                time.sleep(0.5)
        if "<title>Martian Evals</title>" not in page:
            raise ValueError("the image did not answer / with the Evals UI")
        served = json.loads(opener.open(f"{base}/{STAMP}", timeout=5).read())
        if [served.get(key) for key in ("version", "commit", "dirty")] != [
            wheel[key] for key in ("ui_version", "ui_commit", "ui_dirty")
        ]:
            raise ValueError(f"the image serves the {STAMP} of another Evals UI than the wheel's: {served}")
    finally:
        subprocess.run(["docker", "rm", "--force", "--volumes", name], check=True, stdout=subprocess.DEVNULL)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="the image to check, as built by the release")
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--manifest", type=Path, default=Path("image-release.json"))
    args = parser.parse_args()
    image_id = docker("image", "inspect", "--format", "{{.Id}}", args.image).decode().strip()
    wheel = wheel_identity(args.dist_dir)
    compare(wheel, image_identity(image_id))
    serve(image_id, wheel)
    result = {
        "image": args.image,
        "id": image_id,
        "sdk_version": wheel["sdk_version"],
        "evals_ui": {"version": wheel["ui_version"], "commit": wheel["ui_commit"], "digest": wheel["ui_digest"]},
    }
    args.manifest.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
