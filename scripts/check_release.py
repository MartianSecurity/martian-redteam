"""Qualify wheel/sdist and the single-key public imports; optionally clean-install both."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path


def validate(directory: Path) -> list[Path]:
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
    return [wheels[0], sources[0]]


def install(artifacts: list[Path]) -> None:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--manifest", type=Path, default=Path("release-checksums.json"))
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    artifacts = validate(args.dist_dir)
    if args.install:
        install(artifacts)
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts}
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"artifacts": manifest, "clean_installs": args.install}))


if __name__ == "__main__":
    main()
