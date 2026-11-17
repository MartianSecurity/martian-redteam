"""The generator and judge prompts live in the SDK and build the same text the hosted engine built
before they moved (martian-notes#234).

``fixtures/hosted-prompt-digests.json`` holds the sha256 of every prompt ``martian-agents`` built from
its own copy of the text, for every catalogue vulnerability, type and attack method, both modes,
two purposes and four kinds of criteria. Only digests are kept, so the text has one copy."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from martian.redteam.catalog import prompts
from martian.redteam.catalog.prompts import evaluator_system_prompt, simulator_system_prompt
from martian_redteam.catalog import ATTACKS, VULNERABILITIES
from martian_redteam.catalog.vulnerabilities import CATALOG_VERSION
from martian_redteam.types import EvaluatorContext, SimulatorContext

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "hosted-prompt-digests.json").read_text())


@dataclass(frozen=True)
class Engine:
    """The hosted engine's context dataclasses have these fields, plus ones the prompts never read."""

    vulnerability: str
    vulnerability_type: str
    attack_method: str
    purpose: str = ""
    mode: str = "baseline"
    criteria: str | None = None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _built(key: str) -> str:
    kind, vulnerability, vulnerability_type, method, *rest = key.split("|")
    if kind == "simulator":
        mode, purpose = rest
        context = SimulatorContext(
            vulnerability=vulnerability,
            vulnerability_type=vulnerability_type,
            attack_method=method,
            purpose=FIXTURE["purposes"][int(purpose.removeprefix("purpose"))],
            mode=mode,
        )
        return simulator_system_prompt(context)
    criteria = eval(rest[0])  # noqa: S307 - the fixture writes the criteria with repr()
    return evaluator_system_prompt(
        EvaluatorContext(
            vulnerability=vulnerability, vulnerability_type=vulnerability_type, attack_method=method, criteria=criteria
        )
    )


def test_every_prompt_is_the_text_the_hosted_engine_built():
    changed = [key for key, digest in FIXTURE["digests"].items() if _digest(_built(key)) != digest]
    assert not changed, f"{len(changed)} prompts differ, for example {changed[:3]}"


def test_the_fixture_covers_every_catalogue_id():
    keys = FIXTURE["digests"]
    for name, make in VULNERABILITIES.items():
        vulnerability = make()
        for vulnerability_type in vulnerability.allowed_types:
            for attack in ATTACKS.values():
                method = attack().id
                assert f"evaluator|{vulnerability.id}|{vulnerability_type}|{method}|None" in keys, name
                if attack().kind == "llm":
                    assert f"simulator|{vulnerability.id}|{vulnerability_type}|{method}|baseline|purpose0" in keys


def test_the_prompts_read_any_context_with_the_fields_and_both_shapes_agree():
    sdk = SimulatorContext(
        vulnerability="bias", vulnerability_type="race", purpose="Help desk", attack_method="roleplay"
    )
    engine = Engine(vulnerability="bias", vulnerability_type="race", purpose="Help desk", attack_method="roleplay")
    assert simulator_system_prompt(sdk) == simulator_system_prompt(engine)
    judged = EvaluatorContext(vulnerability="bias", vulnerability_type="race", attack_method="roleplay")
    assert evaluator_system_prompt(judged) == evaluator_system_prompt(Engine("bias", "race", "roleplay"))


def test_the_revision_names_the_catalogue_version_and_changes_with_the_text(monkeypatch):
    assert prompts.PROMPTS_REVISION.startswith(f"redteam-prompts-{CATALOG_VERSION}+")
    assert prompts._revision() == prompts.PROMPTS_REVISION
    changed = dict(prompts.RUBRICS) | {"bias": "Score 0 always."}
    monkeypatch.setattr(prompts, "RUBRICS", changed)
    assert prompts._revision() != prompts.PROMPTS_REVISION
    monkeypatch.undo()
    monkeypatch.setattr(prompts, "CATALOG_VERSION", "9.9.9")
    assert prompts._revision().startswith("redteam-prompts-9.9.9+")


@pytest.mark.parametrize("table", [prompts.ATTACK_METHODS, prompts.VULNERABILITY_GOALS, prompts.RUBRICS])
def test_the_text_cannot_be_changed_at_run_time(table):
    with pytest.raises(TypeError):
        table["bias"] = "changed"  # type: ignore[index]
