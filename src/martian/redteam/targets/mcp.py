"""An MCP server as a red team target: one entry tool used as the chat.

The run file's ``mcp`` block names the server (a Streamable HTTP ``url``, or a stdio ``command``),
the ``tool`` that takes a message and the ``message_argument`` the message goes in. Each message is
one call of that tool, with ``arguments`` beside it, and the conversation's id in
``session_argument`` and ``true`` on its first turn in ``reset_argument``, when the block names them
(:mod:`martian.redteam.targets.conversations`). Needs the ``mcp`` extra.

The reply is the text of the result's text content. ``response_path`` reads it from the result's
structured content instead, or from its text as JSON when it has none; ``tool_calls_path`` reads the
tool calls the same way (:mod:`martian.redteam.targets.reply`). A result marked as an error, a value
missing at a path, or a server that cannot be reached is the target's error
(:class:`~martian.evals.runner.TargetCallError`), in words that never repeat the address, a header or
what the server sent.

The tool's input schema is pinned for the run, as the hosted engine pins an MCP target's entry tool:
its SHA-256 over canonical JSON (:func:`martian.redteam._mcp.schema_digest`) is ``schema_sha256``
when the block gives one, else the digest the first message saw, and every message checks it again.
A tool that is gone or whose schema changed raises
:class:`~martian.redteam.targets.TargetChanged`, which stops the run: the run never attacks a tool
other than the one it started with.

One connection serves every message; ``${NAME}`` in ``url`` and header values is read from the
environment when the target is built. A stdio server is started with the first message, with only
the environment variables MCP's stdio client passes on, and ends with :meth:`McpServerTarget.close`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from martian.evals.runner import DEFAULT_TARGET_TIMEOUT_SECONDS, TIMED_OUT, TargetCallError, _from_environment
from martian.redteam._mcp import HeldClient, every_tool, schema_digest
from martian.redteam.runfile import McpTarget
from martian.redteam.targets import TargetChanged, TargetReply
from martian.redteam.targets.conversations import Conversations
from martian.redteam.targets.identity import endpoint_identity
from martian.redteam.targets.reply import read_path, reply_text, tool_calls

UNREACHABLE = "the MCP target could not be reached: {kind}; check target.mcp"


class McpServerTarget:
    """``config`` is the run file's ``mcp`` block. ``base`` is the folder a stdio server starts in."""

    kind = "mcp"

    def __init__(
        self,
        config: McpTarget,
        *,
        timeout_seconds: float = DEFAULT_TARGET_TIMEOUT_SECONDS,
        base: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._timeout = timeout_seconds
        self._base = base
        self._url = _from_environment(config.url, "target.mcp.url", environ) if config.url is not None else None
        self.findings_key, self.name = (
            endpoint_identity(self.kind, self._url, config.tool, has_headers=bool(config.headers))
            if self._url
            else ("", config.tool)
        )
        self._headers = {
            name: _from_environment(value, f"target.mcp.headers.{name}", environ)
            for name, value in config.headers.items()
        }
        self._response = _steps(config.response_path)
        self._calls = _steps(config.tool_calls_path)
        self._pin = config.schema_sha256
        self._conversations = Conversations()
        self._held = HeldClient(self._connect)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Client]:
        if self._url is not None:
            async with (
                httpx2.AsyncClient(headers=self._headers, timeout=self._timeout, follow_redirects=False) as http,
                Client(streamable_http_client(self._url, http_client=http), cache=None) as client,
            ):
                yield client
            return
        assert self._config.command is not None
        program, *arguments = self._config.command
        parameters = StdioServerParameters(command=program, args=arguments, cwd=self._base)
        async with Client(parameters, cache=None) as client:
            yield client

    @property
    def schema_sha256(self) -> str | None:
        """The digest of the entry tool's input schema the run is pinned to, once known."""
        return self._pin

    async def _checked(self, client: Client) -> None:
        tool = (await every_tool(client)).get(self._config.tool)
        if tool is None:
            raise TargetChanged(f"the MCP target no longer lists the tool {self._config.tool!r}; the run stopped")
        found = schema_digest(tool)
        if self._pin is None:
            self._pin = found
        elif found != self._pin:
            raise TargetChanged(
                f"the input schema of the MCP target's tool {self._config.tool!r} is not the one the run is pinned "
                "to; the run stopped. Check target.mcp.schema_sha256, or start a new run to test the tool as it is now"
            )

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        try:
            client = await self._held.open()
            async with self._conversations.turn(session) as conversation:
                await asyncio.wait_for(self._checked(client), self._timeout)
                arguments = {**self._config.arguments, self._config.message_argument: message}
                if self._config.session_argument:
                    arguments[self._config.session_argument] = conversation.id
                if self._config.reset_argument:
                    arguments[self._config.reset_argument] = conversation.new
                started = time.perf_counter()
                result = await asyncio.wait_for(client.call_tool(self._config.tool, arguments), self._timeout)
                latency = (time.perf_counter() - started) * 1000
                text, calls = self._read(result)
                conversation.answered(message, text)
                return TargetReply(
                    text=text,
                    tool_calls=calls,
                    tool_calls_observed=bool(self._calls),
                    session=conversation.session,
                    latency_ms=latency,
                )
        except (TargetCallError, TargetChanged):
            raise
        except TimeoutError:
            raise TargetCallError(TIMED_OUT) from None
        except Exception as error:  # the transport's and the server's errors, in our words only
            raise TargetCallError(UNREACHABLE.format(kind=type(error).__name__)) from None

    def _read(self, result: CallToolResult) -> tuple[str, list[Any]]:
        if result.is_error:
            raise TargetCallError("the MCP target's tool answered with an error")
        text = "\n".join(item.text for item in result.content if item.type == "text")
        if not self._response and not self._calls:
            return text, []
        source: Any = result.structured_content
        if source is None:
            try:
                source = json.loads(text)
            except ValueError:
                raise TargetCallError(
                    "the MCP target's result is not JSON, so target.mcp.response_path cannot be read"
                ) from None
        if self._response:
            found = read_path(source, self._response)
            if found is None:
                raise TargetCallError("the MCP target's result has no value at target.mcp.response_path")
            text = reply_text(found)
        calls = tool_calls(read_path(source, self._calls), "target.mcp.tool_calls_path") if self._calls else []
        return text, calls

    def end(self, session: str) -> None:
        self._conversations.end(session)

    async def close(self) -> None:
        self._conversations.clear()
        await self._held.close()


def _steps(path: str | None) -> list[str]:
    from martian.evals.runner import _response_steps

    return _response_steps(path, "target.mcp path")


__all__ = ["UNREACHABLE", "McpServerTarget"]
