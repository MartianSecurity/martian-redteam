"""Write the release's bills of materials (CycloneDX 1.6 JSON) and refuse a licence outside the policy.

``wheel`` installs the one wheel in ``dist/`` with the ``evals`` extra and the dependency versions
``uv.lock`` pins, as the ``Dockerfile`` does, in a fresh environment, and lists what is installed: the
SDK, its Python dependencies and the npm packages bundled in the Evals UI the wheel ships (from its
``third-party-licenses.md``). ``image`` runs the same probe in the local edition's image and adds what
the image holds besides: its Python interpreter, the base image's Python packages and its Debian
packages.

Every third-party component's licence is checked against ``scripts/licence-policy.json``. A licence
outside its allowed list, an unknown licence or no licence stops the script with exit code 1, after
the bill of materials is written so that it can be read.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts/licence-policy.json"

PROBE = r"""
import importlib.metadata, importlib.util, json, os, platform, sys, sysconfig
def dists(paths=None):
    found = importlib.metadata.distributions(path=paths) if paths else importlib.metadata.distributions()
    return [{"name": d.metadata["Name"], "version": d.version,
             "license_expression": d.metadata.get("License-Expression"), "license": d.metadata.get("License"),
             "classifiers": d.metadata.get_all("Classifier") or []} for d in found]
spec = importlib.util.find_spec("martian")
ui = os.path.join(spec.submodule_search_locations[0], "ui") if spec else None
read = lambda path: open(path, encoding="utf-8").read() if path and os.path.isfile(path) else None
probe = {"python": dists(), "ui": {"stamp": read(ui and os.path.join(ui, "ui-version.json")),
                                    "licences": read(ui and os.path.join(ui, "third-party-licenses.md"))}}
if "--system" in sys.argv:
    base = sysconfig.get_path("purelib", vars={"base": sys.base_prefix, "platbase": sys.base_prefix})
    os_release = dict(line.rstrip().split("=", 1) for line in open("/etc/os-release") if "=" in line)
    debs, entry = [], {}
    status = read("/var/lib/dpkg/status") or ""
    for line in status.splitlines() + [""]:
        if not line and entry:
            if entry.get("Status", "").endswith(" installed"):
                copyright = read(f"/usr/share/doc/{entry['Package']}/copyright") or ""
                licences = sorted({l[8:].strip() for l in copyright.splitlines() if l.startswith("License:")}
                                  if copyright.startswith("Format:") else set())
                debs.append({"name": entry["Package"], "version": entry["Version"],
                             "arch": entry.get("Architecture", ""), "licences": licences})
            entry = {}
        elif ": " in line and not line.startswith(" "):
            key, value = line.split(": ", 1)
            entry[key] = value
    probe["system"] = {
        "python": {"implementation": platform.python_implementation(), "version": platform.python_version()},
        "base_python": dists([base]) if base not in sys.path else [],
        "os": {"id": os_release.get("ID", "").strip('"'), "version_id": os_release.get("VERSION_ID", "").strip('"')},
        "deb": debs,
    }
