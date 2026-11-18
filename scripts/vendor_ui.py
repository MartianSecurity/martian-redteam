"""Replace the Evals UI the wheel ships, src/martian/ui/, with a build of the frontend's Evals app, and pin it.

The build is the ``dist-evals/`` folder that ``npm run build:evals`` writes in MartianSecurity/MartianFrontend,
from a clean checkout of the commit to ship: the Evals app's shell with the Evals and the Red Team pages. Its
``ui-version.json`` names that commit and, under ``apps``, the version of each app it holds; a build without
the Red Team app is refused. This script copies
the build into ``src/martian/ui/`` and writes its version, commit and digest into the ``[tool.martian.ui]``
table of ``pyproject.toml``, which ``check_release.py`` checks the distributions against.

``--check`` writes nothing: it compares a build with the vendored copy and the pin, so a reviewer who
rebuilds the pinned commit can confirm the vendored files are that build.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from check_release import STAMP, check_ui, missing_apps, ui_digest, ui_pin

ROOT = Path(__file__).resolve().parents[1]

TABLE = re.compile(r"^\[tool\.martian\.ui\]\n(?:\w+ = .*\n)*", re.MULTILINE)
"""The pin table in ``pyproject.toml``: its header and its ``key = value`` lines."""


def read_build(folder: Path) -> tuple[dict[str, str], dict[str, bytes]]:
    """A built Evals UI: its stamp, and its files by path. Only a build from a clean tree is taken."""
    files = {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    if "index.html" not in files or STAMP not in files:
        raise ValueError(f"{folder} is not a build of the Evals app: index.html or {STAMP} is missing")
    stamp = json.loads(files[STAMP])
    if stamp.get("name") != "@martian/evals" or not re.fullmatch(r"[0-9a-f]{40}", str(stamp.get("commit"))):
        raise ValueError(f"{folder / STAMP} does not name the Evals app and the commit it was built from")
    if stamp.get("dirty") is not False:
        raise ValueError(
            f"{folder} was built from a frontend tree with uncommitted changes; "
            "build it from a clean checkout of the commit to ship"
        )
    if missing_apps(stamp):
        raise ValueError(
            f"{folder} does not hold {', '.join(missing_apps(stamp))}; build a frontend commit whose "
            "build:evals bundles the Evals and the Red Team apps"
        )
    return stamp, files


def vendor(build: Path, ui: Path, pyproject: Path) -> dict[str, str]:
    """Copy ``build`` into ``ui`` in place of what is there, and pin it in ``pyproject``."""
    stamp, files = read_build(build)
    pin = {"version": stamp["version"], "commit": stamp["commit"], "digest": ui_digest(files)}
    table = "[tool.martian.ui]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in pin.items())
    text, count = TABLE.subn(lambda _: table, pyproject.read_text(encoding="utf-8"))
    if count != 1:
        raise ValueError(f"{pyproject} has no [tool.martian.ui] table to write the pin into")
    if ui.exists():
        shutil.rmtree(ui)
    for path, data in files.items():
        (ui / path).parent.mkdir(parents=True, exist_ok=True)
        (ui / path).write_bytes(data)
    pyproject.write_text(text, encoding="utf-8")
    return pin


def check(build: Path, ui: Path, pyproject: Path) -> dict[str, str]:
    """Require ``build`` to be the UI vendored in ``ui`` and pinned in ``pyproject``. Writes nothing."""
    _, files = read_build(build)
    vendored = {path.relative_to(ui).as_posix(): path.read_bytes() for path in ui.rglob("*") if path.is_file()}
    differ = sorted(path for path in files.keys() | vendored.keys() if files.get(path) != vendored.get(path))
    if differ:
        raise ValueError(f"{ui} differs from {build}: {', '.join(differ)}")
    pin = ui_pin(pyproject.read_bytes())
    check_ui(vendored, pin, str(ui))
    return pin


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("build", type=Path, help="The dist-evals folder of a frontend build.")
    parser.add_argument("--check", action="store_true", help="Compare the build with the vendored UI; write nothing.")
    args = parser.parse_args()
    ui, pyproject = ROOT / "src/martian/ui", ROOT / "pyproject.toml"
    pin = (check if args.check else vendor)(args.build.resolve(), ui, pyproject)
    print(json.dumps({"evals_ui": pin, "checked" if args.check else "vendored": str(ui.relative_to(ROOT))}))


if __name__ == "__main__":
    main()
