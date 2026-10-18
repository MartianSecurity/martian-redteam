"""Opt-in, per-call control decisions for an exact customer-pinned target list."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from martian_redteam.call_gate_schema import (
    BlockReceipt,
    CallDecision,
    CallTarget,
    DecisionRequest,
    GateAssignment,
    GateHeartbeat,
    GateModel,
)

log = logging.getLogger(__name__)


class CallBlocked(RuntimeError):
    """The selected callable was not entered because a reviewed control denied it."""

    def __init__(self, decision: CallDecision) -> None:
        super().__init__(f"Call blocked by Martian control {decision.control_id}")
        self.decision = decision


class GateUnavailable(RuntimeError):
    """No usable decision: the configured callable must not be entered."""


class RuntimeCallGate:
    def __init__(
        self,
        backend_url: str,
        assignment: GateAssignment,
        token_file: Path,
        *,
        allow_http: bool = False,
        kubernetes_token_file: Path | None = None,
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
            raise ValueError("Call gate requires a trusted HTTPS origin; HTTP is explicit local configuration")
        self.backend_url = backend_url.rstrip("/")
        self.assignment = GateAssignment.model_validate(assignment.model_dump())
        self.token_file = token_file
        self.kubernetes_token_file = kubernetes_token_file
        self.process_id = uuid.uuid4()
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None

    def _credential(self) -> str:
        if self.token_file.stat().st_size > 1024:
            raise ValueError("Gate credential file exceeds limit")
        token = self.token_file.read_text().strip()
        if not token.startswith("mg_") or not 32 <= len(token) <= 128:
            raise ValueError("Invalid gate credential file")
        return token

    async def _post(self, endpoint: str, body: GateModel) -> bytes:
        if self._client is None:
            raise GateUnavailable("Start the customer call gate before using guarded calls")
        try:
            async with asyncio.timeout(3):
                headers = {"Authorization": f"Bearer {self._credential()}", "Content-Type": "application/json"}
                if endpoint == "heartbeat" and self.kubernetes_token_file is not None:
                    if self.kubernetes_token_file.stat().st_size > 16 * 1024:
                        raise ValueError("Kubernetes credential exceeds limit")
                    pod_token = self.kubernetes_token_file.read_text().strip()
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{32,16384}", pod_token):
                        raise ValueError("Invalid Kubernetes credential file")
                    headers["X-Martian-Kubernetes-Token"] = pod_token
                async with self._client.stream(
                    "POST",
                    f"{self.backend_url}/v1/discovery/call-gates/{self.assignment.gate_id}/{endpoint}",
                    headers=headers,
                    content=body.model_dump_json(),
                ) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 16 * 1024:
                            raise ValueError("Oversized gate response")
                        data.extend(chunk)
                    return bytes(data)
        except (httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
            # Never include a URL, credential, body or tool argument in this error.
            raise GateUnavailable(
                f"Call gate decision unavailable ({type(exc).__name__}); configured call stopped"
            ) from None

    async def _heartbeat(self) -> None:
        body = GateHeartbeat(**self.assignment.model_dump(), process_id=self.process_id)
        try:
            reply = GateHeartbeat.model_validate_json(await self._post("heartbeat", body))
            if reply != body:
                raise ValueError("Assignment acknowledgement mismatch")
        except ValueError:
            raise GateUnavailable("Call gate assignment acknowledgement invalid") from None

    async def start(self) -> None:
        if self._client is not None:
            raise RuntimeError("Call gate already started")
        self._client = httpx.AsyncClient(timeout=2, follow_redirects=False, trust_env=False)
        try:
            await self._heartbeat()
        except BaseException:
            await self._client.aclose()
            self._client = None
            raise
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._heartbeat()
            except GateUnavailable:
                log.warning("call_gate heartbeat_unavailable; configured calls still require a fresh decision")

    async def before_call(self, target: CallTarget) -> CallDecision | None:
        """Outside-assignment calls are untouched. Selected calls get exactly one decision attempt."""
        if target not in self.assignment.targets:
            return None
        body = DecisionRequest(
            binding_id=self.assignment.binding_id, process_id=self.process_id, request_id=uuid.uuid4(), target=target
        )
        try:
            decision = CallDecision.model_validate_json(await self._post("decisions", body))
            if (
                decision.gate_id != self.assignment.gate_id
                or DecisionRequest.model_validate(decision.model_dump(include=set(DecisionRequest.model_fields)))
                != body
            ):
                raise ValueError("Decision scope mismatch")
        except ValueError:
            raise GateUnavailable("Call gate decision has invalid scope or format; configured call stopped") from None
        if not decision.allowed:
            try:
                await self._post("block-receipts", BlockReceipt(process_id=self.process_id, request_id=body.request_id))
            except GateUnavailable:
                log.warning("call_gate block_receipt_unavailable; selected call was stopped")
            raise CallBlocked(decision)
        return decision

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