print(json.dumps(probe))
"""
"""Run by the Python whose installed packages are listed: in a fresh environment, or in the image."""

LICENCE_ALIASES = {
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "mit license": "MIT",
    "3-clause bsd license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "simplified bsd license": "BSD-2-Clause",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "python software foundation license": "PSF-2.0",
    "the unlicense (unlicense)": "Unlicense",
    "cc0 1.0 universal (cc0 1.0) public domain dedication": "CC0-1.0",
}
"""Free-text licences of Python metadata (the ``License`` field and the ``License ::`` classifiers)
that name exactly one SPDX licence. "BSD License" is not here: it does not say which BSD licence."""

TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9.+:-]+)")


def parse(expression: str) -> Any:
    """An SPDX licence expression as a tree: ``("id", name)``, ``("with", node, exception)``,
    ``("and", nodes)`` or ``("or", nodes)``. Raises ``ValueError`` if it is not one."""
    position, tokens = 0, []
    while position < len(expression.rstrip()):
        match = TOKEN.match(expression, position)
        if not match:
            raise ValueError(f"not an SPDX licence expression: {expression!r}")
        tokens.append(match.group(1))
        position = match.end()
    if not tokens:
        raise ValueError("empty licence expression")

    def compound(index: int, operator: str, operand: Any) -> tuple[Any, int]:
        node, index = operand(index)
        nodes = [node]
        while index < len(tokens) and tokens[index] == operator:
            node, index = operand(index + 1)
            nodes.append(node)
        return (nodes[0] if len(nodes) == 1 else (operator.lower(), nodes)), index

    def primary(index: int) -> tuple[Any, int]:
        if index >= len(tokens):
            raise ValueError(f"incomplete licence expression: {expression!r}")
        if tokens[index] == "(":
            node, index = disjunction(index + 1)
            if index >= len(tokens) or tokens[index] != ")":
                raise ValueError(f"unbalanced licence expression: {expression!r}")
            return node, index + 1
        if tokens[index] in {")", "AND", "OR", "WITH"}:
            raise ValueError(f"not an SPDX licence expression: {expression!r}")
        node: Any = ("id", tokens[index])
        if index + 1 < len(tokens) and tokens[index + 1] == "WITH":
            if index + 2 >= len(tokens) or tokens[index + 2] in {"(", ")", "AND", "OR", "WITH"}:
                raise ValueError(f"incomplete licence expression: {expression!r}")
            return ("with", node, tokens[index + 2]), index + 3
        return node, index + 1

    def conjunction(index: int) -> tuple[Any, int]:
        return compound(index, "AND", primary)

    def disjunction(index: int) -> tuple[Any, int]:
        return compound(index, "OR", conjunction)

    tree, end = disjunction(0)
    if end != len(tokens):
        raise ValueError(f"not an SPDX licence expression: {expression!r}")
    return tree


def permitted(expression: str, allowed: Sequence[str]) -> bool:
    """Whether ``expression`` lets us ship the component under licences in ``allowed``: one side of
    every ``OR`` and both sides of every ``AND``. A licence ``WITH`` an exception is permitted when the
    licence is, or when the whole ``<licence> WITH <exception>`` is allowed. Anything that is not an
    SPDX expression is not permitted."""
    names = {name.casefold() for name in allowed}

    def check(node: Any) -> bool:
        if node[0] == "id":
            return bool(node[1].casefold() in names)
        if node[0] == "with":
            return check(node[1]) or f"{node[1][1]} WITH {node[2]}".casefold() in names
        results = [check(child) for child in node[1]]
        return all(results) if node[0] == "and" else any(results)

    try:
        return check(parse(expression))
    except ValueError:
        return False


def is_expression(text: str) -> bool:
    try:
        parse(text)
    except ValueError:
        return False
    return True


def python_licence(meta: Mapping[str, Any]) -> tuple[str | None, str]:
    """A Python distribution's licence as an SPDX expression, or ``None`` when its metadata does not
    state one, and the licence as the metadata declares it. ``License-Expression`` wins; then a
    ``License`` field that is an expression or a known name; then the licence classifiers, all of
    which must be known names."""
    if expression := (meta.get("license_expression") or "").strip():
        return expression, expression
    text = (meta.get("license") or "").strip()
    if text and "\n" not in text:
        if spdx := LICENCE_ALIASES.get(text.casefold()):
            return spdx, text
        if is_expression(text):
            return text, text
    classifiers = [
        name.removeprefix("License :: ").removeprefix("OSI Approved :: ")
        for name in meta.get("classifiers", [])
        if name.startswith("License ::")
    ]
    if classifiers:
        known = [LICENCE_ALIASES.get(name.casefold()) for name in classifiers]
        declared = "; ".join(classifiers)
        if all(known):
            return " AND ".join(dict.fromkeys(name for name in known if name)), declared
        return None, declared
    return None, text.splitlines()[0] if text else ""


def canonical(name: str) -> str:
    """A Python distribution name as PyPI and the purl spec compare it."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Component:
    """One entry of a bill of materials and the input of the licence check."""

    ecosystem: str
    name: str
    version: str
    licence: str | None
    declared: str
    type: str = "library"
    purl: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    components: list[Component] = field(default_factory=list)
    treatment: str = ""

    @property
    def key(self) -> str:
        return f"{self.ecosystem}:{self.name}"


def python_component(meta: Mapping[str, Any]) -> Component:
    name = canonical(meta["name"])
    licence, declared = python_licence(meta)
    return Component("pypi", name, meta["version"], licence, declared, purl=f"pkg:pypi/{name}@{meta['version']}")


UI_HEADING = re.compile(r"^## (?P<name>\S+) - (?P<version>\S+)(?: \((?P<licence>.+)\))?$", re.MULTILINE)
"""One bundled package in the ``third-party-licenses.md`` that Vite writes into the Evals UI build."""


