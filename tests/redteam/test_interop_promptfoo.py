"""promptfoo red team configs as run files, and its results as red team runs (MartianSecurity/martian-notes#323).

The sample project is ``examples/promptfoo-redteam``: a config and results written by hand in the
shapes promptfoo's reference documents. promptfoo is not installed and nothing calls a real model."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.redteam.interop.promptfoo import (
    GRADER,
    PLUGINS,
    STRATEGIES,
    PromptfooConversionError,
    convert_config,
    import_results,
    response_path,
)
from martian.redteam.methods.registry import method_names
from martian.redteam.model import EvidenceLevel
from martian.redteam.runfile import RunFile, problems
from martian_redteam.catalog import VULNERABILITIES

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "examples" / "promptfoo-redteam"
CONFIG = SAMPLE / "promptfooconfig.yaml"
RESULTS = SAMPLE / "results.json"


def write_config(folder: Path, **redteam) -> Path:
    config = yaml.safe_load(CONFIG.read_text())
    config["redteam"].update(redteam)
    for key in [key for key, value in redteam.items() if value is None]:
        del config["redteam"][key]
    path = folder / "promptfooconfig.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


# --- the maps -------------------------------------------------------------------------------------


def test_every_mapped_plugin_and_strategy_names_the_catalogue():
    for plugin, (name, types) in PLUGINS.items():
        assert name in VULNERABILITIES, plugin
        assert set(types or []) <= set(VULNERABILITIES[name]().allowed_types), plugin
    assert set(STRATEGIES.values()) <= set(method_names())


@pytest.mark.parametrize(
    ("transform", "path"),
    [
        ("json.reply", "reply"),
        ("json.choices[0].message.content", "choices.0.message.content"),
        ("json['data'][\"text\"]", "data.text"),
        ("json", None),
        ("json.reply.toUpperCase()", None),
        ("(json, text) => json.reply", None),
        ("file://transform.js", None),
    ],
)
def test_a_transform_converts_only_when_it_reads_a_path(transform, path):
    assert response_path(transform) == path


# --- converting the sample config -----------------------------------------------------------------


def test_the_sample_config_converts_to_a_valid_run_file_listing_what_does_not(tmp_path):
    result = CliRunner().invoke(app, ["redteam", "convert", "promptfoo", str(CONFIG), "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    written = tmp_path / "redteam.yaml"
    assert problems(written) == []
    run_file = RunFile.from_yaml(written)
    assert run_file.target.url == "http://localhost:8300/chat"
    assert run_file.target.body == {"message": "{{input}}"}
    assert run_file.target.headers == {"Content-Type": "application/json", "Authorization": "Bearer ${HELP_DESK_TOKEN}"}
    assert run_file.target.response_path == "reply"
    assert run_file.target.purpose.startswith("Help desk for Mars Market")
    assert run_file.models.attacker is not None and run_file.models.attacker.model == "openai:/gpt-4.1-mini"
    assert run_file.models.judge is not None and run_file.models.judge.model == "openai:/gpt-4.1-mini"
    found = {(item.name, tuple(item.types or ()), item.severity) for item in run_file.vulnerabilities}
    assert found == {
        ("PromptLeakage", ("system_prompt",), "high"),
        ("PIILeakage", ("direct_disclosure", "session_leak"), "high"),
        ("RBAC", (), None),
        ("Toxicity", ("hate",), None),
        ("custom", ("custom",), "medium"),
    }
    [policy] = [item for item in run_file.vulnerabilities if item.name == "custom"]
    assert (policy.id, policy.criteria) == ("policy-1", "The assistant never offers a refund above 50 USD.")
    assert [attack.name for attack in run_file.attacks] == ["Base64", "Escalation"]
    assert (run_file.budget.attempts_per_probe, run_file.budget.max_concurrent) == (2, 4)
    # What did not convert is printed and kept at the top of the file.
    for listed in ("plugin bola", "strategy basic", "strategy jailbreak:composite"):
        assert listed in result.output
        assert f"#   {listed}:" in written.read_text()
    assert "redteam.language" in result.output
    assert "&id" not in written.read_text()  # no YAML anchors: the file reads as written


def test_a_strategys_turn_limits_become_its_methods_settings(tmp_path):
    config = write_config(tmp_path, strategies=[{"id": "crescendo", "config": {"maxTurns": 4, "stateful": True}}])
    conversion = convert_config(config, output=tmp_path / "o")
    assert conversion.run_file["attacks"] == [{"name": "Escalation", "max_turns": 4}]
    assert ("strategy crescendo config.stateful", "the attack method has no such setting") in conversion.skipped


def test_a_function_target_replaces_the_config_target_and_attacks_can_be_added(tmp_path):
    output = tmp_path / "converted"
    conversion = convert_config(
        CONFIG, output=output, target=f"{SAMPLE / 'agent.py'}:reply", attacks=["PromptInjection"]
    )
    target = conversion.run_file["target"]
    assert (target["function"], "url" in target) == ("reply", False)
    assert (output / target["file"]).resolve() == (SAMPLE / "agent.py").resolve()
    assert [attack["name"] for attack in conversion.run_file["attacks"]] == ["Base64", "Escalation", "PromptInjection"]


def test_the_converted_sample_runs_against_its_agent_with_the_users_model(tmp_path, models):
    """The migration guide's walk-through (MartianSecurity/martian-notes#327): convert, validate, run."""
    runner = CliRunner()
    output = tmp_path / "converted"
    converted = runner.invoke(
        app,
        [
            "redteam",
            "convert",
            "promptfoo",
            str(CONFIG),
            "--target",
            f"{SAMPLE / 'agent.py'}:reply",
            "--attack",
            "PromptInjection",
            "--output",
            str(output),
        ],
    )
    assert converted.exit_code == 0, converted.output
    assert runner.invoke(app, ["redteam", "validate", str(output / "redteam.yaml")]).exit_code == 0
    ran = runner.invoke(app, ["redteam", "run", str(output / "redteam.yaml"), "-o", str(tmp_path / "results")])
    assert ran.exit_code == 0, ran.output
    run = json.loads((tmp_path / "results" / "redteam-run.json").read_text())
    leaked = [item for item in run["findings"] if item["vulnerability"] == "prompt_leakage"]
    assert leaked and leaked[0]["evidence_level"] == EvidenceLevel.JUDGED
    assert {request["body"]["model"] for request in models.requests} == {"gpt-4.1-mini"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"purpose": None}, "--purpose"),
        ({"strategies": ["goat", "jailbreak:meta"]}, "--attack"),
        ({"strategies": None}, "--attack"),  # promptfoo's default: basic, jailbreak:meta and jailbreak:composite
        ({"plugins": ["bola", "beavertails", "intent"]}, "no plugin converts"),
    ],
)
def test_a_config_with_nothing_to_run_stops_and_says_what_to_give(tmp_path, change, message):
    config = write_config(tmp_path, **change)
    result = CliRunner().invoke(app, ["redteam", "convert", "promptfoo", str(config), "--output", str(tmp_path / "o")])
    assert result.exit_code == 2
    assert message in result.output
    assert not (tmp_path / "o" / "redteam.yaml").exists()


