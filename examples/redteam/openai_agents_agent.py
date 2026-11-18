"""An OpenAI Agents SDK agent as a red team target, its tools bound through the recording proxy.

The agent's tools come from its session with ``bind_tools`` and become ``FunctionTool``s. The model is
a stub ``Model`` that plays a model falling for the attack, so the example needs no key, and the SDK's
own tracing export is off. The agent reports its tool calls from the run's items.

    uv run python -m examples.redteam.openai_agents_agent
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from agents import Agent, FunctionTool, Model, ModelResponse, Runner, Usage, set_tracing_disabled
from agents.items import ToolCallItem, ToolCallOutputItem
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

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

# The SDK exports its own traces to OpenAI by default; the example keeps everything local.
set_tracing_disabled(True)


class StubModel(Model):
    """A model that looks the order up, asks for the refund, then answers."""

    def __init__(self, tools: Any) -> None:
        self._tools = tools

    async def get_response(self, system_instructions: str | None, input: Any, *args: Any, **kwargs: Any) -> Any:
        asked = sum(1 for item in input if isinstance(item, dict) and item.get("type") == "function_call")
        if asked < 2:
            name, arguments = (LOOKUP, REFUND)[asked]
            output: Any = ResponseFunctionToolCall(
                type="function_call",
                call_id=f"call_{asked}",
                name=alias_of(name, self._tools),
                arguments=json.dumps(arguments),
            )
        else:
            text = ResponseOutputText(type="output_text", text=FINAL_TEXT, annotations=[])
            output = ResponseOutputMessage(
                type="message", id="msg_1", role="assistant", status="completed", content=[text]
            )
        return ModelResponse(output=[output], usage=Usage(), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError("the example runs without streaming")


def build(session: AssessmentSession) -> Answer:
    async def answer(message: str, session_id: str) -> dict[str, Any]:
        async with bind_tools(session, action_id=session_id) as tools:

            def tool(alias: str) -> FunctionTool:
                definition = tools.tools[alias].definition

                async def invoke(context: Any, arguments: str) -> str:
                    return result_text(
                        await tools.call(alias, json.loads(arguments or "{}"), call_id=context.tool_call_id)
                    )

                return FunctionTool(
                    name=alias,
                    description=definition["description"],
                    params_json_schema=definition["input_schema"],
                    on_invoke_tool=invoke,
                    strict_json_schema=False,
                )

            agent = Agent(
                name="refunds",
                instructions=SYSTEM_PROMPT,
                model=StubModel(tools),
                tools=[tool(alias) for alias in tools.tools],
            )
            result = await Runner.run(agent, message)
        outputs = {
            item.raw_item["call_id"]: str(item.output)
            for item in result.new_items
            if isinstance(item, ToolCallOutputItem) and isinstance(item.raw_item, dict)
        }
        calls = [
            reported(
                tools.tools[item.raw_item.name].remote_name,
                json.loads(item.raw_item.arguments),
                outputs.get(item.raw_item.call_id),
            )
            for item in result.new_items
            if isinstance(item, ToolCallItem) and isinstance(item.raw_item, ResponseFunctionToolCall)
        ]
        return {"text": str(result.final_output), "tool_calls": calls}

    return answer


async def main() -> None:
    show(await attack(build))


if __name__ == "__main__":
    asyncio.run(main())
