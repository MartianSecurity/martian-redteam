"""Response-only invocation for the pinned MLflow provider adapters."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import requests
from mlflow.entities import Feedback
from mlflow.entities.assessment_source import AssessmentSource, AssessmentSourceType
from mlflow.environment_variables import MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS
from mlflow.gateway.constants import MLFLOW_GATEWAY_CALLER_HEADER, GatewayCaller
from mlflow.genai.judges.adapters.gateway_adapter import GatewayAdapter, _build_request
from mlflow.genai.judges.adapters.litellm_adapter import LiteLLMAdapter, _invoke_litellm
from mlflow.genai.judges.adapters.utils import (
    ChatCompletionError,
    _safe_parse_error,
    get_adapter,
    is_context_window_error,
    is_response_format_error,
)
from mlflow.genai.utils.llm_utils import _lookup_model_cost
from mlflow.metrics.genai.model_utils import _get_provider_instance, _parse_model_uri
from mlflow.tracing.constant import AssessmentMetadataKey
from mlflow.types.llm import ChatMessage
from mlflow.utils.workspace_context import get_request_workspace
from mlflow.utils.workspace_utils import WORKSPACE_HEADER_NAME
from pydantic import BaseModel, ValidationError

# A provider reply with one of these statuses carries no judgment, so sending the same request
# again cannot change a verdict that was already given.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504, 529})
# Rate limits and overload are retried for as long as a time budget lasts, not a number of times:
# a provider can throttle for minutes, and every retry is a request that carried no judgment.
_RATE_LIMIT_STATUS = frozenset({429, 529})
_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 300.0
MAX_RATE_LIMIT_WAIT_SECONDS = 3600.0
PROVIDER_RETRIES_KEY = "martian.judge.provider_retries"
_sleep = time.sleep
_clock = time.monotonic


class ProviderStatusError(ChatCompletionError):
    """A provider reply with an error status, with how long the provider asked the caller to wait."""

    def __init__(self, status_code: int, message: str, retry_after: float | None) -> None:
        super().__init__(status_code=status_code, message=message)
        self.retry_after = retry_after


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds a provider asked the caller to wait: ``retry-after-ms`` (OpenAI), or ``Retry-After``
    as seconds or an HTTP date. None when absent or unreadable."""
    lowered = {str(name).lower(): value for name, value in headers.items()}
    try:
        if (milliseconds := lowered.get("retry-after-ms")) is not None:
            seconds = float(milliseconds) / 1000
        elif (value := lowered.get("retry-after")) is None:
            return None
        else:
            try:
                seconds = float(value)
            except ValueError:
                seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _asked_wait(error: Exception) -> float | None:
    if isinstance(error, ProviderStatusError):
        return error.retry_after
    # LiteLLM's errors carry the provider's HTTP response.
    headers = getattr(getattr(error, "response", None), "headers", None)
    return _retry_after(headers) if isinstance(headers, Mapping) else None


def _no_judgment_received(error: Exception) -> bool:
    """True for rate limits, provider outages and connection failures: nothing was judged."""
    status = getattr(error, "status_code", None)
    if status is not None:
        return status in _RETRY_STATUS
    return isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, ConnectionError))


