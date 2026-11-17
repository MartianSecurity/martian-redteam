"""The release's bills of materials and their licence check, scripts/sbom.py with scripts/licence-policy.json."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sbom  # noqa: E402

POLICY = json.loads((ROOT / "scripts/licence-policy.json").read_text(encoding="utf-8"))
STAMP = json.dumps({"name": "@martian/evals", "version": "0.1.0", "commit": "0" * 40, "dirty": False})
UI = "# Licenses\n\n## react - 19.3.0 (MIT)\n\nMIT License\n\n## @fontsource-variable/inter - 5.3.0 (OFL-1.1)\n\nSIL\n"


def meta(name, version="1.0", *, expression=None, license=None, classifiers=()):
    """A Python distribution as the probe reports it."""
    return {
        "name": name,
        "version": version,
        "license_expression": expression,
        "license": license,
        "classifiers": list(classifiers),
    }


def probe(*python, ui=UI, system=None):
    """What the probe prints for an install of the SDK with ``python`` and a UI bundling ``ui``."""
    found = {"python": [meta("martian-sdk", "0.1.4", expression="Apache-2.0"), *python]}
    found["ui"] = {"stamp": STAMP, "licences": ui}
    return found | ({"system": system} if system else {})


def treatments(found):
    return {component.key: component.treatment for component in sbom.walk(found)}


@pytest.mark.parametrize(
    ("expression", "allowed"),
    [
        ("MIT", True),
        ("mit", True),
        ("MIT OR GPL-3.0-only", True),
        ("(Apache-2.0 OR MIT) AND BSD-3-Clause", True),
        ("Apache-2.0 WITH LLVM-exception", True),
        ("MIT AND GPL-3.0-only", False),
        ("GPL-2.0-only", False),
        ("GPL-3.0-or-later", False),
        ("LGPL-3.0-only", False),
        ("AGPL-3.0-only", False),
        ("SSPL-1.0", False),
        ("GPL-2.0-only WITH Classpath-exception-2.0", False),
        ("LicenseRef-Proprietary", False),
        ("UNLICENSED", False),
        ("BSD License", False),
        ("MIT OR", False),
        ("(MIT", False),
        ("", False),
    ],
)
def test_the_allowed_list_decides_each_spdx_expression(expression, allowed):
    assert sbom.permitted(expression, POLICY["allowed"]) is allowed


APACHE_TEXT = "Copyright 2018\nApache License\nVersion 2.0"
APACHE, BSD, MIT = (
    f"License :: OSI Approved :: {name}" for name in ("Apache Software License", "BSD License", "MIT License")
)
GPL = "License :: OSI Approved :: GNU General Public License v3 (GPLv3)"


@pytest.mark.parametrize(
    ("metadata", "licence", "declared"),
    [
        (meta("a", expression="MIT OR Apache-2.0", license="GPL"), "MIT OR Apache-2.0", "MIT OR Apache-2.0"),
        (meta("a", license="Apache 2.0"), "Apache-2.0", "Apache 2.0"),
        (meta("a", license="BSD-3-Clause"), "BSD-3-Clause", "BSD-3-Clause"),
        (meta("a", license=APACHE_TEXT, classifiers=[APACHE]), "Apache-2.0", "Apache Software License"),
        (meta("a", classifiers=[BSD]), None, "BSD License"),
        (meta("a", classifiers=[MIT, GPL]), None, "MIT License; GNU General Public License v3 (GPLv3)"),
        (meta("a"), None, ""),
    ],
    ids=[
        "expression first",
        "known name",
        "spdx field",
        "text to classifiers",
        "bsd variant",
        "unknown classifier",
        "none",
    ],
)
def test_python_metadata_gives_a_licence_only_when_it_names_one(metadata, licence, declared):
    assert sbom.python_licence(metadata) == (licence, declared)


def test_the_check_refuses_a_gpl_dependency_and_fails_the_build(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sbom, "probe_wheel", lambda _: probe(meta("copyleft", "2.0", expression="GPL-3.0-only")))
    with pytest.raises(SystemExit) as stopped:
        sbom.main(["wheel", "--out-dir", str(tmp_path)])
    assert stopped.value.code == 1
    assert "pypi:copyleft 2.0: GPL-3.0-only is outside the allowed list" in capsys.readouterr().err
    written = json.loads((tmp_path / "martian-sdk-0.1.4.cdx.json").read_text())
    refused = [c for c in written["components"] if c["name"] == "copyleft"]
    assert refused[0]["properties"] == [{"name": "martian:licence-policy", "value": "refused"}]


def test_the_check_refuses_a_dependency_with_no_stated_licence():
    found = sbom.components(probe(meta("mystery", classifiers=["License :: OSI Approved :: BSD License"])))
    assert sbom.check(found, POLICY) == ["pypi:mystery 1.0: no SPDX licence stated (declared 'BSD License')"]


def test_the_wheel_bill_lists_the_dependencies_and_the_uis_bundled_packages(tmp_path, monkeypatch):
    found = probe(meta("Typing_Extensions", "4.0", expression="PSF-2.0"), meta("certifi", license="MPL-2.0"))
    monkeypatch.setattr(sbom, "probe_wheel", lambda _: found)
    sbom.main(["wheel", "--out-dir", str(tmp_path)])
    written = json.loads((tmp_path / "martian-sdk-0.1.4.cdx.json").read_text())
    assert (written["bomFormat"], written["specVersion"]) == ("CycloneDX", "1.6")
    assert written["metadata"]["component"]["purl"] == "pkg:pypi/martian-sdk@0.1.4"
    assert written["metadata"]["component"]["licenses"] == [{"expression": "Apache-2.0", "acknowledgement": "declared"}]
    by_name = {c["name"]: c for c in written["components"]}
    assert by_name["typing-extensions"]["purl"] == "pkg:pypi/typing-extensions@4.0"
    assert by_name["certifi"]["licenses"] == [{"expression": "MPL-2.0", "acknowledgement": "declared"}]
    ui = by_name["@martian/evals"]
    assert {"name": "martian:licence-policy", "value": "first-party"} in ui["properties"]
    assert ui["licenses"] == [{"expression": "Apache-2.0", "acknowledgement": "declared"}]
    assert [(c["purl"], c["licenses"][0]["expression"]) for c in ui["components"]] == [
        ("pkg:npm/react@19.3.0", "MIT"),
        ("pkg:npm/%40fontsource-variable/inter@5.3.0", "OFL-1.1"),
    ]


def test_the_uis_own_packages_are_first_party_apache_2_0():
    found = sbom.components(probe())
    assert sbom.check(found, POLICY) == []
    ui = next(c for c in found if c.key == "npm:@martian/evals")
    assert (ui.licence, ui.treatment) == ("Apache-2.0", "first-party")
    sdk = next(c for c in found if c.key == "pypi:martian-sdk")
    assert (sdk.licence, sdk.treatment) == ("Apache-2.0", "first-party")
    relicensed = [sbom.Component("npm", "@martian/design-system", "0.1.0", "MIT", "MIT")]
    assert sbom.check(relicensed, POLICY) == [
        "npm:@martian/design-system 0.1.0: declares MIT, not its first-party licence Apache-2.0"
    ]
    assert relicensed[0].treatment == "refused"


def test_notice_attributes_the_evals_ui_and_its_shadcn_code():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    ui = notice[notice.index("The Evals UI in martian/ui/") :]
    for package in ("@martian/evals", "@martian/redteam", "@martian/design-system", "@martian/client"):
        assert package in ui
    assert "licensed under the Apache License,\nVersion 2.0" in ui
    assert "martian/ui/third-party-licenses.md" in ui
    assert "Copyright (c) 2023 shadcn" in ui
    assert "The above copyright notice and this permission notice shall be included" in ui


def test_a_conclusion_applies_only_to_the_licence_text_it_reviewed():
    found = sbom.components(
        probe(meta("gitdb", license="BSD License", classifiers=["License :: OSI Approved :: BSD License"]))
    )
    assert sbom.check(found, POLICY) == []
    gitdb = next(c for c in found if c.name == "gitdb")
    assert (gitdb.licence, gitdb.treatment) == ("BSD-3-Clause", "concluded")
    changed = sbom.components(probe(meta("gitdb", license="GPL-3.0-only")))
    assert sbom.check(changed, POLICY) == ["pypi:gitdb 1.0: GPL-3.0-only is outside the allowed list"]


def test_an_exception_covers_one_component_under_one_licence():
    found = sbom.components(probe())
    assert sbom.check(found, POLICY) == []
    assert treatments(found)["npm:@fontsource-variable/inter"] == "exception"
    other_font = UI + "## some-font - 1.0.0 (OFL-1.1)\n\ntext\n"
    assert sbom.check(sbom.components(probe(ui=other_font)), POLICY) == [
        "npm:some-font 1.0.0: OFL-1.1 is outside the allowed list"
    ]
    relicensed = UI.replace("(OFL-1.1)", "(GPL-3.0-only)")
    assert sbom.check(sbom.components(probe(ui=relicensed)), POLICY) == [
        "npm:@fontsource-variable/inter 5.3.0: GPL-3.0-only is outside the allowed list"
    ]


def test_the_check_refuses_a_gpl_or_unlicensed_package_bundled_in_the_ui():
    ui = UI + "## copyleft-widget - 2.0.0 (AGPL-3.0-only)\n\ntext\n## no-licence - 1.0.0\n"
    assert sbom.check(sbom.components(probe(ui=ui)), POLICY) == [
        "npm:copyleft-widget 2.0.0: AGPL-3.0-only is outside the allowed list",
        "npm:no-licence 1.0.0: no SPDX licence stated (declared '')",
    ]


def test_the_image_bill_adds_the_interpreter_the_base_image_and_its_debian_packages():
    system = {
        "python": {"implementation": "CPython", "version": "3.12.16"},
        "base_python": [meta("pip", "25.0.1", license="MIT")],
        "os": {"id": "debian", "version_id": "13"},
        "deb": [{"name": "bash", "version": "5.2.37-2+b10", "arch": "amd64", "licences": ["GPL-3+", "MIT-like"]}],
    }
    found = sbom.components(probe(system=system))
    assert sbom.check(found, POLICY) == []
    by_key = {component.key: component for component in sbom.walk(found)}
    assert (by_key["generic:python"].licence, by_key["generic:python"].treatment) == ("PSF-2.0", "concluded")
    assert by_key["pypi:pip"].properties == {"martian:location": "base image"}
    bash = sbom.cyclonedx_component(by_key["deb:bash"])
    assert bash["purl"] == "pkg:deb/debian/bash@5.2.37-2%2Bb10?arch=amd64&distro=debian-13"
    assert bash["licenses"] == [
        {"license": {"name": "GPL-3+", "acknowledgement": "declared"}},
        {"license": {"name": "MIT-like", "acknowledgement": "declared"}},
    ]
    assert bash["properties"] == [{"name": "martian:licence-policy", "value": "not checked"}]


def test_the_packages_in_the_shipped_ui_pass_the_policy():
    licences = (ROOT / "src/martian/ui/third-party-licenses.md").read_text(encoding="utf-8")
    bundled = sbom.ui_components(licences)
    assert len(bundled) == licences.count("\n## ") > 0
    assert sbom.check(bundled, POLICY) == []


def test_the_probe_lists_this_environment_and_its_packaged_ui():
    output = subprocess.run([sys.executable, "-I", "-c", sbom.PROBE], check=True, capture_output=True).stdout
    found = sbom.components(json.loads(output))
    sdk, rest = sbom.split_sdk(found)
    assert sdk.licence == "Apache-2.0"
    ui = next(component for component in rest if component.name == "@martian/evals")
    assert {c.name for c in ui.components} >= {"react", "react-dom"}


def steps(job):
    return [json.dumps(step) for step in job["steps"]]


def position(lines, text):
    found = [number for number, line in enumerate(lines) if text in line]
    assert len(found) == 1, (text, found)
    return found[0]


def test_the_release_checks_licences_before_the_image_and_keeps_both_bills():
    build = steps(yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]["build"])
    order = [
        position(build, "scripts/check_release.py --install"),
        position(build, "scripts/sbom.py wheel"),
        position(build, "docker build"),
        position(build, "scripts/check_image.py"),
        position(build, "scripts/sbom.py image"),
        position(build, '"name": "release-sbom"'),
    ]
    assert order == sorted(order), order


def test_pull_requests_run_the_licence_check_on_the_wheel():
    packaging = steps(
        yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]["packaging"]
    )
    assert position(packaging, "scripts/sbom.py wheel") >= 0
