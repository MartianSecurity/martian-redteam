"""HTTP client for the Martian Agents Aegra API."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from martian_redteam.config import Settings
from martian_redteam.types import EvaluatorContext, EvaluatorOutput, SimulatorContext, SimulatorOutput

_RETRYABLE_STATUS = frozenset({429, 503})


class ClientError(RuntimeError):
    """Transport, HTTP, or schema failure talking to Martian Agents."""


class RetryableClientError(ClientError):
    """Transient failure that may be retried (connect, 429, 503)."""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableClientError)


def _status_error(assistant_id: str, response: httpx.Response) -> ClientError:
    retry_after = response.headers.get("Retry-After")
    suffix = f" (Retry-After: {retry_after})" if retry_after else ""
    message = f"{assistant_id} run failed: HTTP {response.status_code}{suffix}"
    if response.status_code in _RETRYABLE_STATUS:
        return RetryableClientError(message)
    return ClientError(message)


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
            raise ClientError("MARTIAN_API_KEY is not set")
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        headers = {
            "X-API-Key": self.settings.api_key.get_secret_value(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._http = httpx.AsyncClient(
            base_url=str(self.settings.api_url).rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

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
        except ValidationError as error:
            raise ClientError(f"simulator output failed schema validation: {error}") from error

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
        except ValidationError as error:
            raise ClientError(f"evaluator output failed schema validation: {error}") from error

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    async def _wait(self, assistant_id: str, content: str, context: dict[str, Any]) -> dict[str, Any]:
        body = {
            "assistant_id": assistant_id,
            "input": {"messages": [{"role": "human", "content": content}]},
            "context": context,
        }
        try:
            response = await self._http.post("/runs/wait", json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise _status_error(assistant_id, error.response) from error
        except httpx.ConnectTimeout as error:
            raise RetryableClientError(f"{assistant_id} connect timed out") from error
        except httpx.ConnectError as error:
            raise RetryableClientError(f"{assistant_id} connect failed: {error}") from error
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as error:
            raise ClientError(f"{assistant_id} run timed out; not retrying because success is unknown") from error
        except httpx.RequestError as error:
            raise ClientError(f"{assistant_id} request failed: {error}") from error

        try:
            payload = response.json()
        except ValueError as error:
            raise ClientError(f"{assistant_id} returned non-JSON") from error
        if not isinstance(payload, dict):
            raise ClientError(f"{assistant_id} returned a non-object JSON body")
        return payload
