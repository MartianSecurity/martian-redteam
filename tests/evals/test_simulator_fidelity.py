"""Simulated results are labelled, and simulators are checked against real users
(MartianSecurity/martian-notes#194).

Every place a reader sees a run's results says which of them a simulated user stands behind: the
printed table, ``summary.json``, the pull-request comment. ``martian evals fidelity`` compares a
simulator setup's conversations with real transcripts on turns, corrections and goal reached, by
fixed rules. The simulator is the loopback fake model of ``test_simulated_users``, and the real
transcripts are written here with invented text: nothing calls a real model."""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import mlflow
import pytest
from typer.testing import CliRunner

from martian.cli import app
from martian.config import ConfigError
from martian.evals import fidelity
from martian.evals.personas import read_transcripts
from martian.evals.runner import run_suite
from martian.evals.state import STATE
from martian.evals.stats import mean_interval, wilson
from martian.evals.suite_file import load_suite_file
from tests.evals.test_simulated_users import CONVERSATION, KEY, MODEL, SCRIPTS, FakeModel, example

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    model = FakeModel()
    monkeypatch.setenv("OPENAI_API_BASE", model.base)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    try:
        yield model
    finally:
        model.server.shutdown()
        model.server.server_close()
        STATE.reset()
        mlflow.set_tracking_uri(old_uri)


