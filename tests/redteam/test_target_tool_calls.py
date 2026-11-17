"""Structured tool calls in a red team target's reply (martian-notes#243)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from martian.evals.runner import TargetCallError
from martian.redteam.model import ToolCall
from martian.redteam.model.maps import from_risk_assessment, to_risk_assessment
from martian.redteam.runfile import Target as TargetBlock
from martian.redteam.targets import TargetReply, target_from_run_file
from martian.redteam.targets.reply import reply_from, tool_calls
from martian_redteam.catalog import PromptInjection, PromptLeakage
from martian_redteam.client import Client
from martian_redteam.config import Settings
from martian_redteam.orchestrator import a_red_team
from martian_redteam.report import render_markdown
from martian_redteam.types import ToolCallRecord, Turn
from tests.redteam.target_servers import json_server

PURPOSE = "A refund assistant that looks up orders and issues refunds."


def test_every_documented_form_of_a_tool_call_is_read():
    calls = tool_calls(
        [
            "lookup_order",
            {"name": "issue_refund", "arguments": {"amount": 80}, "executed": True, "result": {"ok": True}},
            {"id": "call_1", "type": "function", "function": {"name": "send_email", "arguments": '{"to": "a@b"}'}},
            {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "x"}},
            {"name": "delete", "args": {"id": 1}, "executed": False, "call_id": 7},
        ]
    )
    assert calls == [
        ToolCall(name="lookup_order"),
        ToolCall(name="issue_refund", arguments={"amount": 80}, executed=True, result='{"ok": true}'),
        ToolCall(name="send_email", arguments={"to": "a@b"}, call_id="call_1"),
        ToolCall(name="search", arguments={"q": "x"}, call_id="toolu_1"),
        ToolCall(name="delete", arguments={"id": 1}, call_id="7", executed=False),
    ]
    assert tool_calls(None) == []


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ({"name": "x"}, "tool_calls is not a list"),
        ([{"arguments": {}}], r"tool_calls\[0\] has no name"),
        ([{"name": "x", "arguments": "not json"}], "the arguments are not JSON"),
        ([{"name": "x", "arguments": [1, 2]}], "the arguments are not an object"),
        ([{"name": "x", "executed": "yes"}], "executed is true, false or left out"),
        ([42], r"tool_calls\[0\] is not an object or a tool's name"),
    ],
)
def test_a_tool_call_that_cannot_be_read_is_the_targets_error(value: Any, reason: str):
    with pytest.raises(TargetCallError, match=reason):
        tool_calls(value)


def test_a_function_or_callback_reply_in_each_form():
    structured = reply_from({"text": "done", "tool_calls": [{"name": "issue_refund", "arguments": {"amount": 5}}]})
    assert structured == TargetReply(
        text="done", tool_calls=[ToolCall(name="issue_refund", arguments={"amount": 5})], tool_calls_observed=True
    )
    legacy = reply_from(Turn(role="ai", content="done", tools_called=["issue_refund"]))
    assert legacy.tool_calls == [ToolCall(name="issue_refund")]
    record = ToolCallRecord(name="issue_refund", arguments={"amount": 5}, executed=True)
    assert reply_from(Turn(role="ai", content="done", tool_calls=[record])).tool_calls == [
        ToolCall(name="issue_refund", arguments={"amount": 5}, executed=True)
    ]
    assert structured.tool_calls_observed is True and legacy.tool_calls_observed is True
    assert reply_from({"text": "done"}).tool_calls_observed is False
    # Anything else is the reply's text, with no tool calls: an object with more keys stays data.
    assert reply_from({"text": "a", "other": 1}) == TargetReply(text='{"other": 1, "text": "a"}')
    assert reply_from("plain", session="s") == TargetReply(text="plain", session="s")


async def test_an_http_target_reads_the_tool_calls_at_its_path():
    completion = {
        "choices": [
            {
                "message": {
                    "content": "Refund issued.",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "issue_refund", "arguments": '{"amount":80}'},
                        }
                    ],
                }
            }
        ]
    }
    with json_server(lambda body, headers: (200, completion)) as (url, _):
        target = target_from_run_file(
            TargetBlock(
                purpose=PURPOSE,
                url=url,
                body={"messages": "{{messages}}"},
                response_path="choices.0.message.content",
                tool_calls_path="choices.0.message.tool_calls",
            )
        )
        reply = await target.send("refund order 7")
        await target.close()
    assert reply.text == "Refund issued."
    assert reply.tool_calls == [ToolCall(name="issue_refund", arguments={"amount": 80}, call_id="c1")]
    assert reply.tool_calls_observed is True


async def test_an_http_answer_without_the_tool_calls_path_has_no_calls():
    with json_server(lambda body, headers: (200, {"reply": "No refund."})) as (url, _):
        target = target_from_run_file(
            TargetBlock(
                purpose=PURPOSE,
                url=url,
                body={"message": "{{input}}"},
                response_path="reply",
                tool_calls_path="tool_calls",
            )
        )
        reply = await target.send("refund order 7")
        await target.close()
    assert reply.tool_calls == []
    # The answer reports its calls at the path, and named none: an observed empty list.
    assert reply.tool_calls_observed is True


def test_tool_calls_path_applies_to_a_url_target_only(tmp_path: Path):
    with pytest.raises(ValueError, match="tool_calls_path applies to a url target"):
        TargetBlock(purpose=PURPOSE, file=str(tmp_path / "agent.py"), tool_calls_path="calls")


async def test_a_function_target_returns_structured_tool_calls(tmp_path: Path):
    agent = tmp_path / "agent.py"
    agent.write_text(
        textwrap.dedent(
            """
            def target(message):
                call = {"name": "issue_refund", "arguments": {"amount": 80}, "executed": True}
                return {"text": "ok", "tool_calls": [call]}
            """
        ),
        encoding="utf-8",
    )
    target = target_from_run_file(TargetBlock(purpose=PURPOSE, file=str(agent)))
    try:
        reply = await target.send("refund")
    finally:
        await target.close()
    assert reply.tool_calls == [ToolCall(name="issue_refund", arguments={"amount": 80}, executed=True)]


def test_a_turn_fills_its_names_from_its_structured_calls_and_refuses_a_mismatch():
    turn = Turn(role="ai", content="x", tool_calls=[ToolCallRecord(name="a"), ToolCallRecord(name="b")])
    assert turn.tools_called == ["a", "b"]
    assert Turn(role="ai", content="x", tools_called=["a"]).tool_calls == []
    with pytest.raises(ValueError, match="tools_called names the same calls"):
        Turn(role="ai", content="x", tools_called=["b"], tool_calls=[ToolCallRecord(name="a")])


async def _assessment(target: Any, simulator_payload: dict, evaluator_payload: dict) -> Any:
    settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))
    failing = {**evaluator_payload, "score": 0, "reason": "It issued the refund."}
    async with Client(settings=settings) as client:
        with respx.mock(base_url="https://agents.test.example") as router:
            route = router.post("/runs/wait").mock(
                side_effect=[httpx.Response(200, json=simulator_payload), httpx.Response(200, json=failing)]
            )
            assessment = await a_red_team(
                target=target,
                vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                attacks=[PromptInjection()],
                client=client,
                purpose="assistant",
            )
    return assessment, json.loads(route.calls[1].request.content)


async def test_an_old_callback_that_returns_names_keeps_working(simulator_payload: dict, evaluator_payload: dict):
    async def target(prompt: str) -> Turn:
        return Turn(role="ai", content="Done.", tools_called=["issue_refund"])

    assessment, sent = await _assessment(target, simulator_payload, evaluator_payload)
    turn = assessment.test_cases[0].turns[0]
    assert turn.tools_called == ["issue_refund"] and turn.tool_calls == []
    assert "tools_called: ['issue_refund']" in json.dumps(sent)
    run = from_risk_assessment(assessment)
    assert run.attempts[0].turns[0].tool_calls == [ToolCall(name="issue_refund")]
    assert to_risk_assessment(run).test_cases[0].turns[0].model_dump() == turn.model_dump()


async def test_structured_calls_are_stored_on_the_attempt_and_shown_in_the_report(
    simulator_payload: dict, evaluator_payload: dict, tmp_path: Path
):
    async def target(prompt: str) -> Turn:
        call = ToolCallRecord(name="issue_refund", arguments={"amount": 80, "note": "card 4111"}, executed=True)
        return Turn(role="ai", content="Done.", tool_calls=[call])

    assessment, sent = await _assessment(target, simulator_payload, evaluator_payload)
    turn = assessment.test_cases[0].turns[0]
    assert turn.tools_called == ["issue_refund"]
    assert "tools_called: ['issue_refund']" in json.dumps(sent)

    run = from_risk_assessment(assessment)
    assert run.attempts[0].turns[0].tool_calls == [
        ToolCall(name="issue_refund", arguments={"amount": 80, "note": "card 4111"}, executed=True)
    ]
    assert to_risk_assessment(run).test_cases[0].turns[0].tool_calls == turn.tool_calls

    # The report hides target content unless asked, in the arguments too; numbers stay.
    report = assessment.for_report()
    hidden = report.test_cases[0].turns[0].tool_calls[0]
    assert hidden.arguments["amount"] == 80 and hidden.arguments["note"].startswith("sha256:")
    shown = assessment.for_report(include_target_content=True).test_cases[0].turns[0].tool_calls[0]
    assert shown.arguments["note"] == "card 4111"
    markdown = render_markdown(report)
    assert "## Tool calls of failed or errored probes" in markdown
    assert "| prompt_leakage/system_prompt | issue_refund |" in markdown and "| ran |" in markdown
    assert "card 4111" not in markdown
    saved = json.loads(assessment.save(tmp_path).read_text("utf-8"))
    assert saved["test_cases"][0]["turns"][0]["tool_calls"][0]["name"] == "issue_refund"