def ui_components(licences: str) -> list[Component]:
    """The npm packages bundled in the Evals UI, from its ``third-party-licenses.md``."""
    found = []
    for match in UI_HEADING.finditer(licences):
        name, version, licence = match["name"], match["version"], (match["licence"] or "").strip()
        purl = f"pkg:npm/{quote(name, safe='/')}@{version}"
        found.append(Component("npm", name, version, licence or None, licence, purl=purl))
    return found


def ui_component(ui: Mapping[str, str | None]) -> Component:
    """The Evals UI the ``martian`` package ships in ``martian/ui/``, with the packages bundled in it."""
    if not ui.get("stamp") or ui.get("licences") is None:
        raise ValueError("the installed martian package holds no Evals UI with third-party-licenses.md")
    stamp = json.loads(str(ui["stamp"]))
    return Component(
        "npm",
        str(stamp["name"]),
        str(stamp["version"]),
        None,
        "",
        type="application",
        properties={"martian:path": "martian/ui/", "martian:frontend-commit": str(stamp["commit"])},
        components=ui_components(str(ui["licences"])),
    )


def components(probe: Mapping[str, Any]) -> list[Component]:
    """Every component in what :data:`PROBE` printed."""
    found = sorted((python_component(meta) for meta in probe["python"]), key=lambda component: component.name)
    found.append(ui_component(probe["ui"]))
    if system := probe.get("system"):
        python = system["python"]
        if python["implementation"] != "CPython":
            raise ValueError(f"unexpected Python implementation {python['implementation']}")
        found.append(
            Component(
                "generic",
                "python",
                python["version"],
                None,
                "",
                type="application",
                purl=f"pkg:generic/python@{python['version']}",
            )
        )
        for meta in sorted(system["base_python"], key=lambda meta: canonical(meta["name"])):
            component = python_component(meta)
            component.properties["martian:location"] = "base image"
            found.append(component)
        distro = f"{system['os']['id']}-{system['os']['version_id']}"
        for deb in system["deb"]:
            purl = f"pkg:deb/{system['os']['id']}/{deb['name']}@{quote(deb['version'])}?arch={deb['arch']}"
            declared = "; ".join(deb["licences"])
            found.append(Component("deb", deb["name"], deb["version"], None, declared, purl=f"{purl}&distro={distro}"))
    return found


def walk(found: Sequence[Component]) -> Iterator[Component]:
    for component in found:
        yield component
        yield from walk(component.components)


def check(found: Sequence[Component], policy: Mapping[str, Any]) -> list[str]:
    """Apply the licence policy to every component, recording how each was treated, and return one
    line per component the policy refuses.

    A first-party component is not held to the allowed list. When its policy entry names a licence,
    a component that declares none takes it (the Evals UI's ``ui-version.json`` names no licence), and
    one that declares another is refused."""
    allowed = policy["allowed"]
    concluded = {entry["component"]: entry for entry in policy["concluded"]}
    exceptions = {(entry["component"], entry["licence"]) for entry in policy["exceptions"]}

    def matching(component: Component, section: str) -> Mapping[str, Any] | None:
        return next((e for e in policy[section] if fnmatch.fnmatchcase(component.key, e["component"])), None)

    refused = []
    for component in walk(found):
        if ours := matching(component, "first_party"):
            component.treatment = "first-party"
            if licence := ours.get("licence"):
                if component.licence is None:
                    component.licence = licence
                elif component.licence != licence:
                    component.treatment = "refused"
                    refused.append(
                        f"{component.key} {component.version}: declares {component.licence},"
                        f" not its first-party licence {licence}"
                    )
            continue
        if matching(component, "not_checked"):
            component.treatment = "not checked"
            continue
        treatment = "allowed"
        conclusion = concluded.get(component.key)
        if conclusion and conclusion["declared"] == component.declared:
            component.licence, treatment = conclusion["licence"], "concluded"
        if component.licence is not None and permitted(component.licence, allowed):
            component.treatment = treatment
        elif (component.key, component.licence) in exceptions:
            component.treatment = "exception"
        else:
            component.treatment = "refused"
            reason = (
                f"{component.licence} is outside the allowed list"
                if component.licence
                else f"no SPDX licence stated (declared {component.declared!r})"
            )
            refused.append(f"{component.key} {component.version}: {reason}")
    return refused


