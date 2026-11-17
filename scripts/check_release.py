"""Qualify wheel/sdist, the packaged Evals UI and the single-key public imports; optionally clean-install both."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tarfile
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from collections.abc import Mapping
from email.parser import BytesParser
from pathlib import Path

UI = "martian/ui/"
"""Where the wheel keeps the built Evals UI; the sdist keeps it in ``src/martian/ui/``."""

STAMP = "ui-version.json"
"""The build's identity, written by the frontend's ``npm run build:evals``."""

APPS = ("@martian/evals", "@martian/redteam")
"""The apps the build must hold, which its stamp names under ``apps``: the local edition serves the
evals and the red team routes, and the UI shows both."""


def missing_apps(stamp: Mapping[str, object]) -> list[str]:
    """The apps of :data:`APPS` a build's stamp does not name."""
    apps = stamp.get("apps")
    return [name for name in APPS if not isinstance(apps, dict) or not isinstance(apps.get(name), str)]


def ui_digest(files: Mapping[str, bytes]) -> str:
    """The digest of a built UI: SHA-256 over one ``<sha256>  <path>`` line per file, sorted by path."""
    listing = "".join(f"{hashlib.sha256(files[path]).hexdigest()}  {path}\n" for path in sorted(files))
    return "sha256:" + hashlib.sha256(listing.encode()).hexdigest()


def ui_pin(pyproject: bytes) -> dict[str, str]:
    """The ``[tool.martian.ui]`` table: the version, frontend commit and digest of the UI to ship."""
    try:
        table = tomllib.loads(pyproject.decode())["tool"]["martian"]["ui"]
        return {key: str(table[key]) for key in ("version", "commit", "digest")}
    except KeyError as error:
        raise ValueError(f"pyproject.toml does not pin the Evals UI: [tool.martian.ui] lacks {error}") from None


def check_ui(files: Mapping[str, bytes], pin: Mapping[str, str], where: str) -> None:
    """Require ``files``, a built UI by path, to be the clean build that ``pin`` names, byte for byte."""
    if "index.html" not in files or STAMP not in files:
        raise ValueError(f"{where} holds no built Evals UI: index.html or {STAMP} is missing")
    stamp = json.loads(files[STAMP])
    if stamp.get("dirty") is not False:
        raise ValueError(f"the Evals UI in {where} was built from a frontend tree with uncommitted changes")
    if missing_apps(stamp):
        raise ValueError(f"the Evals UI in {where} does not hold {', '.join(missing_apps(stamp))}")
    if (stamp.get("version"), stamp.get("commit")) != (pin["version"], pin["commit"]):
        raise ValueError(
            f"the Evals UI in {where} is {stamp.get('version')} at {stamp.get('commit')}, "
            f"but pyproject.toml pins {pin['version']} at {pin['commit']}"
        )
    if ui_digest(files) != pin["digest"]:
        raise ValueError(f"the Evals UI files in {where} differ from the build pyproject.toml pins")


def once(names: list[str], what: str) -> None:
    """Require no file twice in a distribution, and one built Evals UI."""
    if len(names) != len(set(names)):
        raise ValueError(f"{what} holds a file more than once")
    if (count := sum(Path(name).name == STAMP for name in names)) != 1:
        raise ValueError(f"{what} holds {count} built Evals UIs; it must hold one")


def wheel_ui(archive: zipfile.ZipFile) -> dict[str, bytes]:
    """The built UI in a wheel, by path below ``martian/ui/``. It must be there once and only there."""
    names = archive.namelist()
    once(names, "the wheel")
    return {name.removeprefix(UI): archive.read(name) for name in names if name.startswith(UI) and name != UI}


def sdist_ui(archive: tarfile.TarFile) -> tuple[dict[str, bytes], bytes]:
    """The built UI in an sdist, by path below ``src/martian/ui/``, and the sdist's ``pyproject.toml``."""
    members = [member for member in archive.getmembers() if member.isfile()]
    once([member.name for member in members], "the source distribution")
    top = Path(members[0].name).parts[0]

    def read(member: tarfile.TarInfo) -> bytes:
        handle = archive.extractfile(member)
        if handle is None:
            raise ValueError(f"{member.name} cannot be read from the source distribution")
        return handle.read()

    prefix = f"{top}/src/{UI}"
    files = {m.name.removeprefix(prefix): read(m) for m in members if m.name.startswith(prefix)}
    pyproject = next((m for m in members if m.name == f"{top}/pyproject.toml"), None)
    if pyproject is None:
        raise ValueError("the source distribution has no pyproject.toml")
    return files, read(pyproject)


