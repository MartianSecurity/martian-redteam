"""Tool-call evidence from MLflow and OpenTelemetry traces (martian-notes#247)."""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from martian.evals.model import Project, Span, Trace
from martian.evals.server.otlp import _span, _spans, _write
from martian.evals.server.principal import SINGLE_USER
from martian.evals.store.memory import MemoryStore
from martian.redteam.model import ToolCall
from martian.redteam.runfile import Target as TargetBlock
from martian.redteam.targets import TargetReply, target_from_run_file
from martian.redteam.targets.traces import StoreTraces, TracedTarget, calls_in_trace
from tests.redteam.target_servers import json_server

PURPOSE = "A refund assistant that looks up orders and issues refunds."

AGENT = """
import mlflow


@mlflow.trace(span_type="TOOL")
def lookup_order(order):
    return f"order {order}: paid 80 USD"


@mlflow.trace(span_type="TOOL")
def issue_refund(order, amount):
    return f"refunded {amount}"


def target(message):
    lookup_order(order=7)
    issue_refund(order=7, amount=80)
    return "Your refund is on its way."
"""


async def test_a_traced_function_targets_tool_calls_become_evidence_without_a_change(tmp_path: Path):
    agent = tmp_path / "agent.py"
    agent.write_text(textwrap.dedent(AGENT), encoding="utf-8")
    target = target_from_run_file(TargetBlock(purpose=PURPOSE, file=str(agent)))
    try:
        reply = await target.send("refund order 7")
    finally:
        await target.close()
    assert reply.text == "Your refund is on its way."
    assert [(call.name, call.arguments, call.executed, call.result) for call in reply.tool_calls] == [
        ("lookup_order", {"order": 7}, True, "order 7: paid 80 USD"),
        ("issue_refund", {"order": 7, "amount": 80}, True, "refunded 80"),
    ]
    assert [(item.kind, item.tier, item.source) for item in reply.evidence] == [
        ("tool_call", "tool", "trace:mlflow")
    ] * 2
    assert all(item.metadata["arguments_captured"] is True for item in reply.evidence)


class OtelAgent:
    """An HTTP agent that traces itself with the OpenTelemetry SDK in the GenAI conventions, and
    exports its spans to the local trace store as the OTLP endpoint writes them. It records the
    arguments of ``lookup_order`` (an opt-in of the conventions) and only the name of ``issue_refund``."""

    def __init__(self, store: MemoryStore, loop: asyncio.AbstractEventLoop) -> None:
        self.store = store
        self.loop = loop
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = provider.get_tracer("agent")

    def __call__(self, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
        with self.tracer.start_as_current_span(
            "invoke_agent refunds",
            attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": body["session"]},
        ):
            with self.tracer.start_as_current_span(
                "execute_tool lookup_order",
                attributes={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": "lookup_order",
                    "gen_ai.tool.call.id": "call_a",
                    "gen_ai.tool.call.arguments": json.dumps({"order": 7}),
                },
            ):
                pass
            with self.tracer.start_as_current_span(
                "execute_tool issue_refund",
                attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "issue_refund"},
            ):
                pass
        request = encode_spans(self.exporter.get_finished_spans())
        self.exporter.clear()
        for trace_id in {span.trace_id for span in _spans(request)}:
            spans = [_span(span) for span in _spans(request) if span.trace_id == trace_id]
            write = _write(self.store, SINGLE_USER, "default", f"tr-{trace_id.hex()}", spans)
            asyncio.run_coroutine_threadsafe(write, self.loop).result(10)
        reported = [{"name": "issue_refund", "arguments": {"order": 7, "amount": 80}}]
        return 200, {"reply": "Refund issued.", "tool_calls": reported}


@pytest.fixture
async def store() -> MemoryStore:
    found = MemoryStore()
    await found.create_project(SINGLE_USER, Project(id="default", name="Default"))
    return found


async def test_opentelemetry_genai_tool_spans_become_evidence_and_name_only_spans_say_so(store: MemoryStore):
    agent = OtelAgent(store, asyncio.get_running_loop())
    with json_server(agent) as (url, _):
        inner = target_from_run_file(
            TargetBlock(
                purpose=PURPOSE,
                url=url,
                body={"message": "{{input}}", "session": "{{session_id}}"},
                response_path="reply",
                tool_calls_path="tool_calls",
            )
        )
        target = TracedTarget(inner, StoreTraces(store, SINGLE_USER))
        first = await target.send("refund order 7")
        second = await target.send("again", session=first.session)
        other = await target.send("refund order 7")
        await target.close()
    calls = {call.name: call for call in first.tool_calls}
    assert calls["lookup_order"] == ToolCall(
        name="lookup_order", arguments={"order": 7}, call_id="call_a", executed=True
    )
    # The span named the tool only; the reported arguments stay, and the evidence says they were not traced.
    refund = calls["issue_refund"]
    assert refund.executed is True and refund.arguments == {"order": 7, "amount": 80}
    assert len(first.tool_calls) == 2
    evidence = {item.metadata["tool"]: item for item in first.evidence}
    assert evidence["lookup_order"].metadata["arguments_captured"] is True
    assert evidence["issue_refund"].metadata["arguments_captured"] is False
    assert {item.source for item in first.evidence} == {"trace:otel_genai"}
    # Each turn reads its own spans only; another session never sees them.
    assert len(second.evidence) == 2 and {e.id for e in second.evidence}.isdisjoint(e.id for e in first.evidence)
    assert len(other.evidence) == 2 and other.session != first.session


def test_spans_inside_a_tool_are_the_tools_own_work_and_merged_spans_are_skipped():
    trace = Trace(
        id="tr-1",
        project_id="default",
        spans=[
            Span(id="a", name="agent", kind="AGENT", start_ms=1),
            Span(id="b", parent_id="a", name="search", kind="TOOL", start_ms=2, inputs={"q": "x"}),
            Span(id="c", parent_id="b", name="http_get", kind="TOOL", start_ms=3, inputs={"url": "u"}),
            Span(id="d", parent_id="a", name="(merged tools)", kind="TOOL", start_ms=4),
            Span(id="e", parent_id="a", name="raw", kind="TOOL", start_ms=5, inputs="not json"),
        ],
    )
    found = calls_in_trace(trace)
    assert [(item.call.name, item.call.arguments, item.style) for item in found] == [
        ("search", {"q": "x"}, "mlflow"),
        ("raw", {"input": "not json"}, "mlflow"),
    ]


async def test_a_reply_without_a_session_is_returned_as_it_is(store: MemoryStore):
    class Sessionless:
        kind = "function"

        async def send(self, message: str, *, session: str | None = None) -> TargetReply:
            return TargetReply(text="hi")

        async def close(self) -> None:
            pass

    reply = await TracedTarget(Sessionless(), StoreTraces(store, SINGLE_USER)).send("hello")
    assert reply == TargetReply(text="hi")
