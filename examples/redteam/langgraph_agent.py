"""A LangGraph agent as a red team target, its tools bound through the recording proxy.

The agent is a two-node graph, a model node and LangGraph's ``ToolNode``, routed by
``tools_condition``. Its tools come from its session with ``bind_tools`` and become LangChain
``StructuredTool``s. The model node is a stub that plays a model falling for the attack, so the
example needs no key. The agent reports its tool calls from the graph's messages.

    uv run python -m examples.redteam.langgraph_agent
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

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


def build(session: AssessmentSession) -> Answer:
    async def answer(message: str, session_id: str) -> dict[str, Any]:
        async with bind_tools(session, action_id=session_id) as tools:

            def tool(alias: str) -> StructuredTool:
                definition = tools.tools[alias].definition

                async def call(**arguments: Any) -> str:
                    return result_text(await tools.call(alias, arguments, call_id=alias))

                return StructuredTool(
                    name=alias,
                    description=definition["description"],
                    args_schema=definition["input_schema"],
                    coroutine=call,
                )

            def model(state: MessagesState) -> dict[str, Any]:
                """A model that looks the order up, asks for the refund, then answers."""
                asked = sum(len(item.tool_calls) for item in state["messages"] if isinstance(item, AIMessage))
                if asked < 2:
                    name, arguments = (LOOKUP, REFUND)[asked]
                    call = {"name": alias_of(name, tools), "args": arguments, "id": f"call_{asked}"}
                    return {"messages": [AIMessage(content="", tool_calls=[call])]}
                return {"messages": [AIMessage(content=FINAL_TEXT)]}

            graph = StateGraph(MessagesState)
            graph.add_node("model", model)
            graph.add_node("tools", ToolNode([tool(alias) for alias in tools.tools]))
            graph.add_edge(START, "model")
            graph.add_conditional_edges("model", tools_condition)
            graph.add_edge("tools", "model")
            state = await graph.compile().ainvoke({"messages": [SystemMessage(SYSTEM_PROMPT), ("user", message)]})
        messages = state["messages"]
        results = {item.tool_call_id: str(item.content) for item in messages if isinstance(item, ToolMessage)}
        calls = [
            reported(tools.tools[call["name"]].remote_name, call["args"], results.get(call["id"] or ""))
            for item in messages
            if isinstance(item, AIMessage)
            for call in item.tool_calls
        ]
        return {"text": str(messages[-1].content), "tool_calls": calls}

    return answer


async def main() -> None:
    show(await attack(build))


if __name__ == "__main__":
    asyncio.run(main())
