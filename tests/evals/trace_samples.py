"""One support-agent turn written in each of the three trace attribute styles, by hand.

An agent span holds a model call, which asks for the ``order_status`` tool, and the tool call.
Every style says the same thing: the model ``example-chat-1`` from ``openai``, 12 input and 8
output tokens, the question "Where is order 42?", the tool call ``call_1`` with
``{"order_id": "42"}`` and its result ``{"status": "shipped"}``, in the session
``conversation-1`` of the user ``alice``. The attributes are as an exporter sends them: the
OpenTelemetry GenAI conventions with structured values as JSON text, OpenInference flattened,
and MLflow with every value JSON-encoded. ``acme.*`` attributes belong to no convention.
"""

from __future__ import annotations

import json
from typing import Any

from martian.evals.model import Span, Trace

STYLES = ("otel_genai", "openinference", "mlflow")
QUESTION = "Where is order 42?"
ANSWER = "Order 42 has shipped."
MODEL = "example-chat-1"
ARGUMENTS = {"order_id": "42"}
RESULT = {"status": "shipped"}
UNKNOWN = {"acme.region": "eu-west"}
"""An attribute no convention defines, on each sample's agent span."""

_ROOT, _MODEL, _TOOL = "000000000000a001", "000000000000a002", "000000000000a003"


def _otel_genai() -> list[dict[str, Any]]:
    question = [{"role": "user", "parts": [{"type": "text", "content": QUESTION}]}]
    return [
        {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "support",
            "gen_ai.provider.name": "openai",
            "gen_ai.conversation.id": "conversation-1",
            "user.id": "alice",
            "gen_ai.input.messages": json.dumps(question),
            "gen_ai.output.messages": json.dumps(
                [{"role": "assistant", "parts": [{"type": "text", "content": ANSWER}], "finish_reason": "stop"}]
            ),
            **UNKNOWN,
        },
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "example-chat",
            "gen_ai.response.model": MODEL,
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.usage.output_tokens": 8,
            "gen_ai.input.messages": json.dumps(question),
            "gen_ai.output.messages": json.dumps(
                [
                    {
                        "role": "assistant",
                        "parts": [
                            {"type": "tool_call", "id": "call_1", "name": "order_status", "arguments": ARGUMENTS}
                        ],
                        "finish_reason": "tool_call",
                    }
                ]
            ),
        },
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "order_status",
            "gen_ai.tool.type": "function",
            "gen_ai.tool.call.id": "call_1",
            "gen_ai.tool.call.arguments": json.dumps(ARGUMENTS),
            "gen_ai.tool.call.result": json.dumps(RESULT),
        },
    ]


def _openinference() -> list[dict[str, Any]]:
    calls = "llm.output_messages.0.message.tool_calls.0.tool_call"
    return [
        {
            "openinference.span.kind": "AGENT",
            "input.value": QUESTION,
            "input.mime_type": "text/plain",
            "output.value": ANSWER,
            "output.mime_type": "text/plain",
            "session.id": "conversation-1",
            "user.id": "alice",
            **UNKNOWN,
        },
        {
            "openinference.span.kind": "LLM",
            "llm.model_name": MODEL,
            "llm.provider": "openai",
            "llm.system": "openai",
            "llm.invocation_parameters": json.dumps({"temperature": 0}),
            "llm.token_count.prompt": 12,
            "llm.token_count.completion": 8,
            "llm.token_count.total": 20,
            "llm.input_messages.0.message.role": "user",
            "llm.input_messages.0.message.content": QUESTION,
            "llm.output_messages.0.message.role": "assistant",
            f"{calls}.id": "call_1",
            f"{calls}.function.name": "order_status",
            f"{calls}.function.arguments": json.dumps(ARGUMENTS),
        },
        {
            "openinference.span.kind": "TOOL",
            "tool.name": "order_status",
            "tool.id": "call_1",
            "tool.description": "Look up an order",
            "input.value": json.dumps(ARGUMENTS),
            "input.mime_type": "application/json",
            "output.value": json.dumps(RESULT),
            "output.mime_type": "application/json",
        },
    ]


def _mlflow() -> list[dict[str, Any]]:
    def encoded(values: dict[str, Any]) -> dict[str, str]:
        return {key: json.dumps(value) for key, value in values.items()}

    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "order_status", "arguments": json.dumps(ARGUMENTS)},
    }
    return [
        {
            **encoded(
                {
                    "mlflow.spanType": "AGENT",
                    "mlflow.spanInputs": {"message": QUESTION},
                    "mlflow.spanOutputs": ANSWER,
                    # MLflow copies a trace's session and user onto its root span when it exports it.
                    "session.id": "conversation-1",
                    "user.id": "alice",
                }
            ),
            **UNKNOWN,
        },
        encoded(
            {
                "mlflow.spanType": "CHAT_MODEL",
                "mlflow.spanInputs": {"messages": [{"role": "user", "content": QUESTION}], "model": MODEL},
                "mlflow.spanOutputs": {
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": None, "tool_calls": [tool_call]},
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                "mlflow.chat.tokenUsage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
                "mlflow.llm.model": MODEL,
                "mlflow.llm.provider": "openai",
            }
        ),
        encoded({"mlflow.spanType": "TOOL", "mlflow.spanInputs": ARGUMENTS, "mlflow.spanOutputs": RESULT}),
    ]


_ATTRIBUTES = {"otel_genai": _otel_genai, "openinference": _openinference, "mlflow": _mlflow}
_NAMES = {
    "otel_genai": ("invoke_agent support", "chat example-chat-1", "execute_tool order_status"),
    "openinference": ("support agent", "ChatCompletion", "order_status"),
    "mlflow": ("support agent", "chat", "order_status"),
}


def sample_spans(style: str) -> list[Span]:
    """The turn's spans in one style, with nothing but their attributes, names and times."""
    root, model, tool = _ATTRIBUTES[style]()
    first, second, third = _NAMES[style]
    return [
        Span(id=_ROOT, name=first, status="ok", start_ms=1_000.0, end_ms=1_900.0, attributes=root),
        Span(id=_MODEL, parent_id=_ROOT, name=second, status="ok", start_ms=1_010.0, end_ms=1_400.0, attributes=model),
        Span(id=_TOOL, parent_id=_ROOT, name=third, status="ok", start_ms=1_400.0, end_ms=1_600.0, attributes=tool),
    ]


def sample_trace(style: str, trace_id: str, *, project_id: str = "default") -> Trace:
    """The turn as a trace that says nothing but its spans: no model, tokens, request, session or user."""
    return Trace(id=trace_id, project_id=project_id, name=f"{style} turn", state="ok", spans=sample_spans(style))
