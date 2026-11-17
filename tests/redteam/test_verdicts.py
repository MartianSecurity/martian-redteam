"""The red team verdict rules pass their published test vectors (martian-notes#227)."""

import copy
from pathlib import Path

import pytest

from martian.redteam.model import EvidenceLevel
from martian.redteam.verify import RULES_VERSION, Repetition, load_vectors, run_vector, verify
from martian.redteam.verify.rules import REQUIRED_TIER, ceiling

VECTORS = load_vectors()["vectors"]
SPEC = Path(__file__).resolve().parents[2] / "docs" / "redteam-verdicts.md"


def test_the_vectors_name_this_rules_version():
    assert load_vectors()["version"] == RULES_VERSION


@pytest.mark.parametrize("vector", VECTORS, ids=[vector["name"] for vector in VECTORS])
def test_the_open_rules_pass_every_vector(vector):
    assert run_vector(vector) == vector["expected"]


def test_a_changed_expectation_is_caught():
    vector = copy.deepcopy(next(item for item in VECTORS if item["rule"] == "verify"))
    vector["expected"]["outcome"] = "verified" if vector["expected"]["outcome"] != "verified" else "consistent"
    assert run_vector(vector) != vector["expected"]


def test_vector_names_are_unique_and_every_rule_has_vectors():
    assert len({vector["name"] for vector in VECTORS}) == len(VECTORS)
    assert {vector["rule"] for vector in VECTORS} == {
        "text_check",
        "canary_check",
        "judge_result",
        "attempt_outcome",
        "verify",
        "ceiling",
        "label",
        "tool_call_check",
        "receipt_check",
    }


def test_an_undetermined_input_is_never_a_hit_a_pass_or_verified_in_any_vector():
    for vector in VECTORS:
        given, expected = vector["input"], vector["expected"]
        if vector["rule"] == "judge_result" and type(given["verdict"]) is not bool:
            assert expected["hit"] is None, vector["name"]
        if vector["rule"] == "attempt_outcome":
            hits = [item for item in given["results"] if item["hit"] is True]
            if not hits and any(item["hit"] is None for item in given["results"]):
                assert expected["outcome"] == "undetermined", vector["name"]
        if vector["rule"] == "verify":
            undecided = any(rep["outcome"] in {"undetermined", "error"} for rep in given["repetitions"])
            control_undecided = given.get("require_control", True) and given.get("control") not in {"resisted", "hit"}
            if undecided or (control_undecided and expected["outcome"] != "not_reproducible"):
                assert expected["outcome"] == "unverifiable", vector["name"]


@pytest.mark.parametrize("objective", ["disclosure", "control", "action", "persistence"])
def test_the_vectors_state_the_ceiling_of_each_objective_without_an_environment(objective):
    stated = [
        vector
        for vector in VECTORS
        if vector["rule"] == "ceiling" and vector["input"] == {"objective": objective, "environment": False}
    ]
    assert len(stated) == 1
    assert stated[0]["expected"]["outcome"] == ceiling(objective, environment=False)[0]


def test_without_an_environment_an_action_never_verifies_whatever_the_evidence():
    for level in range(2, 5):
        reps = [Repetition("hit", EvidenceLevel(level))] * 2
        assert verify("action", reps, control="resisted").outcome == "consistent"
    assert REQUIRED_TIER["action"] == "state"


def test_the_specification_names_every_level_and_outcome():
    text = SPEC.read_text("utf-8")
    for words in (
        "Delivered",
        "Suspected",
        "Confirmed disclosure",
        "Attempted action",
        "Confirmed action",
        "Verified exfiltration",
        "Verified in environment",
        "verified",
        "consistent",
        "not_reproducible",
        "unverifiable",
        RULES_VERSION,
    ):
        assert words in text, words
