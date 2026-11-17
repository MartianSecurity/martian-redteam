"""A Pydantic AI agent as a red team target, its tools bound through the recording proxy.

The agent's tools come from its session with ``bind_tools`` and become Pydantic AI tools with
``Tool.from_schema``. The model is Pydantic AI's ``FunctionModel`` playing a model that falls for the
attack, so the example needs no key. The agent reports its tool calls from the run's messages.

    uv run python -m examples.redteam.pydantic_ai_agent
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from examples.redteam.shared import (
    FINAL_TEXT,
    LOOKUP,
    REFUND,
    SYSTEM_PROMPT,
    Answer,
    alias_of,
    attack,
    reported,
    result_text,
    show,
)
from martian_redteam import AssessmentSession, bind_tools

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


def stub_model(tools: Any) -> FunctionModel:
    """A model that looks the order up, asks for the refund, then answers."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        asked = sum(isinstance(part, ToolCallPart) for message in messages for part in message.parts)
        if asked < 2:
            name, arguments = (LOOKUP, REFUND)[asked]
            return ModelResponse(parts=[ToolCallPart(alias_of(name, tools), arguments, tool_call_id=f"call_{asked}")])
        return ModelResponse(parts=[TextPart(FINAL_TEXT)])

    return FunctionModel(respond)


def build(session: AssessmentSession) -> Answer:
    async def answer(message: str, session_id: str) -> dict[str, Any]:
        async with bind_tools(session, action_id=session_id) as tools:

            def tool(alias: str) -> Tool[Any]:
                definition = tools.tools[alias].definition

                async def call(**arguments: Any) -> str:
                    return result_text(await tools.call(alias, arguments, call_id=alias))

                return Tool.from_schema(call, alias, definition["description"], definition["input_schema"])

            agent = Agent(stub_model(tools), instructions=SYSTEM_PROMPT, tools=[tool(alias) for alias in tools.tools])
            result = await agent.run(message)
        returns = {
            part.tool_call_id: part.content
            for item in result.all_messages()
            for part in item.parts
            if isinstance(part, ToolReturnPart)
        }
        calls = [
            reported(tools.tools[part.tool_name].remote_name, part.args_as_dict(), returns.get(part.tool_call_id))
            for item in result.all_messages()
            for part in item.parts
            if isinstance(part, ToolCallPart)
        ]
        return {"text": result.output, "tool_calls": calls}

    return answer


async def main() -> None:
    show(await attack(build))


if __name__ == "__main__":
    asyncio.run(main())
