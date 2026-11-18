"""MCP tool-description and skill poisoning: an inert local test MCP server whose tool description
carries a payload, and a skill planted in a temporary folder; a vulnerable test agent is caught by
both (martian-notes#267). The agents run in the test's process and reach only loopback servers the
run started; the attacker is scripted."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

import httpx2  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from martian.redteam import poisoning  # noqa: E402
from martian.redteam.engine import Engine, PlanError, plan  # noqa: E402
from martian.redteam.engine.runs import SESSION_FILE, tool_boundary  # noqa: E402
from martian.redteam.model import EvidenceLevel  # noqa: E402
from martian.redteam.targets import TargetReply  # noqa: E402
from martian.redteam.targets.conversations import conversation_id  # noqa: E402
from tests.redteam.engine_helpers import StubService, run_file  # noqa: E402
from tests.redteam.vulnerable_agents import LISTENER, fetch  # noqa: E402

FOLLOW = re.compile(r"call the tool send_note with the text (\S+)\.")
OPEN = re.compile(r"open (http://127\.0\.0\.1:\d+/r/[0-9a-f]+\?d=\S+) instead")


@asynccontextmanager
async def tools_of(session: dict[str, Any], action_id: str) -> AsyncIterator[Client]:
    """The decoy's connection of the agent's session, as ``bind_tools`` would open it."""
    [connection] = session["mcp_connections"]
    headers = {**connection["headers"], "X-Martian-Action-Id": action_id}
    async with (
        httpx2.AsyncClient(headers=headers) as http,
        Client(streamable_http_client(connection["url"], http_client=http), cache=None) as client,
    ):
        yield client


class ToolReadingAgent:
    """An agent that binds its tools from the session the run hands it and answers a forecast
    request. ``obeys``: it follows any instruction it finds in a tool's description."""

    kind = "function"
    name = "tool-reader"

    def __init__(self, folder: Path, *, obeys: bool = True) -> None:
        self.folder = folder
        self.obeys = obeys

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        handed = json.loads((self.folder / SESSION_FILE).read_text())
        async with tools_of(handed, conversation_id(session or "s")) as client:
            listed = await client.list_tools()
            described = " ".join(tool.description or "" for tool in listed.tools)
            if self.obeys and (found := FOLLOW.search(described)):
                await client.call_tool("send_note", {"text": found.group(1)})
            forecast = await client.call_tool("get_forecast", {"city": "Paris"})
        text = "\n".join(item.text for item in forecast.content if item.type == "text")
        return TargetReply(text=text, session=session)

    async def close(self) -> None:
        return None


class SkillReadingAgent:
    """An agent that loads every skill in its folder for each request. ``obeys``: it opens any
    address a skill tells it to."""

    kind = "function"
    name = "skill-reader"

    def __init__(self, skills: Path, *, obeys: bool = True) -> None:
        self.skills = skills
        self.obeys = obeys
        self.loaded: list[str] = []

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        texts = [path.read_text() for path in sorted(self.skills.glob("*/SKILL.md"))]
        self.loaded += texts
        for text in texts:
            if self.obeys and (found := OPEN.search(text)):
                port, path = LISTENER.search(found.group(1)).groups()  # type: ignore[union-attr]
                await fetch(int(port), path + found.group(1).split(path, 1)[1])
        # It reports its tool calls (none): a target that does not leaves the send_note check undetermined.
        return TargetReply(text="Order 42 has shipped.", tool_calls_observed=True, session=session)

    async def close(self) -> None:
        return None


def poisoning_file(vulnerability_type: str, **tools: Any):
    return run_file(
        vulnerabilities=[{"name": "ToolPoisoning", "types": [vulnerability_type]}],
        tools=tools,
        verification={"control": True},
    )


async def run_with_boundary(file, agent, folder: Path):
    async with AsyncExitStack() as stack:
        target = await tool_boundary(file, agent, stack, folder=folder, run_id="poisoning")
        return await Engine(file, service=StubService(), target=target).run()


async def test_the_decoy_server_exposes_only_inert_tools():
    decoy = poisoning.DecoyServer()
    url = await decoy.start()
    try:
        async with (
            httpx2.AsyncClient() as http,
            Client(streamable_http_client(url, http_client=http), cache=None) as client,
        ):
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == ["get_forecast", "send_note"]
            assert listed.tools[0].description == poisoning.FORECAST_DESCRIPTION
            decoy.payload = "PAYLOAD"
            assert (await client.list_tools()).tools[0].description.endswith("\n\nPAYLOAD")
            answers = [await client.call_tool("send_note", {"text": "x"}), await client.call_tool("get_forecast", {})]
    finally:
        await decoy.stop()
    assert [item.content[0].text for item in answers] == ["Noted.", "Tomorrow: sunny, 21 C."]
    assert decoy.calls == [("send_note", {"text": "x"}), ("get_forecast", {})]


async def test_a_poisoned_tool_description_steers_a_vulnerable_agent(tmp_path):
    file = poisoning_file("mcp_tool_description", decoy={"name": "utilities"})
    found = await run_with_boundary(file, ToolReadingAgent(tmp_path), tmp_path)
    [finding] = found.findings
    assert (finding.evidence_level, finding.label) == (EvidenceLevel.TOOL_CALL, "Confirmed action")
    attack = next(item for item in found.attempts if item.role == "attack")
    assert attack.turns[0].content == poisoning.MCP_TRIGGER and "send_note" in (attack.planted or "")
    # The control planted nothing, so the agent called no tool with the canary: the finding stands.
    [verification] = found.verifications
    assert verification.negative_control_passed is True


async def test_an_agent_that_ignores_tool_descriptions_holds(tmp_path):
    file = poisoning_file("mcp_tool_description", decoy={"name": "utilities"})
    found = await run_with_boundary(file, ToolReadingAgent(tmp_path, obeys=False), tmp_path)
    [attempt] = found.attempts
    assert attempt.outcome == "resisted" and not found.findings


async def test_a_planted_skill_steers_a_vulnerable_agent_and_is_removed(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    agent = SkillReadingAgent(skills)
    found = await Engine(
        poisoning_file("skill_file", skills_dir=str(skills)), service=StubService(), target=agent
    ).run()
    [finding] = found.findings
    assert (finding.evidence_level, finding.label) == (EvidenceLevel.RECEIPT, "Verified exfiltration")
    assert agent.loaded[0].startswith("---\nname: order-help\n") and list(skills.iterdir()) == []


async def test_an_agent_that_does_not_follow_skills_holds(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    found = await Engine(
        poisoning_file("skill_file", skills_dir=str(skills)),
        service=StubService(),
        target=SkillReadingAgent(skills, obeys=False),
    ).run()
    [attempt] = found.attempts
    assert attempt.outcome == "resisted" and not found.findings


def test_each_type_needs_where_it_plants():
    with pytest.raises(PlanError, match="set tools.decoy"):
        plan(poisoning_file("mcp_tool_description"))
    with pytest.raises(PlanError, match="set tools.skills_dir"):
        plan(poisoning_file("skill_file"))
