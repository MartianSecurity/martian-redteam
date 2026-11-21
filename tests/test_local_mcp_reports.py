"""The stdio entry point exposes project-scoped saved results and static eval tools."""

import asyncio
import json
import sys
from uuid import uuid4

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from martian import local_mcp  # noqa: E402
from martian.evals.model import CaseResult, Counts, Run  # noqa: E402
from martian.evals.preview import CheckInput, CheckPreview, CheckSample  # noqa: E402
from martian.evals.records.sql import SqlRecordStore  # noqa: E402
from martian.evals.server.principal import SINGLE_USER  # noqa: E402
from martian.local_mcp import _read_only_store  # noqa: E402
from martian.redteam.findings import TriageChange, record_triage  # noqa: E402
from martian.redteam.store import RedteamStore  # noqa: E402
from martian.redteam.verified_fixes import VerifiedFixRequest, record_verified_fix  # noqa: E402
from tests.redteam.test_remediation_verification import saved_scans  # noqa: E402


async def _seed(home):
    home.mkdir()
    records = SqlRecordStore(f"sqlite:///{home / 'martian.db'}")
    project = "library"
    try:
        local = Run(
            id="library-eval",
            project_id=project,
            source="local",
            state="completed",
            counts=Counts(cases=2, passed=1, failed=1),
        )
        await records.create_run(SINGLE_USER, local)
        await records.create_run(
            SINGLE_USER, local.model_copy(update={"id": "private-project-eval", "project_id": "other"})
        )
        await records.put_results(
            SINGLE_USER,
            project,
            [
                CaseResult(run_id=local.id, case_id="hours", position=0, status="passed", output="private-case-reply"),
                CaseResult(
                    run_id=local.id,
                    case_id="catalog",
                    position=1,
                    status="failed",
                    inputs={"question": "private-case-input"},
                ),
            ],
        )
        baseline, clean, _ = saved_scans()
        scans = RedteamStore(records)
        for run in (baseline, clean):
            await scans.save(run.model_copy(update={"project_id": project}), principal=SINGLE_USER)
        await scans.save(
            baseline.model_copy(update={"id": "private-project-scan", "project_id": "other"}), principal=SINGLE_USER
        )
        fixed = await record_verified_fix(
            records,
            SINGLE_USER,
            project,
            baseline.id,
            VerifiedFixRequest(finding_id=baseline.findings[0].id, candidate_run_id=clean.id, request_id=uuid4()),
        )
        await record_triage(
            records, SINGLE_USER, project, fixed.finding_id, TriageChange(comment="private-review-text")
        )
        return fixed.finding_id
    finally:
        records.close()


async def test_stdio_reads_saved_runs_and_reports_without_exposing_private_content(tmp_path):
    home = tmp_path / "library #1"
    finding_id = await _seed(home)
    database = home / "martian.db"
    before = database.read_bytes()
    server = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from martian.cli import app; app()", "mcp", "--data-dir", str(home), "--project-id", "library"],
    )
    async with (
        stdio_client(server) as (reader, writer),
        ClientSession(reader, writer, read_timeout_seconds=15) as client,
    ):
        await client.initialize()
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "list_runs",
            "get_evaluation",
            "list_findings",
            "get_finding",
            "get_remediation",
            "compare_remediation",
            "validate_suite",
            "run_check",
        }
        assert all(tool.annotations.read_only_hint and tool.output_schema for tool in tools)
        assert all("project_id" not in tool.input_schema["properties"] for tool in tools)

        async def read(name, arguments=None):
            response = await client.call_tool(name, arguments or {})
            assert not response.is_error, response.content
            serialized = json.dumps(response.structured_content)
            assert "private-" not in serialized
            return response.structured_content

        listing = await read("list_runs")
        assert [run["id"] for run in listing["items"]] == ["library-eval"]
        report = await read("get_evaluation", {"run_id": "library-eval", "limit": 1})
        assert report["run"]["counts"]["failed"] == 1 and report["cases"]["items"][0]["status"] == "passed"
        next_page = await read("get_evaluation", {"run_id": "library-eval", "offset": report["cases"]["next_cursor"]})
        assert next_page["cases"] == {
            "items": [{"case_id": "catalog", "position": 1, "status": "failed", "latency_ms": None}],
            "next_cursor": None,
        }

        grouped = await read("list_findings", {"status": "fixed", "evidence": "confirmed"})
        assert [finding["id"] for finding in grouped["items"]] == [finding_id]
        finding = await read("get_finding", {"finding_id": finding_id})
        assert finding["observations"]["items"][0]["run_id"] == "baseline"
        assert finding["reviews"]["items"][0]["verified_fix"]["follow_up_run"] == "follow-up"
        remediation = await read("get_remediation", {"run_id": "baseline", "limit": 1})
        assert remediation["summary"]["confirmed"] == 1 and remediation["findings"]["next_cursor"] == "1"
        assert remediation["actions"][0]["steps"]
        compared = await read("compare_remediation", {"baseline_run": "baseline", "follow_up_run": "follow-up"})
        assert compared["coverage"]["matched_probes"] == 2
        assert {row["status"] for row in compared["findings"]["items"]} == {"not_observed"}

        for name, arguments in (
            ("get_evaluation", {"run_id": "private-project-eval"}),
            ("get_remediation", {"run_id": "private-project-scan"}),
            ("compare_remediation", {"baseline_run": "baseline", "follow_up_run": "baseline"}),
            ("list_runs", {"limit": 101}),
        ):
            refusal = await client.call_tool(name, arguments)
            assert refusal.is_error and refusal.structured_content is None
    assert database.read_bytes() == before