def _post_chat(endpoint: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    """One chat request, as MLflow's ``send_chat_request`` sends it with no retries, except that an
    error status keeps the provider's Retry-After, and a timeout or connection failure stays a
    ``requests`` error. MLflow's helper wraps those in an exception without a status, which the
    retry loop could not tell apart from a failure that must not be retried."""
    timeout: float = MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS.get()  # type: ignore[no-untyped-call]
    response = requests.post(
        url=endpoint, headers={"Content-Type": "application/json", **headers}, json=payload, timeout=timeout
    )
    if response.status_code == 400:
        message = _safe_parse_error(response)
        raise ChatCompletionError(
            status_code=400, message=message, is_context_window_error=is_context_window_error(message)
        )
    if response.status_code >= 400:
        raise ProviderStatusError(response.status_code, _safe_parse_error(response), _retry_after(response.headers))
    reply: dict[str, Any] = response.json()
    return reply


def _gateway_completion(
    provider_name: str,
    model: str,
    prompt: list[ChatMessage],
    schema: type[BaseModel],
    params: dict[str, Any] | None,
) -> Any:
    # The pinned helper is annotated with BaseProvider, whose HTTP implementations
    # add the wire adapter and headers used by MLflow's own Gateway judge.
    provider: Any = _get_provider_instance(provider_name, model)
    endpoint = provider.get_endpoint_url("llm/v1/chat")
    headers = dict(provider.headers or {})
    if provider_name == "gateway":
        headers[MLFLOW_GATEWAY_CALLER_HEADER] = GatewayCaller.JUDGE.value
        if workspace := get_request_workspace():
            headers[WORKSPACE_HEADER_NAME] = workspace

    def send(*, structured: bool) -> dict[str, Any]:
        payload = _build_request(prompt, None, schema, structured, params)
        return _post_chat(
            endpoint=endpoint, headers=headers, payload=provider.adapter_class.chat_to_model(payload, provider.config)
        )

    try:
        raw = send(structured=True)
    except ChatCompletionError as error:
        if error.status_code != 400 or error.is_context_window_error or not is_response_format_error(error.message):
            raise
        # A provider can decline the request format before returning a judgment.
        raw = send(structured=False)
    return provider.adapter_class.model_to_chat(raw, provider.config)


def _litellm_completion(
    model: str,
    prompt: list[ChatMessage],
    schema: type[BaseModel],
    params: dict[str, Any] | None,
) -> Any:
    messages = [{"role": message.role, "content": message.content} for message in prompt]

    def send(*, structured: bool) -> Any:
        return _invoke_litellm(
            litellm_model=model,
            messages=messages,
            tools=[],
            num_retries=0,
            response_format=schema,
            include_response_format=structured,
            inference_params=params,
        )

    try:
        return send(structured=True)
    except Exception as error:
        if getattr(error, "status_code", None) != 400 or not is_response_format_error(str(error)):
            raise
        return send(structured=False)


def _provider_failure(stage: str, error: Exception) -> ValueError:
    # MLflow's outer retry classifier inspects type, status attributes and text.
    # Only these fixed messages may cross into its error assessment.
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return ValueError(f"Judge {stage} failed [judge_provider_auth]. Check provider credentials and model access.")
    if status == 429:
        return ValueError(f"Judge {stage} failed [judge_provider_throttled]. Check the provider quota.")
    return ValueError(f"Judge {stage} failed [judge_provider_error]. Check the model and provider configuration.")


def invoke_response_judge(
    *,
    model_uri: str,
    prompt: list[ChatMessage],
    assessment_name: str,
    response_format: type[BaseModel],
    inference_params: dict[str, Any] | None,
    stage: Literal["draft", "review"],
    max_retries: int = 0,
    rate_limit_wait_seconds: float = 0.0,
) -> Feedback:
    # Reuse routing, credentials and wire transforms from MLflow 3.16.1.
    # Calling the judge adapter itself would dispatch tools and drop envelope fields.
    try:
        provider, model = _parse_model_uri(model_uri)
        adapter = get_adapter(model_uri, prompt)
    except Exception:
        raise ValueError(f"Judge {stage} failed [judge_provider_config]. Check the provider/model URI.") from None

    retries = 0  # every retry, recorded in the feedback metadata
    failures = 0  # retries after a server error or connection failure, limited by max_retries
    started = _clock()
    while True:
        try:
            if isinstance(adapter, GatewayAdapter):
                completion = _gateway_completion(provider, model, prompt, response_format, inference_params)
            elif isinstance(adapter, LiteLLMAdapter):
                completion = _litellm_completion(f"{provider}/{model}", prompt, response_format, inference_params)
            else:
                raise ValueError("A direct chat completion provider is required")
            break
        except Exception as error:
            # Only a failure that returned no judgment is retried. A reply that was received is
            # final, valid or not: a second attempt at it would be a retry towards a pass.
            if not _no_judgment_received(error):
                raise _provider_failure(stage, error) from None
            asked = _asked_wait(error)
            backoff = min(_MAX_BACKOFF_SECONDS, 2.0**retries + random.uniform(0.0, 0.5))
            if getattr(error, "status_code", None) in _RATE_LIMIT_STATUS:
                # Wait as long as the provider asks; stop when that would outlast the budget.
                remaining = rate_limit_wait_seconds - (_clock() - started)
                wait = asked if asked is not None else min(backoff, remaining)
                if remaining <= 0 or wait > remaining:
                    raise _provider_failure(stage, error) from None
            else:
                if failures >= max_retries:
                    raise _provider_failure(stage, error) from None
                failures += 1
                wait = min(asked, _MAX_BACKOFF_SECONDS) if asked is not None else backoff
            _sleep(wait)
            retries += 1

    if not completion.choices:
        raise ValueError(f"Judge {stage} returned no choices [judge_invalid_response]")
    choice = completion.choices[0]
    message = choice.message
    if (
        message.tool_calls
        or getattr(message, "function_call", None)
        or choice.finish_reason
        in {
            "tool_calls",
            "function_call",
            "tool_use",
        }
    ):
        raise ValueError(f"Judge {stage} returned a tool call [judge_unexpected_tool_call]")

    content = message.content
    if isinstance(content, list):
        content = "\n".join(part.text for part in content if getattr(part, "text", None))
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"Judge {stage} returned no text [judge_invalid_response]")
    content = content.strip()
    # Allow a single enclosing fence from JSON-in-text providers, with no surrounding prose.
    fence, _, body = content.partition("\n")
    if fence.strip().lower() in {"```", "```json"} and body.endswith("\n```"):
        content = body.removesuffix("\n```")
    try:
        judgment = response_format.model_validate_json(content)
    except ValidationError:
        raise ValueError(f"Judge {stage} reply failed schema validation [judge_invalid_schema]") from None

    metadata: dict[str, Any] = {PROVIDER_RETRIES_KEY: retries}
    if (usage := completion.usage) is not None:
        prompt_tokens = usage.prompt_tokens
        completion_tokens = usage.completion_tokens
        if prompt_tokens is not None:
            metadata[AssessmentMetadataKey.JUDGE_INPUT_TOKENS] = prompt_tokens
        if completion_tokens is not None:
            metadata[AssessmentMetadataKey.JUDGE_OUTPUT_TOKENS] = completion_tokens
        if (
            prompt_tokens is not None
            and completion_tokens is not None
            and (cost := _lookup_model_cost(model_uri, prompt_tokens, completion_tokens)) is not None
        ):
            metadata[AssessmentMetadataKey.JUDGE_COST] = cost
    return Feedback(
        name=assessment_name,
        value=judgment.model_dump()["result"],
        source=AssessmentSource(source_type=AssessmentSourceType.LLM_JUDGE, source_id=model_uri),
        metadata=metadata,
    )
