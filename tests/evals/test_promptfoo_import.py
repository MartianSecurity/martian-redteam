"""promptfoo results imported as runs: one per provider, opened, compared and scored through the
local edition with every assertion as the file grades it.

``examples/promptfoo/results.json`` is written by hand for ``promptfooconfig.yaml`` there, in the
shape promptfoo's configuration reference gives its output file (``EvaluateSummary`` version 3);
it is not promptfoo's own test data. ``tests/evals/promptfoo/results.json`` is: promptfoo 0.125.1
wrote it for the config there, two Python providers answering from a table.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.evals.local import create_local_app
from martian.evals.promptfoo_import import (
    PromptfooGrading,
    PromptfooResultsError,
    case_id,
    import_promptfoo_results,
    promptfoo_runs,
    promptfoo_scores,
    read_promptfoo_results,
)
from tests.evals.evals_contract import validate_response

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "promptfoo"
RESULTS = EXAMPLE / "results.json"
# promptfoo's own output: each provider has its own prompt columns, so bot-v1's rows have
# promptIdx 0 and 1 and bot-v2's 2 and 3 for the same two prompts.
WRITTEN = Path(__file__).resolve().parent / "promptfoo" / "results.json"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))


def local_ui(home: Path) -> TestClient:
    """The local edition on the data folder the results were imported into, as `martian ui` serves it."""
    ui = home.parent / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return TestClient(create_local_app(home, ui), base_url="http://127.0.0.1:7600")


def items_of(client: TestClient, run_id: str) -> dict[str, dict]:
    detail = client.get(f"/v1/evals/{run_id}").json()
    validate_response("get", "/v1/evals/{run_id}", detail)
    return {item["dataset_item_id"]: item for item in detail["items"]}


def scores_of(client: TestClient, run_id: str) -> dict[str, dict[str, dict]]:
    """The run's scores by case id and scorer, as the workbench serves them."""
    items = {item["id"]: case for case, item in items_of(client, run_id).items()}
    found: dict[str, dict[str, dict]] = {case: {} for case in items.values()}
    for assessment in client.get(f"/v1/evals/{run_id}/workbench").json()["assessments"]:
        found[items[assessment["item_id"]]][assessment["name"]] = assessment
    return found


def file_grades(provider: str) -> dict[str, dict[str, tuple[bool, float]]]:
    """Each row's assertions as the file grades them, pass and score, by case id and type."""
    rows = json.loads(RESULTS.read_text(encoding="utf-8"))["results"]["results"]
    return {
        f"test {row['testIdx'] + 1}": {
            part["assertion"]["type"]: (part["pass"], part["score"])
            for part in (row["gradingResult"] or {}).get("componentResults") or []
        }
        for row in rows
        if row["provider"]["label"] == provider
    }