def cyclonedx_component(component: Component) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": component.type,
        "bom-ref": component.purl or f"{component.key}@{component.version}",
        "name": component.name,
        "version": component.version,
    }
    if component.purl:
        entry["purl"] = component.purl
    if component.licence:
        acknowledgement = "concluded" if component.treatment == "concluded" else "declared"
        entry["licenses"] = [{"expression": component.licence, "acknowledgement": acknowledgement}]
    elif component.declared:
        names = component.declared.split("; ")
        entry["licenses"] = [{"license": {"name": name, "acknowledgement": "declared"}} for name in names]
    properties = dict(component.properties)
    if component.treatment == "concluded" and component.declared:
        properties["martian:declared-licence"] = component.declared
    if component.treatment:
        properties["martian:licence-policy"] = component.treatment
    if properties:
        entry["properties"] = [{"name": name, "value": value} for name, value in properties.items()]
    if component.components:
        entry["components"] = [cyclonedx_component(child) for child in component.components]
    return entry


def bom(subject: Mapping[str, Any], found: Sequence[Component]) -> dict[str, Any]:
    """A CycloneDX 1.6 bill of materials for ``subject``, the release artifact, holding ``found``."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": {"components": [{"type": "application", "name": "martian-redteam scripts/sbom.py"}]},
            "component": dict(subject),
        },
        "components": [cyclonedx_component(component) for component in found],
    }


def split_sdk(found: list[Component]) -> tuple[Component, list[Component]]:
    sdk = [component for component in found if component.key == "pypi:martian-sdk"]
    if len(sdk) != 1:
        raise ValueError(f"expected one martian-sdk installed, found {len(sdk)}")
    return sdk[0], [component for component in found if component is not sdk[0]]


def run(command: Sequence[str]) -> bytes:
    return subprocess.run(list(command), check=True, stdout=subprocess.PIPE).stdout


def probe_wheel(dist_dir: Path) -> dict[str, Any]:
    """Install the wheel as the image does, in a fresh environment, and run :data:`PROBE` there."""
    wheels = list(dist_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"expected exactly one wheel in {dist_dir}, found {len(wheels)}")
    with tempfile.TemporaryDirectory(prefix="martian-sbom-") as work:
        requirements, environment = Path(work) / "requirements.txt", Path(work) / "venv"
        export = ["uv", "export", "--locked", "--no-dev", "--extra", "evals", "--no-emit-project", "--no-header"]
        run([*export, "--quiet", "--output-file", str(requirements), "--project", str(ROOT)])
        run(["uv", "venv", "--quiet", "--python", "3.12", str(environment)])
        python = str(environment / "bin/python")
        install = ["uv", "pip", "install", "--quiet", "--python", python, "--no-deps"]
        run([*install, "--require-hashes", "--requirements", str(requirements)])
        run([*install, str(wheels[0].resolve())])
        run(["uv", "pip", "check", "--quiet", "--python", python])
        return dict(json.loads(run([python, "-I", "-c", PROBE])))


def sdk_subject(sdk: Component) -> dict[str, Any]:
    return cyclonedx_component(sdk) | {"type": "application"}


def write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("subject", choices=["wheel", "image"])
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"), help="wheel: the folder with the one wheel")
    parser.add_argument("--image", help="image: the image to list, as built by the release")
    parser.add_argument("--out-dir", type=Path, default=Path("release-sbom"))
    parser.add_argument("--policy", type=Path, default=POLICY)
    args = parser.parse_args(argv)
    policy = json.loads(args.policy.read_text(encoding="utf-8"))

    if args.subject == "wheel":
        sdk, found = split_sdk(components(probe_wheel(args.dist_dir)))
        subject = sdk_subject(sdk)
        path = args.out_dir / f"martian-sdk-{sdk.version}.cdx.json"
    else:
        if not args.image:
            parser.error("image needs --image")
        image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", args.image]).decode().strip()
        docker = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", image_id]
        probe = run([*docker, "-I", "-c", PROBE, "--system"])
        found = components(json.loads(probe))
        sdk, _ = split_sdk(found)
        repository = args.image.rsplit(":", 1)[0] if ":" in args.image.rsplit("/", 1)[-1] else args.image
        subject = {
            "type": "container",
            "bom-ref": args.image,
            "name": repository,
            "version": sdk.version,
            "properties": [{"name": "martian:image-id", "value": image_id}],
        }
        path = args.out_dir / f"martian-evals-image-{sdk.version}.cdx.json"

    refused = check(found, policy)
    write(path, bom(subject, found))
    counts: dict[str, int] = {}
    for component in walk(found):
        counts[component.treatment] = counts.get(component.treatment, 0) + 1
    print(json.dumps({"sbom": str(path), "components": sum(counts.values()), "licence_policy": counts}))
    if refused:
        print(f"licence check failed for {len(refused)} component(s):", *refused, sep="\n  ", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