def validate(directory: Path) -> tuple[list[Path], dict[str, str]]:
    """Check both distributions; return them and the Evals UI pin they share."""
    wheels, sources = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("expected exactly one wheel and one source distribution")
    metadata = []
    required = {
        "martian/py.typed",
        "martian/config.py",
        "martian/redteam/client.py",
        "martian_redteam/py.typed",
        "martian/evals/__init__.py",
        "martian/redteam/__init__.py",
    }
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        if not required.issubset(names):
            raise ValueError("wheel is missing a public module or typed marker")
        metadata.append(
            BytesParser().parsebytes(archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))))
        )
        entries = archive.read(next(n for n in names if n.endswith(".dist-info/entry_points.txt"))).decode()
        if "martian = martian.cli:app" not in entries or "martian-redteam =" not in entries:
            raise ValueError("CLI entry points are missing")
        packaged_ui = wheel_ui(archive)
    with tarfile.open(sources[0]) as archive:
        members = archive.getmembers()
        for member in members:
            parts = Path(member.name).parts
            if member.issym() or member.islnk() or ".." in parts or Path(member.name).is_absolute():
                raise ValueError("unsafe source archive member")
            if any(p in (".env", ".venv", "dist", ".git", "pr-reviews") or p.startswith(".env.") for p in parts):
                raise ValueError("private or generated material found in source distribution")
        package_info = next(m for m in members if m.name.endswith("/PKG-INFO"))
        handle = archive.extractfile(package_info)
        if handle is None:
            raise ValueError("source metadata is missing")
        metadata.append(BytesParser().parsebytes(handle.read()))
        source_ui, pyproject = sdist_ui(archive)
    pin = ui_pin(pyproject)
    check_ui(packaged_ui, pin, "the wheel")
    check_ui(source_ui, pin, "the source distribution")
    for info in metadata:
        if info["Name"] != "martian-sdk" or not info["Version"]:
            raise ValueError("incorrect distribution identity")
        if info["Requires-Python"] != ">=3.12" or info["License-Expression"] != "Apache-2.0":
            raise ValueError("incorrect supported Python or license metadata")
        if not {"evals", "mcp"}.issubset(info.get_all("Provides-Extra", [])):
            raise ValueError("dependency extras are missing")
        if "Typing :: Typed" not in info.get_all("Classifier", []):
            raise ValueError("typed package classifier is missing")
        if any(" @ " in r or "file:" in r or "../" in r for r in info.get_all("Requires-Dist", [])):
            raise ValueError("distribution depends on a local or unpublished source")
    if metadata[0]["Version"] != metadata[1]["Version"]:
        raise ValueError("wheel and source versions differ")
    return [wheels[0], sources[0]], pin


def serve_ui(environment: Path, work: Path, pin: Mapping[str, str]) -> None:
    """Start the installed ``martian ui`` with no ``--ui-dir`` and read the packaged UI and the API from it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    home = work / "evals-home"
    command = [str(environment / "bin/martian"), "ui", "--no-open", "--port", str(port), "--data-dir", str(home)]
    # The packaged UI, not one a variable names.
    variables = {key: value for key, value in os.environ.items() if key != "MARTIAN_EVALS_UI_DIR"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with (work / "martian-ui.log").open("w+") as log:
        server = subprocess.Popen(command, cwd=work, env=variables, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            while True:
                try:
                    page = opener.open(base + "/", timeout=5).read().decode()
                    break
                except OSError:
                    if server.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise ValueError(f"martian ui did not serve the packaged UI: {log.read()}") from None
                    time.sleep(0.25)
            if "<title>Martian Evals</title>" not in page:
                raise ValueError("martian ui did not answer / with the Evals UI")
            stamp = json.loads(opener.open(f"{base}/{STAMP}", timeout=5).read())
            if (stamp["version"], stamp["commit"]) != (pin["version"], pin["commit"]):
                raise ValueError("martian ui serves another Evals UI than the one pyproject.toml pins")
            session = json.loads(
                opener.open(urllib.request.Request(base + "/v1/auth/refresh", method="POST"), timeout=5).read()
            )
            bootstrap = json.loads(opener.open(base + "/v1/evals/bootstrap", timeout=5).read())
            if session["session"]["kind"] != "single_user" or bootstrap["principal"]["kind"] != "single_user":
                raise ValueError("martian ui did not answer the session call and the bootstrap for the local user")
            runs = json.loads(opener.open(base + "/v1/redteam/runs", timeout=5).read())
            if runs != {"items": [], "next_cursor": None}:
                raise ValueError("martian ui did not answer the red team runs of an empty data folder")
        finally:
            server.terminate()
            server.wait(10)


def install(artifacts: list[Path], pin: Mapping[str, str]) -> None:
    for artifact in artifacts:
        with tempfile.TemporaryDirectory(prefix="martian-sdk-install-") as work:
            environment = Path(work) / "venv"
            subprocess.run(["uv", "venv", str(environment), "--python", "3.12"], check=True)
            subprocess.run(
                ["uv", "pip", "install", "--python", str(environment), f"{artifact.resolve()}[evals]"], check=True
            )
            subprocess.run(["uv", "pip", "check", "--python", str(environment)], check=True)
            python = environment / "bin/python"
            source = (
                "from martian import MartianConfig, CiContext; "
                "from martian.ci import CiSuite, run; from martian.result import SessionResult; "
                "from martian.evals import assert_gate, assert_redteam; from martian.redteam import Client; "
                "import martian_evals_kit, martian_redteam; "
                "assert Client is not martian_redteam.Client; "
                "assert MartianConfig('https://backend.example', 'install-test').trust_env is False; "
                "import importlib.metadata as m; print(m.version('martian-sdk'))"
            )
            subprocess.run([str(python), "-I", "-c", source], cwd=work, check=True)
            for command in (("martian", "--version"), ("martian", "ci", "--help"), ("martian-redteam", "--version")):
                subprocess.run([str(environment / "bin" / command[0]), *command[1:]], cwd=work, check=True)
            serve_ui(environment, Path(work), pin)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--manifest", type=Path, default=Path("release-checksums.json"))
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    artifacts, pin = validate(args.dist_dir)
    if args.install:
        install(artifacts, pin)
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts}
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"artifacts": manifest, "evals_ui": pin, "clean_installs": args.install}))


if __name__ == "__main__":
    main()