def test_each_provider_is_a_run_with_its_cases_outputs_and_verdicts(tmp_path):
    home = tmp_path / "evals-home"
    mini, agent = import_promptfoo_results([RESULTS], home)
    client = local_ui(home)

    runs = client.get("/v1/evals").json()
    validate_response("get", "/v1/evals", runs)
    assert {(r["id"], r["run_code"], r["state"], r["source"], r["dataset_name"]) for r in runs} == {
        (mini.id, "promptfoo-mini-1", "completed", "local", "Travel assistant"),
        (agent.id, "promptfoo-agent-1", "completed", "local", "Travel assistant"),
    }
    by_id = {r["id"]: r for r in runs}
    assert [by_id[mini.id][key] for key in ("planned_count", "passed", "failed", "errored")] == [5, 1, 3, 1]
    assert [by_id[agent.id][key] for key in ("planned_count", "passed", "failed", "errored")] == [5, 4, 1, 0]

    items = items_of(client, mini.id)
    assert [(case, item["status"]) for case, item in items.items()] == [
        ("test 1", "passed"),
        ("test 2", "failed"),
        ("test 3", "error"),
        ("test 4", "failed"),
        ("test 5", "failed"),
    ]
    first = items["test 1"]
    assert first["prompt"] == (
        "You are a travel assistant. Answer Sam in one sentence. Question: What is the capital of France?"
    )
    assert (first["output"], first["latency_ms"]) == ("The capital of France is Paris.", 812)
    assert [assertion["type"] for assertion in json.loads(first["expected"])["assert"]] == [
        "not-icontains",
        "not-regex",
        "latency",
        "starts-with",
        "icontains",
        "llm-rubric",
    ]
    assert items["test 3"]["error"].startswith("API error: 429")

    # The provider is the run's target, in its provenance and as each trace's model.
    source = client.get(f"/v1/evals/{mini.id}/provenance").json()["admission"]["promptfoo"]
    assert (source["provider"], source["eval_id"], source["results_version"], source["results"]) == (
        {"id": "openai:gpt-4.1-mini", "label": "mini"},
        "eval-k3Q-2026-11-14T10:00:00",
        3,
        str(RESULTS),
    )
    assert len(source["results_sha256"]) == 64 and source["stats"]["durationMs"] == 31840
    assert [prompt["label"] for prompt in source["prompts"]] == ["answer"]
    trace = client.get(f"/v1/evals/{mini.id}/items/{first['id']}/trace").json()
    assert [(s["name"], s["type"]) for s in trace["spans"]] == [("mini", "LLM")]
    assert trace["spans"][0]["attributes"]["gen_ai.request.model"] == "openai:gpt-4.1-mini"
    assert {a["assessment_name"] for a in trace["annotations"]} == set(file_grades("mini")["test 1"])


@pytest.mark.parametrize("provider", ["mini", "agent"])
def test_every_assertion_is_a_score_as_the_file_grades_it(tmp_path, provider):
    home = tmp_path / "evals-home"
    runs = {run.provenance["promptfoo"]["provider"]["label"]: run for run in import_promptfoo_results([RESULTS], home)}
    found = scores_of(local_ui(home), runs[provider].id)
    # Each assertion's value is its pass, as a suite file's checks record theirs, and promptfoo's
    # score stays with it; a row the provider failed (mini's test 3) has none.
    assert {
        case: {name: (a["value"], a["provenance"]["promptfoo.score"]) for name, a in scores.items()}
        for case, scores in found.items()
    } == file_grades(provider)
    if provider == "agent":
        assert found["test 3"]["similar"]["provenance"]["promptfoo.score"] == 0.91
    judged = {a["source"] for scores in found.values() for name, a in scores.items() if name == "llm-rubric"}
    coded = {a["source"] for scores in found.values() for name, a in scores.items() if name != "llm-rubric"}
    assert (judged, coded) == ({"LLM_JUDGE"}, {"CODE"})
    failed = found["test 3" if provider == "agent" else "test 2"]["starts-with"]
    assert (failed["value"], failed["rationale"]) == (False, 'Expected output to start with "The capital of"')
    assert found["test 1"]["icontains"]["provenance"]["promptfoo.assertion"] == {
        "type": "icontains",
        "value": "paris",
        "weight": 2,
    }


def test_two_providers_of_one_file_compare_case_by_case(tmp_path):
    home = tmp_path / "evals-home"
    mini, agent = import_promptfoo_results([RESULTS], home)
    compared = local_ui(home).get("/v1/evals/compare", params={"baseline_id": mini.id, "candidate_id": agent.id})
    body = compared.json()
    assert (body["compatible"], body["coverage"]["paired"]) == (True, 5)
    assert {row["dataset_item_id"]: row["change"] for row in body["items"]} == {
        "test 1": "unchanged",
        "test 2": "improved",
        "test 3": "not_comparable",
        "test 4": "improved",
        "test 5": "improved",
    }


