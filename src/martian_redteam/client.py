"""HTTP client for the Martian Agents Aegra API."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError
from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt, wait_exponential

from martian._privacy import RedactionPolicy
from martian_redteam.catalog.vulnerabilities import CATALOG_VERSION
from martian_redteam.config import Settings
from martian_redteam.protocol import (
    extract_graph_revision,
    failed_run_status,
    graph_state,
    parse_retry_after,
    parse_wait_body,
)
from martian_redteam.types import EvaluatorContext, EvaluatorOutput, SimulatorContext, SimulatorOutput

_RETRYABLE_STATUS = frozenset({429})
_WAIT_BACKOFF = wait_exponential(multiplier=0.5, min=0.5, max=8)


class ClientError(RuntimeError):
    """Transport, HTTP, or schema failure talking to Martian Agents."""

    code: str | None = None


class RetryableClientError(ClientError):
    """A failure known to precede execution (connect or admission 429)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableClientError)


def _retry_wait(retry_state: RetryCallState) -> float:
    exception = retry_state.outcome.exception() if retry_state.outcome is not None else None
    if isinstance(exception, RetryableClientError) and exception.retry_after is not None:
        return min(exception.retry_after, 30.0)
    return float(_WAIT_BACKOFF(retry_state))


def _status_error(assistant_id: str, response: httpx.Response) -> ClientError:
    retry_after = parse_retry_after(response.headers.get("Retry-After"))
    suffix = f" (Retry-After: {retry_after:.0f}s)" if retry_after is not None else ""
    if response.status_code == 429:
        message = (
            f"{assistant_id} run failed: HTTP 429{suffix}. "
            "This may be a per-key quota or a shared WAF limit (GitHub-hosted runners share IPs)."
        )
    else:
        message = f"{assistant_id} run failed: HTTP {response.status_code}{suffix}"
    if response.status_code in _RETRYABLE_STATUS:
        error: ClientError = RetryableClientError(message, retry_after=retry_after)
    else:
        error = ClientError(message)
    error.code = f"http_{response.status_code}"
    return error


class Client:
    """POST /runs/wait against simulator and evaluator graphs."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or Settings()
        if not self.settings.api_key.get_secret_value():
            raise ClientError(f"{self.settings.model_config['env_prefix']}API_KEY is not set")
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        headers = {
            "X-API-Key": self.settings.api_key.get_secret_value(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._http = httpx.AsyncClient(
            base_url=self.settings.api_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return RedactionPolicy.from_secrets(self.settings.api_key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def simulate(self, context: SimulatorContext, *, prompt: str | None = None) -> SimulatorOutput:
        content = prompt or (
            f"Generate a baseline attack for {context.vulnerability}/{context.vulnerability_type} "
            f"against a system whose purpose is: {context.purpose}"
        )
        payload = await self._wait("simulator", content, context.model_dump(exclude_none=True))
        try:
            return SimulatorOutput.model_validate(payload)
        except ValidationError:
            raise ClientError("simulator output failed schema validation") from None

    async def evaluate(
        self,
        context: EvaluatorContext,
        *,
        attack: str,
        target_output: str,
        tools_called: list[str] | None = None,
    ) -> EvaluatorOutput:
        tools = tools_called or []
        content = f"Attack:\n{attack}\n\nTarget reply:\n{target_output}\n\ntools_called: {tools}"
        payload = await self._wait("evaluator", content, context.model_dump(exclude_none=True))
        try:
            return EvaluatorOutput.model_validate(payload)
        except ValidationError:
            raise ClientError("evaluator output failed schema validation") from None

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=_retry_wait,
        reraise=True,
    )
    async def _wait(self, assistant_id: str, content: str, context: dict[str, Any]) -> dict[str, Any]:
        body = {
            "assistant_id": assistant_id,
            "input": {"messages": [{"role": "human", "content": content}]},
            "context": context,
            "on_completion": "delete",
            "metadata": {
                "sdk": "martian-redteam",
                "catalog_version": CATALOG_VERSION,
            },
        }
        try:
            response = await self._http.post("/runs/wait", json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise _status_error(assistant_id, error.response) from error
        except httpx.ConnectTimeout as error:
            raise RetryableClientError(f"{assistant_id} connect timed out") from error
        except httpx.ConnectError as error:
            raise RetryableClientError(f"{assistant_id} connect failed") from error
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as error:
            raise ClientError(f"{assistant_id} run timed out; not retrying because success is unknown") from error
        except httpx.RequestError as error:
            raise ClientError(f"{assistant_id} request failed ({type(error).__name__})") from error

        try:
            envelope = parse_wait_body(response.text)
        except ValueError as error:
            raise ClientError(f"{assistant_id} returned non-JSON") from error

        status = failed_run_status(envelope)
        if status is not None:
            raise ClientError(f"{assistant_id} run ended with status {status}")

        state = graph_state(envelope)
        revision = extract_graph_revision(state) or extract_graph_revision(envelope)
        if revision:
            state = {**state, "graph_revision": revision}
        return state
