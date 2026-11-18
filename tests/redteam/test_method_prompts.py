"""The single-turn rewrite pack and the method prompt files' licence register (martian-notes#274).

Every prompt file of the attack methods is in ``methods/prompts/register.json`` with a source and a
licence the release's licence policy allows; every registered method is documented with what it
tries."""

import json
import re
from pathlib import Path

import pytest

from martian.redteam.engine import Engine
from martian.redteam.methods import library, method_names, resolve
from martian.redteam.methods.rewrites import REWRITES
from martian.redteam.runfile import AttackSpec
from tests.redteam.engine_helpers import CountingTarget, StubService, run_file

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = json.loads((ROOT / "scripts" / "licence-policy.json").read_text())["allowed"]
DOC = (ROOT / "docs" / "redteam-methods.md").read_text()


def test_every_method_prompt_file_is_in_the_licence_register_under_an_allowed_licence():
    assert library.prompt_files(), "no prompt files found"
    assert library.register_problems(ALLOWED) == []


def test_the_register_check_fails_on_an_unregistered_file_and_a_licence_that_is_not_allowed(tmp_path, monkeypatch):
    (tmp_path / "rewrites").mkdir()
    (tmp_path / "rewrites" / "ours.md").write_text("Write the attack.")
    (tmp_path / "rewrites" / "stray.md").write_text("Copied from somewhere.")
    register = tmp_path / "register.json"
    register.write_text(
        json.dumps(
            {
                "files": {
                    "rewrites/ours.md": {"licence": "GPL-3.0", "source": "a GPL project"},
                    "rewrites/gone.md": {"licence": "Apache-2.0", "source": "Martian Security"},
                }
            }
        )
    )
    monkeypatch.setattr(library, "FOLDER", tmp_path)
    monkeypatch.setattr(library, "REGISTER", register)
    assert library.register_problems(ALLOWED) == [
        "rewrites/stray.md: not in the licence register",
        "rewrites/gone.md: listed in the register, but there is no such file",
        "rewrites/ours.md: licence 'GPL-3.0' is not allowed",
    ]


def test_every_registered_method_is_documented_with_what_it_tries():
    rows = {
        match.group(1): match.group(2)
        for match in re.finditer(r"^\| `(\w+)` \| `\w+` \|(?:[^|]*\|){4} ([^|]+) \|$", DOC, re.MULTILINE)
    }
    for name in method_names():
        assert name in rows and len(rows[name].split()) >= 5, name


@pytest.mark.parametrize("style", list(REWRITES))
async def test_a_rewrite_asks_the_attacker_for_its_style_and_the_goal_and_sends_once(style):
    name, label, file = REWRITES[style]
    method = resolve(AttackSpec(name=name))
    assert (method.kind, method.arm, method.query_cap, method.legacy) == ("rewrite", "static", 1, False)
    service, target = StubService(), CountingTarget()
    file_ = run_file(
        attacks=[{"name": name}],
        vulnerabilities=[
            {
                "name": "ExcessiveAgency",
                "types": ["functionality"],
                "goal": {"tool": "issue_refund", "where": {"amount": "> 50"}},
                "checks": [{"type": "tool_call", "tool": "issue_refund"}],
            }
        ],
    )
    run = await Engine(file_, service=service, target=target).run()
    [(context, request)] = service.simulated
    assert context.attack_method == style and context.mode == "baseline"
    assert request.startswith(f"Attack style: {label}. {library.prompt(file)}")
    assert request.endswith("The goal of the attack: call issue_refund with amount > 50")
    assert len(target.calls) == 1 and run.attempts[0].target_calls == 1
    assert run.provenance["methods_revision"] == library.METHODS_REVISION


def test_a_changed_prompt_file_changes_the_revision_and_the_fingerprint(tmp_path, monkeypatch):
    import shutil

    from martian.redteam.engine import plan

    copy = tmp_path / "prompts"
    shutil.copytree(library.FOLDER, copy)
    monkeypatch.setattr(library, "FOLDER", copy)
    before = library._revision()
    (copy / "rewrites" / "authority.md").write_text("Something else.")
    assert library._revision() != before
    [planned] = plan(
        run_file(
            attacks=[{"name": "Authority"}], vulnerabilities=[{"name": "PromptLeakage", "types": ["system_prompt"]}]
        )
    )
    engine = Engine(run_file(), service=StubService(), target=CountingTarget())
    first = engine.fingerprint(planned, 1)
    monkeypatch.setattr(library, "METHODS_REVISION", library._revision())
    assert engine.fingerprint(planned, 1) != first