def test_two_providers_of_a_file_promptfoo_wrote_compare_case_by_case(tmp_path):
    home = tmp_path / "evals-home"
    v1, v2 = import_promptfoo_results([WRITTEN], home)
    client = local_ui(home)
    assert (v1.code, v2.code) == ("promptfoo-bot-v1-1", "promptfoo-bot-v2-1")
    cases = [f"test {test}, prompt {prompt}" for test in (1, 2, 3) for prompt in (1, 2)]
    assert list(items_of(client, v1.id)) == list(items_of(client, v2.id)) == cases
    compared = client.get("/v1/evals/compare", params={"baseline_id": v1.id, "candidate_id": v2.id}).json()
    assert (compared["compatible"], compared["coverage"]["paired"]) == (True, 6)
    # bot-v2 answers the opening hours with 9am, and bot-v1 has no reply to the card question.
    assert {row["dataset_item_id"]: row["change"] for row in compared["items"]} == {
        "test 1, prompt 1": "improved",
        "test 1, prompt 2": "improved",
        "test 2, prompt 1": "unchanged",
        "test 2, prompt 2": "unchanged",
        "test 3, prompt 1": "not_comparable",
        "test 3, prompt 2": "not_comparable",
    }


def provider_prompts(path: Path) -> list[dict[str, str]]:
    """Each provider's cases as an import gives them: case id and rendered prompt."""
    runs = promptfoo_runs(read_promptfoo_results(path), codes={"bot-v1": "v1", "bot-v2": "v2"})
    return [{case.result.case_id: case.result.inputs["prompt"] for case in run.cases} for run in runs]


def one_label(found: dict) -> None:
    """Two prompts with one label: promptfoo gives them one promptId."""
    for row in found["results"]["results"]:
        row["promptId"] = found["results"]["results"][0]["promptId"]


def second_prompt_for_v1(found: dict) -> None:
    """bot-v1 limited to the second prompt (promptfoo's providerPromptMap): one column fewer."""
    rows = found["results"]["results"]
    rows[:] = [row for row in rows if (row["provider"]["label"], row["promptIdx"]) != ("bot-v1", 0)]
    for row in rows:
        row["promptIdx"] -= 1


@pytest.mark.parametrize("change", [None, one_label, second_prompt_for_v1], ids=["as written", "one label", "map"])
def test_a_case_id_names_the_same_prompt_under_every_provider(tmp_path, change):
    v1, v2 = provider_prompts(WRITTEN if change is None else changed(tmp_path, change, WRITTEN))
    # Every bot-v1 case pairs with bot-v2's case of the same id, on the same prompt, and bot-v2's
    # six cases stay six.
    assert v1.keys() <= v2.keys() and all(v1[case] == v2[case] for case in v1)
    assert len(set(v2.values())) == 6 and len(v1) == (3 if change is second_prompt_for_v1 else 6)


def test_assertion_scores_name_repeats_and_sets_and_keep_grader_errors():
    grading = PromptfooGrading.model_validate(
        {
            "pass": False,
            "score": 0.5,
            "reason": "",
            "componentResults": [
                {"pass": True, "score": 1, "reason": "ok", "assertion": {"type": "icontains", "value": "a"}},
                {"pass": False, "reason": "missing", "assertion": {"type": "icontains", "value": "b"}},
                {
                    "pass": True,
                    "score": 1,
                    "reason": "set passed",
                    "assertion": {"type": "assert-set"},
                    "componentResults": [{"pass": True, "score": 1, "reason": "", "assertion": {"type": "regex"}}],
                },
                {
                    "pass": False,
                    "score": 0,
                    "reason": "grader timed out",
                    "assertion": {"type": "llm-rubric", "provider": "openai:gpt-4.1"},
                    "metadata": {"graderError": True},
                },
                {
                    "pass": True,
                    "score": 1,
                    "reason": "",
                    "assertion": {
                        "type": "factuality",
                        "provider": {"id": "openai:gpt-4.1", "config": {"apiKey": "placeholder-key"}},
                    },
                },
            ],
        }
    )
    scores = {score.scorer: score for score in promptfoo_scores(grading, run_id="r", case_id="test 1")}
    assert {name: (s.value, s.passed, s.metadata["promptfoo.score"], s.error) for name, s in scores.items()} == {
        "icontains": (True, True, 1, ""),
        # promptfoo leaves out a failed grade's score: it is 0.
        "icontains (2)": (False, False, 0, ""),
        "assert-set": (True, True, 1, ""),
        "assert-set/regex": (True, True, 1, ""),
        "llm-rubric": (None, None, 0, "grader timed out"),
        "factuality": (True, True, 1, ""),
    }
    assert (scores["llm-rubric"].source, scores["llm-rubric"].source_id) == ("llm_judge", "openai:gpt-4.1")
    # A grading provider given with its settings keeps only its id: they may hold a key.
    assert scores["factuality"].metadata["promptfoo.assertion"] == {"type": "factuality", "provider": "openai:gpt-4.1"}
    assert scores["factuality"].source_id == "openai:gpt-4.1"
    # A row with one assertion and no componentResults is graded by that assertion.
    single = PromptfooGrading.model_validate({"pass": True, "score": 1, "reason": "", "assertion": {"type": "equals"}})
    assert [s.scorer for s in promptfoo_scores(single, run_id="r", case_id="c")] == ["equals"]
    assert promptfoo_scores(None, run_id="r", case_id="c") == []