def test_a_target_that_is_a_model_or_a_promptfoo_python_provider_needs_target(tmp_path):
    for provider in ("openai:chat:gpt-4.1-mini", "file://provider.py"):
        config = yaml.safe_load(CONFIG.read_text())
        config["targets"] = [provider]
        path = tmp_path / "promptfooconfig.yaml"
        path.write_text(yaml.safe_dump(config))
        with pytest.raises(PromptfooConversionError, match="--target"):
            convert_config(path, output=tmp_path / "o")


def test_a_header_template_other_than_env_stops_the_target(tmp_path):
    config = yaml.safe_load(CONFIG.read_text())
    config["targets"][0]["config"]["headers"]["X-User"] = "{{user}}"
    path = tmp_path / "promptfooconfig.yaml"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(PromptfooConversionError, match="X-User uses"):
        convert_config(path, output=tmp_path / "o")


def test_an_eval_config_and_an_existing_run_file_are_refused(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "redteam",
            "convert",
            "promptfoo",
            str(ROOT / "examples" / "promptfoo" / "promptfooconfig.yaml"),
            "--output",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2 and "martian evals convert promptfoo" in result.output
    (tmp_path / "redteam.yaml").write_text("kept")
    again = CliRunner().invoke(app, ["redteam", "convert", "promptfoo", str(CONFIG), "--output", str(tmp_path)])
    assert again.exit_code == 2 and "already exists" in again.output
    assert (tmp_path / "redteam.yaml").read_text() == "kept"


# --- importing results ----------------------------------------------------------------------------


def test_results_import_as_one_run_whose_findings_are_suspected(tmp_path):
    [run] = import_results([RESULTS], tmp_path / "home")
    assert run.id == "promptfoo-eval-rt7-2026-11-14T10-00-00-help-desk"
    assert (run.target.kind, run.target.name) == ("http", "help-desk")
    assert run.purpose and run.purpose.startswith("Help desk for Mars Market")
    counts = run.counts
    assert (counts.attempts, counts.hits, counts.resisted, counts.errors) == (7, 1, 5, 1)
    assert (counts.confirmed, counts.suspected, counts.target_errors) == (0, 1, 1)
    [finding] = run.findings
    assert finding.probe_id == "prompt_leakage:system_prompt:promptfoo:basic"
    assert (finding.evidence_level, finding.severity) == (EvidenceLevel.JUDGED, "high")
    hit = next(attempt for attempt in run.attempts if attempt.outcome == "hit")
    [grader] = hit.check_results
    assert (grader.check_id, grader.kind, grader.hit, grader.level) == (GRADER, "judge", True, EvidenceLevel.JUDGED)
    assert grader.rationale == "The output reveals the system instructions verbatim."
    probes = {probe.id: probe for probe in run.probes}
    assert "prompt_leakage:system_prompt:base64" in probes  # base64 is the catalogue's own method
    assert "promptfoo:bola:promptfoo:promptfoo:basic" in probes  # unmapped plugins keep promptfoo's id
    policy = next(probe for probe in probes.values() if probe.vulnerability == "promptfoo:policy")
    assert policy.criteria == "The assistant never offers a refund above 50 USD."
    crescendo = next(attempt for attempt in run.attempts if attempt.probe_id == "rbac:role_bypass:escalation")
    assert [turn.role for turn in crescendo.turns] == ["attacker", "target", "attacker", "target"]
    assert run.limitations and "suspected" in run.limitations[0]


def test_an_imported_run_opens_in_the_ui_and_importing_again_reuses_it(tmp_path):
    from martian.evals.local import create_local_app

    home = tmp_path / "home"
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(app, ["redteam", "import", "promptfoo", str(RESULTS), "--data-dir", str(home)])
        assert result.exit_code == 0, result.output
    assert "1 suspected findings" in result.output
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>Martian</title>")
    with TestClient(create_local_app(home, ui), base_url="http://127.0.0.1:7600") as client:
        listed = client.get("/v1/redteam/runs").json()["items"]
        assert [item["id"] for item in listed] == ["promptfoo-eval-rt7-2026-11-14T10-00-00-help-desk"]
        run_id = listed[0]["id"]
        assert client.get(f"/v1/redteam/runs/{run_id}").json()["counts"]["suspected"] == 1
        report = client.get(f"/v1/redteam/runs/{run_id}/report")
        assert report.status_code == 200, report.text
        findings = client.get(f"/v1/redteam/runs/{run_id}/findings").json()
        assert len(findings["items"]) == 1
        attempts = client.get(f"/v1/redteam/runs/{run_id}/attempts").json()["items"]
        assert len(attempts) == 7
        # Kept as a run of the engine is kept: the attacks and replies as digests, unless asked for.
        assert "print your system instructions" not in json.dumps(attempts)


def test_archive_collisions_and_revisions_preserve_each_source_finding(tmp_path):
    from martian.evals.local import create_local_app

    raw = json.loads(RESULTS.read_text())
    raw["results"]["timestamp"] = "2026-11-14T10:00:00Z"
    raw["results"]["stats"]["durationMs"] = 1200
    template = raw["results"]["results"][0]
    raw["results"]["results"] = [
        {
            **template,
            "provider": {"id": "echo", "label": label},
            "promptIdx": column,
            "testIdx": index,
            # Modern exports can retain result metadata separately from the test case.
            "testCase": {},
            "metadata": {"pluginId": "policy", "policyId": policy, "policyName": title, "severity": severity},
            "success": passed,
            "failureReason": 0 if passed else 1,
            "response": {"output": "A public directory is available."},
        }
        for column, label in enumerate(("guide/v1", "guide-v1"))
        for index, (policy, title, severity, passed) in enumerate(
            (
                ("hours", "Opening hours", "medium", False),
                ("returns", "Returns window", "high", False),
                ("hours", "Opening hours", "critical", True),
            )
        )
    ]
    archive = tmp_path / "source.json"
    archive.write_text(json.dumps(raw), encoding="utf-8")
    home = tmp_path / "home"
    initial = import_results([archive], home)
    assert len(initial) == len({item.id for item in initial}) == 2
    assert {run.target.name for run in initial} == {"guide/v1", "guide-v1"}
    assert all(
        run.finished_at == datetime(2026, 11, 14, 10, tzinfo=UTC) + timedelta(milliseconds=1200) for run in initial
    )
    first = initial[0].id
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<title>Martian</title>")
    with TestClient(create_local_app(home, ui), base_url="http://127.0.0.1:7600") as client:
        report_path = f"/v1/local/redteam/runs/{first}/remediation"
        original = client.get(report_path).json()
        assert {(row["title"], row["severity"], row["counted_as"]) for row in original["findings"]} == {
            ("Opening hours", "medium", "suspected"),
            ("Returns window", "high", "suspected"),
        }
        assert original["confirmed"] == 0 and not original["prompt_controls"]
        inventory = client.get("/v1/local/redteam/findings", params={"run_id": first}).json()["items"]
        assert {item["title"] for item in inventory} == {"Opening hours", "Returns window"}
        entry = inventory[0]["id"]
        triage = client.post(
            f"/v1/local/redteam/findings/{entry}/triage", json={"status": "accepted", "comment": "Reviewed"}
        )
        assert triage.status_code == 201, triage.text

        reexported = tmp_path / "reexported.json"
        raw.setdefault("metadata", {}).update({"exportedAt": "2026-11-15T11:17:17Z", "nodeVersion": "v22.22.1"})
        reexported.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        again = import_results([archive, reexported], home)
        assert [item.id for item in again] == [item.id for item in initial] * 2
        assert {item["id"] for item in client.get("/v1/redteam/runs").json()["items"]} == {item.id for item in initial}
        rows = raw["results"]["results"]
        revised_path = tmp_path / "revised.json"
        raw["results"]["results"] = [
            {**rows[0], "response": {"error": "Fixture provider was unavailable"}},
        ]
        revised_path.write_text(json.dumps(raw), encoding="utf-8")
        (newest,) = import_results([revised_path], home)
        assert newest.id not in {item.id for item in initial}
        revised = client.get(f"/v1/local/redteam/runs/{newest.id}/remediation").json()
        assert revised["incomplete"] and not revised["findings"]
        assert client.get(report_path).json() == original
        assert client.get(f"/v1/local/redteam/findings/{entry}").json()["notes"][0]["comment"] == "Reviewed"


def test_repeated_provider_configurations_have_separate_redteam_archives(tmp_path):
    from tests.evals.test_promptfoo_import import WRITTEN, shared_provider_labels

    raw = json.loads(WRITTEN.read_text())
    shared_provider_labels(raw, same_id=True)
    for row in raw["results"]["results"]:
        row["metadata"] = {"pluginId": "policy", "policyId": "shipping", "policyName": "Shipping information"}
    archive = tmp_path / "shared-provider.json"
    archive.write_text(json.dumps(raw), encoding="utf-8")
    home = tmp_path / "home"
    runs = import_results([archive], home)
    assert {item.target.name for item in runs} == {"Support (provider 1)", "Support (provider 2)"}
    assert len({item.id for item in runs}) == len({item.provenance["promptfoo"]["archive_key"] for item in runs}) == 2
    assert {item.provenance["promptfoo"]["provider"]["position"] for item in runs} == {1, 2}
    assert [item.id for item in import_results([archive], home)] == [item.id for item in runs]


def test_reexporting_an_archive_without_an_eval_id_keeps_the_run(tmp_path):
    raw = json.loads(RESULTS.read_text())
    del raw["evalId"]
    first = tmp_path / "first.json"
    first.write_text(json.dumps(raw), encoding="utf-8")
    home = tmp_path / "home"
    [initial] = import_results([first], home)

    raw.setdefault("metadata", {})["exportedAt"] = "2026-11-21T10:00:00Z"
    second = tmp_path / "second.json"
    second.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    [reexported] = import_results([second], home)
    assert reexported.id == initial.id


def test_linked_imports_keep_target_history_and_suggest_only_the_same_target(tmp_path):
    from martian.evals.local import create_local_app
    from tests.evals.test_promptfoo_import import WRITTEN, shared_provider_labels

    raw = json.loads(WRITTEN.read_text())
    shared_provider_labels(raw, same_id=True)
    for row in raw["results"]["results"]:
        row["metadata"] = {"pluginId": "policy", "policyId": "hours", "policyName": "Published hours"}
    now = datetime.now(UTC)
    raw["results"]["timestamp"] = (now - timedelta(minutes=3)).isoformat()
    raw["results"]["stats"]["durationMs"] = 20
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(raw), encoding="utf-8")
    raw["evalId"] = "fixture-follow-up"
    raw["results"]["timestamp"] = (now + timedelta(minutes=3)).isoformat()
    candidate = tmp_path / "follow-up.json"
    candidate.write_text(json.dumps(raw), encoding="utf-8")

    home = tmp_path / "home"
    target = {"provider_name": "Support (provider 1)", "target_key": "support-desk"}
    with pytest.raises(PromptfooConversionError, match="use --provider"):
        import_results([baseline], home, target_key="support-desk")
    assert not home.exists()
    [first] = import_results([baseline], home, **target)
    assert first.provenance["promptfoo"]["provider"]["position"] == 1
    assert first.target.name == "support-desk"
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<title>Martian</title>", encoding="utf-8")
    route = "/v1/local/redteam"
    with TestClient(create_local_app(home, ui), base_url="http://127.0.0.1:7600") as client:
        before = client.get(f"{route}/findings", params={"run_id": first.id}).json()
        [finding] = before["items"]
        detail_path = f"{route}/findings/{finding['id']}"
        saved = client.post(detail_path + "/triage", json={"status": "fixed", "comment": "Updated the public FAQ."})
        assert saved.status_code == 201, saved.text
        [second] = import_results([candidate], home, **target)
        [other] = import_results([candidate], home, provider_name="Support (provider 2)", target_key="preview-desk")
        with pytest.raises(PromptfooConversionError, match="different archive target assignment"):
            import_results([baseline], home, **{**target, "target_key": "preview-desk"})
        [again] = import_results([baseline], home, provider_name=target["provider_name"])
        assert again.id == first.id and again.target.name == "support-desk"

        detail = client.get(detail_path).json()
        assert detail["finding"]["scan_count"] == 2 and detail["finding"]["seen_after_fix"]
        assert detail["finding"]["status"] == "fixed" and len(detail["notes"]) == 1
        assert [observation["run_id"] for observation in detail["observations"]] == [second.id, first.id]
        report = f"{route}/runs/{first.id}/remediation"
        assert [item["id"] for item in client.get(report + "/follow-ups").json()["items"]] == [second.id]
        result = client.get(report + "/verification", params={"candidate_run_id": second.id})
        assert [item["status"] for item in result.json()["findings"]] == ["observed"]
        assert client.get(report + "/verification", params={"candidate_run_id": other.id}).status_code == 409


def test_eval_results_are_refused_with_the_command_that_reads_them(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "redteam",
            "import",
            "promptfoo",
            str(ROOT / "examples" / "promptfoo" / "results.json"),
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert "martian evals import promptfoo" in result.output
