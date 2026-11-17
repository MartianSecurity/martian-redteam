"""MCP servers the red team MCP tests attack, served on loopback or over stdio. Nothing here reaches
another machine. Run as a script, it serves :func:`chat_server` over stdio."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from martian.redteam._mcp import LoopbackServer


def chat_server() -> MCPServer:
    """An agent exposed as an MCP server: ``chat`` keeps a turn count per session, and ``reset``
    starts the count again. ``agent`` answers in structured content with the tool calls it made."""
    server = MCPServer("chat-agent")
    turns: dict[str, int] = {}

    @server.tool()
    def chat(message: str, session: str = "", reset: bool = False, tone: str = "plain") -> str:
        """Talk to the agent."""
        if reset:
            turns[session] = 0
        turns[session] = turns.get(session, 0) + 1
        return f"turn {turns[session]} ({tone}): {message}"

    @server.tool()
    def agent(message: str) -> dict[str, Any]:
        """Talk to the agent, which reports its tool calls."""
        return {
            "reply": f"refunded for {message}",
            "tool_calls": [{"name": "issue_refund", "arguments": {"amount": 80}, "executed": True}],
        }

    @server.tool()
    def broken(message: str) -> str:
        """Always fails."""
        raise ValueError("a secret in the failure")

    return server


class ChangingTool:
    """A server whose ``chat`` tool's input schema can change between calls."""

    def __init__(self) -> None:
        self.schema: dict[str, Any] = {"type": "object", "properties": {"message": {"type": "string"}}}
        self.calls = 0

        async def list_tools(ctx: Any, params: Any) -> ListToolsResult:
            return ListToolsResult(tools=[Tool(name="chat", input_schema=self.schema)])

        async def call_tool(ctx: Any, params: Any) -> CallToolResult:
            self.calls += 1
            return CallToolResult(content=[TextContent(type="text", text="ok")])

        self.server = Server("changing", on_list_tools=list_tools, on_call_tool=call_tool)


@asynccontextmanager
async def served(server: MCPServer | Server[Any]) -> AsyncIterator[str]:
    """``server`` over Streamable HTTP on 127.0.0.1; yields its address."""
    loopback = LoopbackServer(server.streamable_http_app(host="127.0.0.1"))
    await loopback.start()
    try:
        yield f"http://127.0.0.1:{loopback.port}/mcp"
    finally:
        await loopback.stop()


class CommerceTools:
    """The agent's own tool server: what the recording proxy sits in front of. ``executed`` lists the
    calls that reached it."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        server = MCPServer("commerce")

        @server.tool()
        def lookup_order(order: int) -> str:
            """Look up an order."""
            self.executed.append(("lookup_order", {"order": order}))
            return f"order {order}: 2 items, paid 80 USD"

        @server.tool()
        def issue_refund(order: int, amount: float) -> str:
            """Refund an order."""
            self.executed.append(("issue_refund", {"order": order, "amount": amount}))
            return f"refunded {amount} on order {order}"

        self.server = server


if __name__ == "__main__":
    chat_server().run("stdio")