def test_case_ids_name_the_prompt_and_the_repeat_only_when_there_are_several():
    assert case_id(0, 0, prompts=1) == "test 1"
    assert case_id(2, 1, prompts=2) == "test 3, prompt 2"
    assert case_id(0, 0, prompts=1, repeat=1) == "test 1 (repeat 2)"


def text_file(tmp_path: Path, text: str, name: str = "changed.json") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def changed(tmp_path: Path, change, source: Path = RESULTS) -> Path:
    found = json.loads(source.read_text(encoding="utf-8"))
    change(found)
    return text_file(tmp_path, json.dumps(found))


def relabel(found: dict) -> None:
    for row in found["results"]["results"]:
        row["provider"]["label"] = "same"


@pytest.mark.parametrize(
    ("make", "problem"),
    [
        (lambda tmp: text_file(tmp, "{}", "results.yaml"), "ends in .json"),
        (lambda tmp: text_file(tmp, "not json"), "it is not JSON"),
        (lambda tmp: text_file(tmp, '{"version": 3, "results": []}'), "has no results object"),
        (lambda tmp: changed(tmp, lambda f: f["results"].update(version=2)), "results version 2"),
        (lambda tmp: changed(tmp, lambda f: f["results"].update(results=[])), "has no results"),
        (lambda tmp: changed(tmp, lambda f: f["results"]["results"][0].pop("provider")), "provider"),
        (lambda tmp: changed(tmp, lambda f: f["results"]["results"].append(f["results"]["results"][0])), "twice"),
        (lambda tmp: changed(tmp, relabel), "the same label"),
    ],
)
def test_files_that_are_not_promptfoo_results_are_refused_before_anything_is_written(tmp_path, make, problem):
    path = make(tmp_path)
    with pytest.raises(PromptfooResultsError, match=problem):
        import_promptfoo_results([RESULTS, path], tmp_path / "evals-home")
    assert not (tmp_path / "evals-home").exists()


def test_the_command_imports_each_provider_into_the_data_folder_it_names(tmp_path):
    folder = tmp_path / "elsewhere"
    result = CliRunner().invoke(app, ["evals", "import", "promptfoo", str(RESULTS), "--data-dir", str(folder)])
    assert result.exit_code == 0, result.output
    assert (
        "results.json: provider mini, run promptfoo-mini-1, 5 cases: 1 passed, 3 failed, 1 errors\n"
        "results.json: provider agent, run promptfoo-agent-1, 5 cases: 4 passed, 1 failed, 0 errors\n"
        f"Open them in the local Evals UI: martian ui --data-dir {folder}\n"
    ) in result.output
    again = CliRunner().invoke(app, ["evals", "import", "promptfoo", str(RESULTS), "--data-dir", str(folder)])
    assert "run promptfoo-mini-2" in again.output and "run promptfoo-agent-2" in again.output
    refused = CliRunner().invoke(app, ["evals", "import", "promptfoo", str(tmp_path / "missing.json")])
    assert refused.exit_code == 2 and "Import failed: cannot read" in refused.output
    assert not (tmp_path / "evals-home").exists()
