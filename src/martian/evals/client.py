"""Minimal Backend client. Uploads are idempotent on request_id, so transient failures retry."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import httpx

from martian._errors import message, reference
from martian._privacy import safe_text

RETRY_STATUS = {429, 502, 503, 504}
MAX_RETRY_AFTER = 30
# Backend answers this 409 when two requests raced on one upload; the retry replays or lands.
RETRY_CONFLICT = "conflicted with a concurrent request"


class BackendError(RuntimeError):
    def __init__(
        self,
        status: int,
        detail: str,
        retry_after: float = 0.0,
        *,
        request_id: str | None = None,
        code: str | None = None,
    ) -> None:
        self.status = status
        self.detail = detail
        self.retry_after = retry_after
        self.code = code or (f"http_{status}" if status else "invalid_response")
        self.request_id = reference(request_id)
        # Status-zero messages originate in the SDK's local receipt validators,
        # not in HTTP/provider bodies. Keep their useful integrity explanation.
        detail_message = safe_text(detail) + " " if not status and code is None else ""
        super().__init__("Evals: " + detail_message + message(self.code, request_id=self.request_id))


class Backend:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        trust_env: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=False,
            trust_env=trust_env,
            transport=transport,
        )
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def _send(self, method: str, path: str, *, json: dict[str, Any] | None = None, attempts: int = 1) -> dict[str, Any]:
        last = BackendError(0, "no Backend request attempted")
        for attempt in range(attempts):
            request_id = reference(json.get("request_id")) if json else None
            try:
                response = self._client.request(method, path, json=json)
            except httpx.TransportError as exc:
                last = BackendError(
                    0, f"could not reach Backend ({type(exc).__name__})", code="connection_error", request_id=request_id
                )
            else:
                if 200 <= response.status_code < 300:
                    try:
                        body = response.json()
                    except ValueError:
                        raise BackendError(
                            response.status_code,
                            "unexpected response body",
                            code="invalid_response",
                            request_id=request_id,
                        ) from None
                    if not isinstance(body, dict):
                        raise BackendError(
                            response.status_code,
                            "unexpected response body",
                            code="invalid_response",
                            request_id=request_id,
                        )
                    return body
                detail = ""
                with suppress(ValueError):
                    error_body = response.json()
                    detail = str(error_body.get("detail", "")) if isinstance(error_body, dict) else ""
                try:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                except ValueError:
                    retry_after = 0.0
                if not math.isfinite(retry_after):
                    retry_after = 0.0
                # Validation/error bodies may echo submitted target content or credentials.
                last = BackendError(
                    response.status_code,
                    response.reason_phrase,
                    retry_after,
                    request_id=reference(response.headers.get("x-request-id")) or request_id,
                )
                retryable = response.status_code in RETRY_STATUS or (
                    response.status_code == 409 and RETRY_CONFLICT in detail
                )
                if not retryable:
                    raise last
            if attempt + 1 < attempts:
                delay = 0.5 * 2**attempt
                if isinstance(last, BackendError) and last.status == 429:
                    delay = min(MAX_RETRY_AFTER, max(delay, last.retry_after))
                self._sleep(delay)
        raise last

    def pull_dataset(self, name: str) -> dict[str, Any]:
        from urllib.parse import quote

        return self._send("GET", "/v1/evals/ci/datasets/" + quote(name, safe=""), attempts=3)

    def upload_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._send("POST", "/v1/evals/ci/runs", json=payload, attempts=3)

    def upload_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._send("POST", "/v1/evals/ci/sessions", json=payload, attempts=3)

    def get_session(self, session_id: str) -> dict[str, Any]:
        from uuid import UUID

        return self._send("GET", f"/v1/evals/ci/sessions/{UUID(session_id)}", attempts=3)
