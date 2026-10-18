"""Opt-in metadata reporting to a deployment-bound Martian discovery endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from martian_redteam.call_gate import CallBlocked, GateUnavailable, RuntimeCallGate
from martian_redteam.call_gate_schema import CallTarget
from martian_redteam.decision_schema import DECISION_REASONS, SecurityDecisionBatch, SecurityDecisionEvent
from martian_redteam.output_policy import OutputDecision
from martian_redteam.runtime_resources import RuntimeResourceRule, describe_resources
from martian_redteam.runtime_schema import (
    KubernetesRequest,
    NativeRuntimeBatch,
    NativeRuntimeEvent,
    ResourceReport,
    ResourceRuntimeBatch,
    ResourceRuntimeEvent,
)

log = logging.getLogger(__name__)


class RuntimeSpan:
    def __init__(self, event: ResourceRuntimeEvent) -> None:
        self.event = event
        self.outcome: Literal["success", "error", "unknown"] = "unknown"
        self.resources = ResourceReport()

    @property
    def id(self) -> uuid.UUID:
        return self.event.event_id


class RuntimeTrace:
    """One locally generated trace ID; never a customer/session identifier."""

    def __init__(
        self,
        reporter: RuntimeReporter,
        resource_rules: Mapping[tuple[str, str], Sequence[RuntimeResourceRule]] | None = None,
    ) -> None:
        self.reporter = reporter
        self.id = uuid.uuid4()
        self.resource_rules = dict(resource_rules or {})

    def resources(self, system: str, tool: str, result: dict[str, Any]) -> ResourceReport:
        return describe_resources(result, self.resource_rules.get((system, tool), ()), self.reporter.resource_reference)

    def security_decision(
        self,
        target: CallTarget,
        reason: str,
        *,
        task_ref: str | None = None,
        remote_task_ref: str | None = None,
        policy_ref: str | None = None,
    ) -> None:
        if not self.reporter.security_decisions:
            return
        control, decision = DECISION_REASONS[reason]
        self.reporter.record_decision(
            SecurityDecisionEvent.model_validate(
                {
                    "event_id": uuid.uuid4(),
                    "occurred_at": datetime.now(UTC),
                    "trace_id": self.id,
                    "target_kind": target.kind,
                    "system": target.system,
                    "name": target.name,
                    "control": control,
                    "decision": decision,
                    "reason": reason,
                    "task_ref": task_ref,
                    "remote_task_ref": remote_task_ref,
                    "policy_ref": policy_ref,
                }
            )
        )

    def output_decision(self, decision: OutputDecision) -> None:
        if not self.reporter.security_decisions:
            return
        self.reporter.record_decision(
            SecurityDecisionEvent.model_validate(
                {
                    "event_id": uuid.uuid4(),
                    "occurred_at": datetime.now(UTC),
                    "trace_id": self.id,
                    "target_kind": "output",
                    "system": "application",
                    "name": "final_response",
                    "control": "output_policy",
                    "decision": "admitted" if decision.allowed else "denied",
                    "reason": decision.reason,
                    "policy_ref": decision.policy_id,
                }
            )
        )

    @contextmanager
    def call(
        self,
        *,
        kind: Literal["model", "tool"],
        system: str,
        name: str,
        parent_event_id: uuid.UUID | None = None,
        kubernetes_request: KubernetesRequest | None = None,
    ) -> Iterator[RuntimeSpan]:
        if self.reporter.call_gate is not None:
            raise RuntimeError("Use async with trace.guarded_call(...) when a call gate is configured")
        with self._observe(
            kind=kind, system=system, name=name, parent_event_id=parent_event_id, kubernetes_request=kubernetes_request
        ) as span:
            yield span

    @asynccontextmanager
    async def guarded_call(
        self,
        *,
        kind: Literal["model", "tool"],
        system: str,
        name: str,
        parent_event_id: uuid.UUID | None = None,
        kubernetes_request: KubernetesRequest | None = None,
        transport_name: str | None = None,
        before_entry: Callable[[], None] | None = None,
        before_entry_async: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[RuntimeSpan]:
        target = CallTarget(kind=kind, system=system, name=name)
        if self.reporter.call_gate is not None:
            gate_target = target
            try:
                if transport_name is not None:
                    gate_target = CallTarget(kind=kind, system=system, name=transport_name)
                    await self.reporter.call_gate.before_call(gate_target)
                gate_target = target
                await self.reporter.call_gate.before_call(gate_target)
            except (CallBlocked, GateUnavailable) as exc:
                self.security_decision(
                    gate_target,
                    "call_gate_blocked" if isinstance(exc, CallBlocked) else "call_gate_unavailable",
                )
                raise
        if before_entry_async is not None:
            await before_entry_async()
        if before_entry is not None:
            before_entry()
        # Denied/unavailable calls never become executed runtime activity.
        with self._observe(
            kind=kind, system=system, name=name, parent_event_id=parent_event_id, kubernetes_request=kubernetes_request
        ) as span:
            yield span

    @contextmanager
    def _observe(
        self,
        *,
        kind: Literal["model", "tool"],
        system: str,
        name: str,
        parent_event_id: uuid.UUID | None = None,
        kubernetes_request: KubernetesRequest | None = None,
    ) -> Iterator[RuntimeSpan]:
        if kubernetes_request is not None and not self.reporter.native_kubernetes:
            raise ValueError("Kubernetes request metadata requires native_kubernetes=True")
        event_type = NativeRuntimeEvent if self.reporter.native_kubernetes else ResourceRuntimeEvent
        event = event_type.model_validate(
            {
                "event_id": uuid.uuid4(),
                "occurred_at": datetime.now(UTC),
                "trace_id": self.id,
                "parent_event_id": parent_event_id,
                "kind": kind,
                "system": system,
                "name": name,
                "outcome": "unknown",
                "duration_ms": 0,
                **({"kubernetes_request": kubernetes_request} if self.reporter.native_kubernetes else {}),
            }
        )
        span = RuntimeSpan(event)
        started = perf_counter()
        try:
            yield span
        except asyncio.CancelledError:
            span.outcome = "unknown"
            raise
        except Exception:
            span.outcome = "error"
            raise
        finally:
            completed = event.model_copy(
                update={
                    "occurred_at": datetime.now(UTC),
                    "outcome": span.outcome,
                    "duration_ms": min(86400000, round((perf_counter() - started) * 1000)),
                    "resource_report": span.resources if span.outcome == "success" else ResourceReport(),
                }
            )
            self.reporter.record(completed)


class RuntimeReporter:
    """Bounded volatile queue; one delivery attempt, with explicit loss counters.

    Export runs independently of business calls. Export errors cannot reinterpret a
    completed tool action, and an uncertain HTTP delivery is never replayed here.
    """

    def __init__(
        self,
        backend_url: str,
        binding_id: uuid.UUID,
        token_file: Path,
        *,
        allow_http: bool = False,
        native_kubernetes: bool = False,
        security_decisions: bool = False,
        call_gate: RuntimeCallGate | None = None,
    ) -> None:
        origin = urlsplit(backend_url)
        if (
            not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
            or origin.scheme not in ({"https", "http"} if allow_http else {"https"})
        ):
            raise ValueError("Runtime reporting requires a trusted HTTPS origin; HTTP is explicit local configuration")
        self.backend_url = backend_url.rstrip("/")
        self.binding_id = binding_id
        self.token_file = token_file
        self.native_kubernetes = native_kubernetes
        self.security_decisions = security_decisions
        if call_gate is not None and (
            call_gate.assignment.binding_id != binding_id or call_gate.backend_url != self.backend_url
        ):
            raise ValueError("Reporter and call gate must pin the same backend and deployment binding")
        self.call_gate = call_gate
        self._resource_key = hmac.new(
            self._credential().encode(),
            b"martian-resource-reference-v1",
            hashlib.sha256,
        ).digest()
        self._queue: deque[ResourceRuntimeEvent] = deque()
        self._decisions: deque[SecurityDecisionEvent] = deque()
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self.sent = 0
        self.delivery_failed = 0
        self.dropped = 0
        self.decisions_sent = 0
        self.decisions_delivery_failed = 0
        self.decisions_dropped = 0

    def _credential(self) -> str:
        if self.token_file.stat().st_size > 1024:
            raise ValueError("Runtime credential file exceeds limit")
        token = self.token_file.read_text().strip()
        if not token.startswith("mt_") or not 32 <= len(token) <= 128:
            raise ValueError("Invalid runtime credential file")
        return token

    def resource_reference(self, service: str, resource_type: str, native_id: str) -> str:
        identity = json.dumps([str(self.binding_id), service, resource_type, native_id], separators=(",", ":"))
        return hmac.new(self._resource_key, identity.encode(), hashlib.sha256).hexdigest()

    def trace(
        self,
        *,
        resource_rules: Mapping[tuple[str, str], Sequence[RuntimeResourceRule]] | None = None,
    ) -> RuntimeTrace:
        return RuntimeTrace(self, resource_rules)

    def record(self, event: ResourceRuntimeEvent) -> None:
        event_type = NativeRuntimeEvent if self.native_kubernetes else ResourceRuntimeEvent
        event = event_type.model_validate(event.model_dump())
        if self._task is None or self._task.done() or len(self._queue) >= 1000:
            self.dropped += 1
            log.warning("runtime_reporting event_dropped total=%d", self.dropped)
            return
        self._queue.append(event)

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Runtime reporter already started")
        if self.call_gate is not None:
            await self.call_gate.start()
        self._client = httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False)
        self._task = asyncio.create_task(self._run())

    def record_decision(self, event: SecurityDecisionEvent) -> None:
        if not self.security_decisions:
            raise ValueError("Security decision reporting requires explicit opt-in")
        event = SecurityDecisionEvent.model_validate(event.model_dump())
        if self._task is None or self._task.done() or len(self._decisions) >= 1000:
            self.decisions_dropped += 1
            log.warning("security_decisions event_dropped total=%d", self.decisions_dropped)
            return
        self._decisions.append(event)

    async def _send_decisions(self) -> None:
        if not self._decisions:
            return
        assert self._client is not None
        batch = SecurityDecisionBatch(events=[self._decisions.popleft() for _ in range(min(25, len(self._decisions)))])
        try:
            response = await self._client.post(
                f"{self.backend_url}/v1/discovery/telemetry-bindings/{self.binding_id}/security-decisions",
                headers={"Authorization": f"Bearer {self._credential()}", "Content-Type": "application/json"},
                content=batch.model_dump_json(),
            )
            response.raise_for_status()
            if len(response.content) > 1024 or response.json() != {"accepted": len(batch.events)}:
                raise ValueError("Unexpected security decision acknowledgement")
        except asyncio.CancelledError:
            self.decisions_delivery_failed += len(batch.events)
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self.decisions_delivery_failed += len(batch.events)
            log.warning(
                "security_decisions delivery_failed events=%d error_type=%s total=%d",
                len(batch.events),
                type(exc).__name__,
                self.decisions_delivery_failed,
            )
        else:
            self.decisions_sent += len(batch.events)
            log.info("security_decisions delivered=%d total=%d", len(batch.events), self.decisions_sent)

    async def _send(self) -> None:
        if not self._queue:
            return
        assert self._client is not None
        events = [self._queue.popleft().model_dump() for _ in range(min(25, len(self._queue)))]
        batch = (
            NativeRuntimeBatch.model_validate({"events": events})
            if self.native_kubernetes
            else ResourceRuntimeBatch.model_validate({"events": events})
        )
        endpoint = "native-events" if self.native_kubernetes else "resource-events"
        try:
            response = await self._client.post(
                f"{self.backend_url}/v1/discovery/telemetry-bindings/{self.binding_id}/{endpoint}",
                headers={"Authorization": f"Bearer {self._credential()}", "Content-Type": "application/json"},
                content=batch.model_dump_json(),
            )
            response.raise_for_status()
            if len(response.content) > 1024 or response.json() != {"accepted": len(batch.events)}:
                raise ValueError("Unexpected runtime event acknowledgement")
        except asyncio.CancelledError:
            self.delivery_failed += len(batch.events)
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self.delivery_failed += len(batch.events)
            log.warning(
                "runtime_reporting delivery_failed events=%d error_type=%s total=%d",
                len(batch.events),
                type(exc).__name__,
                self.delivery_failed,
            )
        else:
            self.sent += len(batch.events)
            log.info("runtime_reporting delivered=%d total=%d", len(batch.events), self.sent)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(2)
            await self._send()
            await self._send_decisions()

    async def close(self) -> None:
        if self.call_gate is not None:
            await self.call_gate.close()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # Shutdown does not replay an uncertain delivery or keep the app alive.
        self.dropped += len(self._queue)
        self._queue.clear()
        self.decisions_dropped += len(self._decisions)
        self._decisions.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        log.info(
            "runtime_reporting stopped sent=%d delivery_failed=%d dropped=%d",
            self.sent,
            self.delivery_failed,
            self.dropped,
        )
        log.info(
            "security_decisions stopped sent=%d delivery_failed=%d dropped=%d",
            self.decisions_sent,
            self.decisions_delivery_failed,
            self.decisions_dropped,
        )
