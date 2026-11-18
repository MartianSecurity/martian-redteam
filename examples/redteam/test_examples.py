"""The red team framework examples, each run against its stub model: no key, no other machine.

uv run --extra evals --extra mcp --group examples pytest examples/redteam
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from examples.redteam import claude_agent_sdk_agent, langgraph_agent, openai_agents_agent, pydantic_ai_agent
from examples.redteam.shared import FINAL_TEXT, attack
from martian.redteam._mcp import LoopbackServer
from martian.redteam.targets import TargetReply


async def claude_reply() -> TargetReply:
    model = LoopbackServer(claude_agent_sdk_agent.STUB_MODEL)
    await model.start()
    try:
        return await attack(lambda session: claude_agent_sdk_agent.build(session, f"http://127.0.0.1:{model.port}"))
    finally:
        await model.stop()


async def built(build: Callable[..., Any]) -> TargetReply:
    return await attack(build)


@pytest.mark.parametrize(
    "run",
    [
        lambda: built(openai_agents_agent.build),
        lambda: built(langgraph_agent.build),
        lambda: built(pydantic_ai_agent.build),
        claude_reply,
    ],
    ids=["openai-agents", "langgraph", "pydantic-ai", "claude-agent-sdk"],
)
async def test_each_example_reports_its_calls_and_the_proxy_blocks_the_refund(run: Callable[[], Any]):
    reply = await run()
    assert reply.text == FINAL_TEXT
    calls = [(call.name, call.arguments, call.executed) for call in reply.tool_calls]
    assert calls == [
        ("lookup_order", {"order": 7}, True),
        ("issue_refund", {"order": 7, "amount": 500}, False),
    ]
    # The agent reported each call with its result; the proxy's ledger says which ran.
    assert reply.tool_calls[0].result is not None and "80 USD" in reply.tool_calls[0].result
    assert [(item.source, item.metadata["executed"]) for item in reply.evidence] == [
        ("proxy:commerce", True),
        ("proxy:commerce", False),
    ]