def comment_module():
    spec = importlib.util.spec_from_file_location("evals_pr_comment", REPOSITORY / "scripts/evals_pr_comment.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["evals_pr_comment"] = module
    spec.loader.exec_module(module)
    return module


def said(*users: str) -> list[dict[str, str]]:
    """A conversation of these user messages, each answered."""
    return [m for text in users for m in ({"role": "user", "content": text}, {"role": "assistant", "content": "OK."})]


# Invented real conversations. The first is labelled; the outcome rule reads the others: the second
# corrects a request and then thanks, the third repeats its request and stops, and the fourth asks
# one question and says nothing the rule reads.
REAL = [
    {
        "id": "real-1",
        "goal_reached": True,
        "messages": [
            {"role": "system", "content": "You are a shop assistant."},
            *said("I need to cancel order A-1002.", "A-1002, the lamp.", "Thanks!"),
        ],
    },
    {
        "id": "real-2",
        "messages": said(
            "Where is my parcel?", "That's not what I asked. Where is my parcel now?", "Thank you, that helps."
        ),
    },
    {"id": "real-3", "messages": said("Can you cancel my order A-1002?", "Please cancel my order A-1002.")},
    {"id": "real-4", "messages": said("Do you ship to Norway?")},
]


def write_real(folder: Path) -> Path:
    path = folder / "transcripts.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in REAL), encoding="utf-8")
    return path


# ---------- the label ----------


def test_simulated_results_are_labelled_where_a_run_is_read(tmp_path, fake):
    result = CliRunner().invoke(app, ["evals", "run", str(example(tmp_path))])
    assert result.exit_code == 1
    printed = result.output
    # The table's title counts the simulated cases, and its Simulated row has their verdicts.
    assert "evals.yaml: 2 cases, 2 simulated" in printed
    row = next(line for line in printed.splitlines() if "Simulated" in line and "│" in line)
    assert [cell.strip() for cell in row.split("│")[1:5]] == ["Simulated", "1", "1", "0"]

    summary = json.loads((tmp_path / "results/summary.json").read_text())
    simulated = summary["simulator"]
    assert (simulated["cases"], simulated["passed"], simulated["failed"], simulated["errors"]) == (2, 1, 1, 0)
    assert simulated["confidence_95"] == list(wilson(1, 2))

    comment = comment_module()
    body = comment.render(comment.load_outcome(tmp_path / "results", 1), suite="evals.yaml", key="evals.yaml")
    assert "`evals.yaml`: 2 cases (2 simulated), 1 passed, 1 failed, 0 errors." in body
    assert "| **Simulated** (cases) | 1 | 1 | 0 | 50% (9–91%) |" in body
    assert "| `cancel-after-chat` (simulated) | `goal_reached` |" in body


def test_a_scripted_conversation_run_has_no_simulated_label(tmp_path, fake):
    for name in ("evals.yaml", "cases.jsonl", "agent.py"):
        shutil.copy(CONVERSATION / name, tmp_path / name)
    result = CliRunner().invoke(app, ["evals", "run", str(tmp_path / "evals.yaml")])
    assert result.exit_code == 1
    assert "simulated" not in result.output.lower()
    assert "simulator" not in json.loads((tmp_path / "results/summary.json").read_text())
    comment = comment_module()
    body = comment.render(comment.load_outcome(tmp_path / "results", 1), suite="evals.yaml", key="evals.yaml")
    assert "simulated" not in body.lower()


# ---------- the rules ----------


@pytest.mark.parametrize(
    ("messages", "found"),
    [
        # The first message is never a correction, whatever it says.
        (["That's not what I asked."], []),
        (["Where is A-1002?", "No, I meant order A-1001."], [2]),
        (["Where is A-1002?", "That’s not right, it shipped."], [2]),  # a curly apostrophe
        (["Where is A-1002?", "I already gave you the number."], [2]),
        # A repeat shares most of its content words with an earlier message, in any order.
        (["Can you cancel my order A-1002?", "Is it shipped?", "Please cancel order A-1002."], [3]),
        # Grammar and pleasantries are not content: these are two requests.
        (["Can you cancel my order?", "Can you check my order?"], []),
        # A message of one content word is never compared.
        (["Thanks!", "Thanks!"], []),
        # Whole words only: "said" inside "unsaid" is not a phrase.
        (["Hello.", "It was left unsaid."], []),
    ],
)
def test_corrections_are_found_by_fixed_rules(messages, found):
    assert fidelity.corrections(messages) == found


@pytest.mark.parametrize(
    ("messages", "reached"),
    [
        (["Where is my parcel?", "Thank you, that helps."], True),
        (["Where is my parcel?", "That's not what I asked."], False),  # ends on a correction
        (["Where is my parcel?", "Thanks, but that didn't help."], False),  # unresolved wins
        (["Can you cancel my order A-1002?", "Please cancel my order A-1002."], False),  # ends on a repeat
        (["Do you ship to Norway?"], None),
    ],
)
def test_the_outcome_rule_reads_the_last_user_message(messages, reached):
    assert fidelity.outcome_by_rule(messages) is reached


def test_a_difference_of_shares_has_newcombes_interval():
    # Newcombe (1998), Statistics in Medicine 17:873-890, Table II, example (a): 56/70 against
    # 48/80 by the hybrid score method (method 10) gives 0.0524 to 0.3339.
    found = fidelity.difference(56 / 70, wilson(56, 70), 48 / 80, wilson(48, 80))
    assert found["value"] == pytest.approx(0.2)
    assert [round(bound, 4) for bound in found["confidence_95"]] == [0.0524, 0.3339]
    assert found["reading"] == "higher"
    # Two means' symmetric t intervals combine as the root of the sum of their squared half-widths.
    low, high = fidelity.difference(3.0, (2.0, 4.0), 1.0, (-1.0, 3.0))["confidence_95"]
    assert (low, high) == pytest.approx((2 - 5**0.5, 2 + 5**0.5))
    assert fidelity.difference(0.5, (0.1, 0.9), 0.5, (0.2, 0.8))["reading"] == "within_noise"
    assert fidelity.difference(0.5, None, 0.5, (0.2, 0.8)) == {"value": 0.0, "confidence_95": None, "reading": None}


# ---------- the report ----------


def test_the_fidelity_report_compares_a_setup_with_real_conversations(tmp_path, fake):
    run_suite(example(tmp_path))
    transcripts = write_real(tmp_path)
    output = tmp_path / "fidelity.json"
    # Wide enough that no cell of the table wraps.
    result = CliRunner().invoke(
        app,
        ["evals", "fidelity", str(transcripts), "--results", str(tmp_path / "results"), "-o", str(output)],
        env={"COLUMNS": "300"},
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert report["version"] == fidelity.VERSION
    real = report["real"]
    assert real["source"]["transcripts"] == str(transcripts)
    assert real["conversations"] == [
        {"id": "real-1", "turns": 3, "corrections": [], "goal_reached": True, "outcome": "label"},
        {"id": "real-2", "turns": 3, "corrections": [2], "goal_reached": True, "outcome": "rule"},
        {"id": "real-3", "turns": 2, "corrections": [2], "goal_reached": False, "outcome": "rule"},
        {"id": "real-4", "turns": 1, "corrections": [], "goal_reached": None, "outcome": None},
    ]
    (each,) = report["reports"]
    assert each["setup"] == {
        "model": f"openai:/{MODEL}",
        "parameters": {"temperature": 0.7},
        "version": "simulated-user-v1",
        "personas": {"data": "cases.jsonl", "digest": load_suite_file(tmp_path / "evals.yaml")[0].dataset_digest},
    }
    assert (each["results"], each["cases"], each["left_out"]) == ([str(tmp_path / "results")], 2, 0)
    # The polite user asks once and is answered; the chatty one says the number is already given.
    assert each["conversations"] == [
        {"id": "cancel-clearly", "turns": 1, "corrections": [], "goal_reached": True, "outcome": "simulator"},
        {"id": "cancel-after-chat", "turns": 4, "corrections": [4], "goal_reached": False, "outcome": "simulator"},
    ]
    assert SCRIPTS["chatty"][3].startswith("I already")

    turns = each["turns"]
    assert turns["simulated"] | {"confidence_95": None} == {
        "conversations": 2,
        "turns": 5,
        "mean": 2.5,
        "confidence_95": None,
        "median": 2.5,
        "max": 4,
    }
    assert turns["simulated"]["confidence_95"] == pytest.approx(mean_interval([1, 4]))
    assert (turns["real"]["conversations"], turns["real"]["turns"], turns["real"]["mean"]) == (4, 9, 2.25)
    assert turns["real"]["confidence_95"] == pytest.approx(mean_interval([3, 3, 2, 1]))
    assert turns["difference"]["value"] == pytest.approx(0.25)
    assert turns["difference"]["reading"] == "within_noise"

    fixes = each["corrections"]
    assert {key: fixes["simulated"][key] for key in ("with_correction", "corrections", "turns", "per_turn")} == {
        "with_correction": 1,
        "corrections": 1,
        "turns": 5,
        "per_turn": 0.2,
    }
    assert (fixes["real"]["with_correction"], fixes["real"]["conversations"], fixes["real"]["rate"]) == (2, 4, 0.5)
    assert fixes["real"]["confidence_95"] == pytest.approx(wilson(2, 4))
    assert fixes["difference"]["value"] == 0.0

    goal = each["goal_reached"]
    assert goal["simulated"] | {"confidence_95": None} == {
        "conversations": 2,
        "reached": 1,
        "not_reached": 1,
        "unknown": 0,
        "rate": 0.5,
        "confidence_95": None,
        "from": {"simulator": 2},
    }
    # The unknown real conversation is left out of the share: 2 of the 3 with an outcome.
    assert goal["real"]["from"] == {"label": 1, "rule": 2}
    assert (goal["real"]["reached"], goal["real"]["conversations"], goal["real"]["unknown"]) == (2, 3, 1)
    assert goal["difference"]["value"] == pytest.approx(0.5 - 2 / 3)
    assert goal["difference"]["confidence_95"] == pytest.approx(
        fidelity.difference(0.5, wilson(1, 2), 2 / 3, wilson(2, 3))["confidence_95"]
    )

    # The printed table, and nothing anyone said in the report.
    printed = " ".join(result.output.split())
    assert f'Simulator setup: openai:/{MODEL}, parameters {{"temperature": 0.7}}' in printed
    assert "Fidelity of openai:/your-model with personas from cases.jsonl" in printed
    assert "goal reached as labelled for 1, by the outcome rule for 2, unknown for 1" in printed
    assert "1 of 2, 50% (9–91%)" in printed and "2 of 3, 67%" in printed
    assert "-17 points" in printed
    text = output.read_text()
    assert "A-1002" not in text and "parcel" not in text and "shop assistant" not in text


def test_each_simulator_setup_has_its_own_report(tmp_path, fake):
    path = example(tmp_path)
    run_suite(path, output=tmp_path / "first")
    # The same setup with each case twice: its conversations join the first folder's.
    run_suite(path, output=tmp_path / "again", repeats=2)
    path.write_text(path.read_text().replace("temperature: 0.7", "temperature: 0.2"), encoding="utf-8")
    run_suite(path, output=tmp_path / "cooler")
    report = fidelity.fidelity_report(
        [fidelity.read_simulated(tmp_path / name) for name in ("first", "again", "cooler")],
        fidelity.real_conversations(read_transcripts(write_real(tmp_path))),
        {"transcripts": "transcripts.jsonl"},
    )
    warm, cool = report["reports"]
    assert (warm["setup"]["parameters"], cool["setup"]["parameters"]) == ({"temperature": 0.7}, {"temperature": 0.2})
    assert warm["results"] == [str(tmp_path / "first"), str(tmp_path / "again")]
    assert (warm["cases"], warm["turns"]["simulated"]["conversations"]) == (4, 6)
    assert [c["id"] for c in warm["conversations"]][2:4] == ["cancel-clearly attempt 1", "cancel-clearly attempt 2"]
    # The attempts and the earlier run of a case are one persona's conversations: one group each.
    lengths = [c["turns"] for c in warm["conversations"]]
    groups = ["clearly", "chat", "clearly", "clearly", "chat", "chat"]
    assert warm["turns"]["simulated"]["confidence_95"] == pytest.approx(mean_interval(lengths, groups))
    assert cool["turns"]["simulated"]["conversations"] == 2


def test_sessions_in_the_data_folder_are_real_conversations_too(tmp_path, fake):
    for name in ("evals.yaml", "cases.jsonl", "agent.py"):
        shutil.copy(CONVERSATION / name, tmp_path / "scripted.yaml" if name == "evals.yaml" else tmp_path / name)
    run_suite(tmp_path / "scripted.yaml", output=tmp_path / "scripted")
    sessions = [
        json.loads(line)["session_id"] for line in (tmp_path / "scripted/results.jsonl").read_text().splitlines()
    ]
    run_suite(example(tmp_path))
    output = tmp_path / "fidelity.json"
    arguments = ["evals", "fidelity", "--results", str(tmp_path / "results"), "-o", str(output)]
    for session in sessions:
        arguments += ["--session", session]
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert report["real"]["source"] == {"sessions": sessions}
    assert [c["turns"] for c in report["real"]["conversations"]] == [3, 3]
    assert "2 sessions of the data folder" in " ".join(result.output.split())


def test_what_the_report_cannot_read_is_refused(tmp_path, fake):
    for name in ("evals.yaml", "cases.jsonl", "agent.py"):
        shutil.copy(CONVERSATION / name, tmp_path / name)
    run_suite(tmp_path / "evals.yaml")
    with pytest.raises(ConfigError, match="the run has no simulated users"):
        fidelity.read_simulated(tmp_path / "results")
    with pytest.raises(ConfigError, match="is not the output folder of a finished martian evals run"):
        fidelity.read_simulated(tmp_path / "missing")
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"messages": said("Hi"), "goal_reached": "yes"}) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="goal_reached must be true or false"):
        read_transcripts(bad)
    transcripts = write_real(tmp_path)
    both = ["evals", "fidelity", str(transcripts), "--session", "s", "--results", str(tmp_path / "results")]
    assert CliRunner().invoke(app, both).exit_code == 2
    assert CliRunner().invoke(app, ["evals", "fidelity", str(transcripts)]).exit_code == 2
    result = CliRunner().invoke(app, ["evals", "fidelity", str(transcripts), "--results", str(tmp_path / "results")])
    assert result.exit_code == 2 and "the run has no simulated users" in result.output