async def test_stdio_previews_suite_and_checks_without_a_store(tmp_path):
    home = tmp_path / "unused-store"
    server = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from martian.cli import app; app()", "mcp", "--preview-only", "--data-dir", str(home)],
    )
    async with (
        stdio_client(server) as (reader, writer),
        ClientSession(reader, writer, read_timeout_seconds=15) as client,
    ):
        await client.initialize()
        assert {tool.name for tool in (await client.list_tools()).tools} == {"validate_suite", "run_check"}

        async def call(name, **arguments):
            response = await client.call_tool(name, arguments)
            assert not response.is_error, response.content
            return response.structured_content

        suite = json.dumps({"data": "client/library.jsonl", "checks": [{"type": "contains", "values": ["library"]}]})
        case = {"id": "hours", "input": "When does the library open?", "output": "The library opens at nine."}
        config_only = await call("validate_suite", suite_yaml=suite)
        assert config_only["valid"] and config_only["cases"] is None
        valid = await call("validate_suite", suite_yaml=suite, case_data={"text": json.dumps(case)})
        assert valid == {"valid": True, "cases": 1, "checks": 1, "target": "stored", "error": None}
        missing_output = await call(
            "validate_suite", suite_yaml=suite, case_data={"text": json.dumps({"input": case["input"]})}
        )
        assert not missing_output["valid"] and "output" in missing_output["error"]

        for output, verdict in ((case["output"], "passed"), ("Monday to Friday.", "failed")):
            checked = await call(
                "run_check",
                kind="contains",
                sample={"output": output, "expectations": {"branch": "library"}},
                options={"expectation": "branch"},
            )
            assert checked["verdict"] == verdict and checked["reason"] and not checked["error"]
        missing = await call("run_check", kind="cost", sample={"output": case["output"]}, options={"max_tokens": 20})
        assert missing["verdict"] == "error" and "no token usage" in missing["error"]
        measured = await call(
            "run_check", kind="cost", sample={"output": case["output"], "total_tokens": 20}, options={"max_tokens": 20}
        )
        assert measured["verdict"] == "passed"

        custom = await call(
            "run_check", kind="python", sample={"output": case["output"]}, options={"file": "scorers.py"}
        )
        assert custom["verdict"] == "error" and "deterministic check" in custom["error"]
        reply = '{  "opens": 9}'
        exact = await call("run_check", kind="equals", sample={"output": reply}, options={"values": [reply]})
        assert exact["verdict"] == "passed"
    assert not home.exists()


async def test_timed_out_check_process_is_reaped(monkeypatch):
    create_process = asyncio.create_subprocess_exec
    process = None

    async def sleep_process(*_args, **kwargs):
        nonlocal process
        process = await create_process(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        return process

    monkeypatch.setattr(local_mcp.asyncio, "create_subprocess_exec", sleep_process)
    monkeypatch.setattr(local_mcp, "_PREVIEW_TIMEOUT", 0.1)
    with pytest.raises(ToolError, match="time limit"):
        await local_mcp._preview(CheckInput(kind="contains", sample=CheckSample(output="library")), CheckPreview)
    assert process is not None and process.returncode is not None


async def test_check_preview_refuses_nonfinite_output_instead_of_changing_it():
    request = CheckInput(kind="is_json", sample=CheckSample(output={"quantity": float("inf")}))
    with pytest.raises(ToolError, match="valid UTF-8 JSON"):
        await local_mcp._preview(request, CheckPreview)


async def test_local_mcp_connection_rejects_database_writes(tmp_path):
    home = tmp_path / "data #1"
    await _seed(home)
    records = _read_only_store(home)
    try:
        assert (await records.list_runs(SINGLE_USER, "library")).items[0].id == "library-eval"
        with pytest.raises(OperationalError, match="readonly database"):
            await records.create_run(SINGLE_USER, Run(id="new", project_id="library", source="local", state="queued"))
    finally:
        records.close()
