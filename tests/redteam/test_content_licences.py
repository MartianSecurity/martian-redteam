"""The red team content register covers every content file, and CI fails on one it does not
(MartianSecurity/martian-notes#325)."""

import copy
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import content_licences  # noqa: E402

REGISTER = json.loads(content_licences.REGISTER.read_text(encoding="utf-8"))


def test_the_register_covers_every_content_file_the_package_ships():
    files = content_licences.tracked()
    assert content_licences.check(files, REGISTER) == []
    covered = set(content_licences.content_files(files, REGISTER))
    assert {"src/martian/redteam/catalog/prompts.py", "src/martian/redteam/verify/vectors.json"} <= covered
    assert "src/martian/redteam/server/contract/redteam-operations.json" not in covered


def test_the_script_passes_on_the_repository():
    result = subprocess.run([sys.executable, "scripts/content_licences.py"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_an_unregistered_content_file_fails():
    files = [*content_licences.tracked(), "src/martian/redteam/catalog/jailbreaks.jsonl", "src/martian/redteam/x.csv"]
    problems = content_licences.check(files, REGISTER)
    assert [line.split(":")[0] for line in problems] == [
        "src/martian/redteam/catalog/jailbreaks.jsonl",
        "src/martian/redteam/x.csv",
    ]
    assert all("content with no entry" in line for line in problems)


def test_a_licence_that_is_not_permitted_or_not_commercial_fails():
    register = copy.deepcopy(REGISTER)
    entry = next(item for item in register["entries"] if item["path"].endswith("prompts.py"))
    entry["licence"] = "CC-BY-NC-4.0"
    entry["commercial_use"] = False
    problems = content_licences.check(content_licences.tracked(), register)
    assert len(problems) == 2 and all(line.startswith("src/martian/redteam/catalog/prompts.py") for line in problems)


def test_an_entry_for_a_file_that_is_gone_fails():
    register = copy.deepcopy(REGISTER)
    register["entries"].append({**register["entries"][0], "path": "src/martian/redteam/catalog/gone.json"})
    assert content_licences.check(content_licences.tracked(), register) == [
        "src/martian/redteam/catalog/gone.json: registered, but no such file is tracked; remove its entry"
    ]


def test_a_shipped_source_under_a_restrictive_licence_fails():
    register = copy.deepcopy(REGISTER)
    beavertails = next(item for item in register["sources"] if item["name"] == "BeaverTails")
    beavertails["shipped"] = True
    assert content_licences.check(content_licences.tracked(), register) == [
        "source BeaverTails: shipped under 'CC-BY-NC-4.0', which is not permitted"
    ]


def test_the_private_repositories_are_recorded_as_not_opened():
    found = {item["repository"]: item["status"] for item in REGISTER["not_opened"]}
    assert found == dict.fromkeys(
        ("MartianSecurity/martian-agents", "MartianSecurity/martian-skills", "MartianSecurity/martian-wiki"),
        "not opened",
    )
    restricted = {item["name"] for item in REGISTER["sources"] if not item["shipped"] and not item["commercial_use"]}
    assert {"BeaverTails", "Do-Not-Answer"} <= restricted


def test_the_register_ships_in_the_package():
    from importlib import resources

    text = resources.files("martian.redteam.catalog").joinpath("licences.json").read_text(encoding="utf-8")
    assert json.loads(text)["version"] == 1
