"""Local red team sessions in the same shape as cloud assessment sessions (martian-notes#246)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

import httpx2  # noqa: E402
from mcp import Client, MCPError  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from martian.redteam._mcp import LoopbackServer  # noqa: E402
from martian.redteam.proxy import ToolProxy, session_payload  # noqa: E402
from martian.redteam.runfile import ProxiedServer  # noqa: E402
from martian_redteam import AssessmentSession, bind_sandbox_mcp  # noqa: E402
from tests.redteam.mcp_servers import CommerceTools, served  # noqa: E402

RECORDED = Path(__file__).with_name("fixtures") / "cloud-assessment-session.json"


async def refund_agent(payload: dict[str, Any], action_id: str) -> list[str]:
    """An agent written against the SDK's binding, as a customer writes it for the cloud: it binds
    the session it is given and calls two of its tools. It knows nothing of the edition."""
    async with bind_sandbox_mcp(payload, action_id=action_id) as tools:
        by_name = {tool.remote_name: alias for alias, tool in tools.tools.items()}
        looked = await tools.call(by_name["lookup_order"], {"order": 7}, call_id="c1")
        refunded = await tools.call(by_name["issue_refund"], {"order": 7, "amount": 80}, call_id="c2")
    return [looked["content"][0]["text"], refunded["content"][0]["text"]]


@asynccontextmanager
async def cloud_gateway(tools: CommerceTools) -> AsyncIterator[tuple[str, list[dict[str, str]]]]:
    """A stand-in for the cloud's run-scoped gateway, at the address the recorded session names: it
    answers only the recorded token, and records each request's action id."""
    recorded = json.loads(RECORDED.read_text("utf-8"))
    connection = recorded["mcp_connections"][0]
    path = connection["url"].removeprefix("GATEWAY")
    token = connection["headers"]["Authorization"].encode()
    app = tools.server.streamable_http_app(host="127.0.0.1", streamable_http_path=path)
    seen: list[dict[str, str]] = []

    async def gateway(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if headers.get(b"authorization") != token:
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            seen.append({"action_id": headers.get(b"x-martian-action-id", b"").decode()})
        await app(scope, receive, send)

    loopback = LoopbackServer(gateway)
    await loopback.start()
    try:
        yield f"http://127.0.0.1:{loopback.port}", seen
    finally:
        await loopback.stop()


@asynccontextmanager
async def commerce_proxy(tools: CommerceTools) -> AsyncIterator[ToolProxy]:
    async with (
        served(tools.server) as upstream,
        ToolProxy([ProxiedServer(name="commerce", upstream=upstream)]) as proxy,
    ):
        yield proxy


def recorded_session(gateway: str) -> dict[str, Any]:
    text = RECORDED.read_text("utf-8").replace("GATEWAY", gateway)
    expires = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    return json.loads(text.replace("EXPIRES_AT", expires))


async def test_an_agent_written_against_bind_tools_runs_unchanged_locally_and_in_the_cloud():
    local_tools, cloud_tools = CommerceTools(), CommerceTools()
    async with served(local_tools.server) as upstream, cloud_gateway(cloud_tools) as (gateway, seen):
        async with ToolProxy([ProxiedServer(name="commerce", upstream=upstream)]) as proxy:
            session = await proxy.issue_session(target_id="refund-agent", campaign_id="run-1")
            local = await refund_agent(session_payload(session), "attempt-1")
            calls = proxy.calls()
        cloud = await refund_agent(recorded_session(gateway), "attempt-1")
    assert local == cloud == ["order 7: 2 items, paid 80 USD", "refunded 80.0 on order 7"]
    assert local_tools.executed == cloud_tools.executed
    # Both boundaries see the same action id, sent by the same binding.
    assert {call.action_id for call in calls} == {"attempt-1"}
    assert {item["action_id"] for item in seen if item["action_id"]} == {"attempt-1"}
    assert {call.session_id for call in calls} == {session.session_id}


async def test_a_local_session_has_the_cloud_shape():
    tools = CommerceTools()
    async with commerce_proxy(tools) as proxy:
        session = await proxy.issue_session(target_id="refund-agent", campaign_id="run-1")
        payload = session_payload(session)
    assert set(payload) == set(json.loads(RECORDED.read_text("utf-8")))
    assert AssessmentSession.model_validate(payload) == session
    [connection] = session.mcp_connections
    assert connection.name == "commerce" and connection.allowed_tools == ["issue_refund", "lookup_order"]
    assert connection.url.startswith("http://127.0.0.1:")
    assert payload["mcp_connections"][0]["headers"]["Authorization"].startswith("Bearer ")
    assert (session.campaign_id, session.target_id) == ("run-1", "refund-agent")
    assert session.session_id.startswith("local-")


async def test_the_session_is_refused_after_the_run_ends():
    tools = CommerceTools()
    async with served(tools.server) as upstream:
        proxy = ToolProxy([ProxiedServer(name="commerce", upstream=upstream)])
        async with proxy:
            ended = await proxy.issue_session()
            kept = await proxy.issue_session()
            assert await refund_agent(session_payload(ended), "a")
            proxy.end_session(ended.session_id)
            with pytest.raises(ExceptionGroup) as refused:
                await refund_agent(session_payload(ended), "b")
            assert refused.value.subgroup(lambda error: isinstance(error, MCPError)) is not None
            assert await refund_agent(session_payload(kept), "c")
            url = proxy.url("commerce")
        # The run ended: the proxy stopped, and no session of it is honoured again.
        with pytest.raises(ExceptionGroup) as gone:
            await refund_agent(session_payload(kept), "d")
        assert gone.value.subgroup(lambda error: isinstance(error, httpx2.ConnectError)) is not None
    assert {call.action_id for call in proxy.calls()} == {"a", "c"}
    assert url.startswith("http://127.0.0.1:")


async def raw_status(url: str, token: str | None) -> int:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with httpx2.AsyncClient() as client:
        return (await client.post(url, headers=headers, json=body)).status_code


async def test_requests_without_an_open_session_are_refused_before_any_tool():
    tools = CommerceTools()
    async with commerce_proxy(tools) as proxy:
        short = await proxy.issue_session(ttl=timedelta(milliseconds=300))
        token = short.mcp_connections[0].headers["Authorization"].get_secret_value().removeprefix("Bearer ")
        url = proxy.url("commerce")
        assert await raw_status(url, None) == 401
        assert await raw_status(url, "not-a-token") == 401
        assert await raw_status(url, token) != 401
        await asyncio.sleep(0.4)
        assert await raw_status(url, token) == 401
        async with (
            httpx2.AsyncClient(headers={"Authorization": "Bearer nope"}) as http,
        ):
            with pytest.raises(ExceptionGroup):
                async with Client(streamable_http_client(url, http_client=http), cache=None) as client:
                    await client.call_tool("issue_refund", {"order": 1, "amount": 1})
    assert tools.executed == [] and proxy.calls() == []


async def test_a_session_is_issued_only_by_a_running_proxy():
    proxy = ToolProxy([ProxiedServer(name="commerce", upstream="http://127.0.0.1:9/mcp")])
    with pytest.raises(RuntimeError, match="not running"):
        await proxy.issue_session()


def test_a_proxied_servers_name_is_a_valid_session_connection_name():
    with pytest.raises(ValueError, match="String should match pattern"):
        ProxiedServer(name="commerce-api", upstream="http://localhost:9000/mcp")
