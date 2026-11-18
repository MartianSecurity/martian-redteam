"""An MCP server as a red team target (martian-notes#244)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

pytest.importorskip("mcp")

from martian.evals.runner import TargetCallError  # noqa: E402
from martian.redteam.model import ToolCall  # noqa: E402
from martian.redteam.runfile import McpTarget  # noqa: E402
from martian.redteam.runfile import Target as TargetBlock  # noqa: E402
from martian.redteam.targets import TargetChanged, target_from_run_file  # noqa: E402
from martian.redteam.targets.mcp import McpServerTarget  # noqa: E402
from martian_redteam.catalog import PromptInjection, PromptLeakage  # noqa: E402
from martian_redteam.client import Client  # noqa: E402
from martian_redteam.config import Settings  # noqa: E402
from martian_redteam.orchestrator import a_red_team  # noqa: E402
from tests.redteam.mcp_servers import ChangingTool, chat_server, served  # noqa: E402

PURPOSE = "A support agent exposed as an MCP server."
SERVER = Path(__file__).with_name("mcp_servers.py")


def mcp_block(**fields: Any) -> TargetBlock:
    return TargetBlock.model_validate({"purpose": PURPOSE, "mcp": fields})


async def test_a_red_team_run_completes_against_a_local_mcp_server(simulator_payload: dict, evaluator_payload: dict):
    async with served(chat_server()) as url:
        target = target_from_run_file(mcp_block(url=url, tool="chat", arguments={"tone": "calm"}))
        assert isinstance(target, McpServerTarget)
        replies: list[str] = []

        async def callback(prompt: str) -> str:
            reply = await target.send(prompt)
            replies.append(reply.text)
            return reply.text

        settings = Settings(api_url="https://agents.test.example", api_key=SecretStr("test-key"))
        async with Client(settings=settings) as client:
            with respx.mock(base_url="https://agents.test.example") as router:
                router.post("/runs/wait").mock(
                    side_effect=[
                        httpx.Response(200, json=simulator_payload),
                        httpx.Response(200, json=evaluator_payload),
                    ]
                )
                assessment = await a_red_team(
                    target=callback,
                    vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                    attacks=[PromptInjection()],
                    client=client,
                    purpose=PURPOSE,
                )
        await target.close()
    assert assessment.errored == 0 and assessment.test_cases[0].score == 1
    assert replies == [f"turn 1 (calm): {simulator_payload['attack']}"]
    assert target.schema_sha256 is not None


async def test_the_session_and_reset_arguments_keep_and_start_conversations():
    async with served(chat_server()) as url:
        target = target_from_run_file(
            mcp_block(url=url, tool="chat", session_argument="session", reset_argument="reset")
        )
        first = await target.send("a")
        second = await target.send("b", session=first.session)
        fresh = await target.send("c", session=first.session + "-x")
        again = await target.send("d", session=first.session + "-x")
        # The same id again, as a new conversation: the reset argument starts the server's count over.
        target.end(first.session)
        reset = await target.send("e", session=first.session)
        await target.close()
    assert [first.text, second.text] == ["turn 1 (plain): a", "turn 2 (plain): b"]
    assert [fresh.text, again.text] == ["turn 1 (plain): c", "turn 2 (plain): d"]
    assert reset.text == "turn 1 (plain): e"


async def test_a_stdio_server_is_a_target_too():
    target = target_from_run_file(
        mcp_block(command=[sys.executable, str(SERVER)], tool="chat", session_argument="session"),
        base=Path(__file__).parents[2],
    )
    try:
        first = await target.send("hello")
        second = await target.send("again", session=first.session)
    finally:
        await target.close()
    assert [first.text, second.text] == ["turn 1 (plain): hello", "turn 2 (plain): again"]


async def test_the_reply_and_tool_calls_are_read_from_structured_content():
    async with served(chat_server()) as url:
        target = target_from_run_file(
            mcp_block(url=url, tool="agent", response_path="reply", tool_calls_path="tool_calls")
        )
        reply = await target.send("order 7")
        await target.close()
    assert reply.text == "refunded for order 7"
    assert reply.tool_calls == [ToolCall(name="issue_refund", arguments={"amount": 80}, executed=True)]


async def test_a_changed_tool_schema_stops_the_run_with_a_clear_error():
    changing = ChangingTool()
    async with served(changing.server) as url:
        target = McpServerTarget(McpTarget(url=url, tool="chat"))
        assert (await target.send("first")).text == "ok"
        changing.schema = {
            **changing.schema,
            "properties": {"message": {"type": "string"}, "admin": {"type": "boolean"}},
        }
        with pytest.raises(TargetChanged, match="input schema of the MCP target's tool 'chat' is not the one"):
            await target.send("second")
        await target.close()
    assert changing.calls == 1


async def test_a_pinned_schema_digest_that_does_not_match_stops_before_any_call():
    changing = ChangingTool()
    async with served(changing.server) as url:
        target = McpServerTarget(McpTarget(url=url, tool="chat", schema_sha256="0" * 64))
        with pytest.raises(TargetChanged, match="schema_sha256"):
            await target.send("first")
        await target.close()
    assert changing.calls == 0


async def test_a_tool_that_is_gone_stops_the_run():
    async with served(chat_server()) as url:
        target = McpServerTarget(McpTarget(url=url, tool="missing"))
        with pytest.raises(TargetChanged, match="no longer lists the tool 'missing'"):
            await target.send("hello")
        await target.close()


async def test_errors_name_the_setting_and_never_what_the_server_sent():
    async with served(chat_server()) as url:
        target = McpServerTarget(McpTarget(url=url, tool="broken"))
        with pytest.raises(TargetCallError, match="answered with an error") as raised:
            await target.send("hello")
        await target.close()
    assert "secret" not in str(raised.value)
    unreachable = McpServerTarget(McpTarget(url="http://127.0.0.1:9/mcp", tool="chat"), timeout_seconds=5)
    with pytest.raises(TargetCallError, match="could not be reached: .*; check target.mcp") as raised:
        await unreachable.send("hello")
    await unreachable.close()
    assert "127.0.0.1" not in str(raised.value)


async def test_messages_from_many_tasks_share_one_connection_and_close_from_another():
    async with served(chat_server()) as url:
        target = McpServerTarget(McpTarget(url=url, tool="chat", session_argument="session"))
        replies = await asyncio.gather(*(asyncio.create_task(target.send(f"m{i}")) for i in range(5)))
        await asyncio.create_task(target.close())
    assert sorted(reply.text for reply in replies) == [f"turn 1 (plain): m{i}" for i in range(5)]


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"command": ["x"], "tool": "chat", "headers": {"A": "b"}}, "headers apply to an mcp target's url"),
        ({"url": "http://x/mcp", "tool": "chat", "session_argument": "message"}, "name different arguments"),
        ({"url": "http://x/mcp", "tool": "chat", "arguments": {"message": "x"}}, "arguments must not set message"),
        ({"url": "http://x/mcp", "tool": "chat", "schema_sha256": "abc"}, "String should match pattern"),
    ],
)
def test_the_mcp_block_refuses_settings_that_cannot_work(fields: dict, reason: str):
    with pytest.raises(ValueError, match=reason):
        mcp_block(**fields)


def test_an_mcp_target_takes_a_time_limit():
    target = TargetBlock.model_validate(
        {"purpose": PURPOSE, "timeout_seconds": 30, "mcp": {"url": "http://x/mcp", "tool": "chat"}}
    )
    assert target.kind == "mcp" and target.timeout_seconds == 30
