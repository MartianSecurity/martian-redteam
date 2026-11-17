"""A Claude Agent SDK agent as a red team target, its MCP tools reached through the recording proxy.

The Claude Agent SDK connects to MCP servers itself, so the agent hands it its session's connections
as ``mcp_servers``: each proxied server's address and headers, with the conversation's id in
``X-Martian-Action-Id``, as ``bind_tools`` would send it. The SDK runs the Claude Code CLI it bundles.
Here the CLI talks to a stub of the Messages API on loopback that plays a model falling for the
attack, so the example needs no key and reaches no other machine. The agent reports its tool calls
from the SDK's messages.

    uv run python -m examples.redteam.claude_agent_sdk_agent
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from examples.redteam.shared import FINAL_TEXT, LOOKUP, REFUND, SYSTEM_PROMPT, Answer, attack, reported, show
from martian.redteam._mcp import LoopbackServer
from martian_redteam import AssessmentSession


def _blocks(body: dict[str, Any]) -> list[dict[str, Any]]:
    """What the stub model answers: the next tool it asks for, or the final text."""
    tools = [tool["name"] for tool in body.get("tools") or [] if isinstance(tool, dict)]
    results = sum(
        1
        for message in body.get("messages") or []
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    if results < 2 and tools:
        name, arguments = (LOOKUP, REFUND)[results]
        found = next((tool for tool in tools if tool.endswith(f"__{name}")), None)
        if found is not None:
            return [{"type": "tool_use", "id": f"toolu_{results}", "name": found, "input": arguments}]
    return [{"type": "text", "text": FINAL_TEXT if tools else "ok"}]


def _events(blocks: list[dict[str, Any]]) -> AsyncIterator[bytes]:
    """The blocks as the Messages API streams them."""
    stop = "tool_use" if blocks[0]["type"] == "tool_use" else "end_turn"

    def event(kind: str, data: dict[str, Any]) -> bytes:
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode()

    async def stream() -> AsyncIterator[bytes]:
        usage = {"input_tokens": 1, "output_tokens": 1}
        message = {"id": "msg_stub", "type": "message", "role": "assistant", "model": "stub", "content": []}
        yield event("message_start", {"message": {**message, "stop_reason": None, "usage": usage}})
        for index, block in enumerate(blocks):
            if block["type"] == "tool_use":
                yield event("content_block_start", {"index": index, "content_block": {**block, "input": {}}})
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            else:
                yield event("content_block_start", {"index": index, "content_block": {"type": "text", "text": ""}})
                delta = {"type": "text_delta", "text": block["text"]}
            yield event("content_block_delta", {"index": index, "delta": delta})
            yield event("content_block_stop", {"index": index})
        yield event("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None}, "usage": usage})
        yield event("message_stop", {})

    return stream()


async def _messages(request: Request) -> Response:
    body = await request.json()
    blocks = _blocks(body)
    if body.get("stream"):
        return StreamingResponse(_events(blocks), media_type="text/event-stream")
    stop = "tool_use" if blocks[0]["type"] == "tool_use" else "end_turn"
    return JSONResponse(
        {
            "id": "msg_stub",
            "type": "message",
            "role": "assistant",
            "model": "stub",
            "content": blocks,
            "stop_reason": stop,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


async def _count_tokens(request: Request) -> Response:
    return JSONResponse({"input_tokens": 1})


STUB_MODEL = Starlette(
    routes=[
        Route("/v1/messages", _messages, methods=["POST"]),
        Route("/v1/messages/count_tokens", _count_tokens, methods=["POST"]),
    ]
)
"""A stub of the Messages API: what the bundled CLI calls in place of a model."""


def build(session: AssessmentSession, model_url: str) -> Answer:
    async def answer(message: str, session_id: str) -> dict[str, Any]:
        servers: dict[str, Any] = {
            connection.name: {
                "type": "http",
                "url": connection.url,
                "headers": {
                    **{name: value.get_secret_value() for name, value in connection.headers.items()},
                    "X-Martian-Action-Id": session_id,
                },
            }
            for connection in session.mcp_connections
        }
        allowed = [
            f"mcp__{connection.name}__{tool}"
            for connection in session.mcp_connections
            for tool in connection.allowed_tools
        ]
        with tempfile.TemporaryDirectory(prefix="claude-agent-") as home:
            options = ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT,
                mcp_servers=servers,
                strict_mcp_config=True,
                tools=[],
                allowed_tools=allowed,
                setting_sources=[],
                max_turns=6,
                cwd=home,
                env={
                    "ANTHROPIC_BASE_URL": model_url,
                    "ANTHROPIC_API_KEY": "stub-key-for-a-local-stub-model",
                    "CLAUDE_CONFIG_DIR": home,
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "DISABLE_AUTOUPDATER": "1",
                    "DISABLE_TELEMETRY": "1",
                },
            )
            uses: list[ToolUseBlock] = []
            results: dict[str, str] = {}
            text = ""
            async for item in query(prompt=message, options=options):
                if isinstance(item, AssistantMessage):
                    uses += [block for block in item.content if isinstance(block, ToolUseBlock)]
                    text = "".join(block.text for block in item.content if isinstance(block, TextBlock)) or text
                elif isinstance(item, UserMessage) and isinstance(item.content, list):
                    for block in item.content:
                        if isinstance(block, ToolResultBlock):
                            content = block.content
                            results[block.tool_use_id] = content if isinstance(content, str) else json.dumps(content)
                elif isinstance(item, ResultMessage) and item.result:
                    text = item.result
        calls = [reported(use.name.rsplit("__", 1)[-1], use.input, results.get(use.id)) for use in uses]
        return {"text": text, "tool_calls": calls}

    return answer


async def main() -> None:
    model = LoopbackServer(STUB_MODEL)
    await model.start()
    try:
        show(await attack(lambda session: build(session, f"http://127.0.0.1:{model.port}")))
    finally:
        await model.stop()


if __name__ == "__main__":
    asyncio.run(main())
