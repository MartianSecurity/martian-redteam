"""Response-only invocation for the pinned MLflow provider adapters."""

from __future__ import annotations

import math
import os
import random
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal

import requests
from mlflow.entities import AssessmentSource, AssessmentSourceType, Feedback
from mlflow.environment_variables import MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS, MLFLOW_MODEL_CATALOG_URI
from mlflow.types.llm import ChatMessage
from pydantic import BaseModel, ValidationError

from martian.evals._endpoint_errors import names_unknown_model
from martian.evals._mlflow import (
    MLFLOW_GATEWAY_CALLER_HEADER,
    WORKSPACE_HEADER_NAME,
    AssessmentMetadataKey,
    ChatCompletionError,
    GatewayAdapter,
    GatewayCaller,
    LiteLLMAdapter,
    build_chat_request,
    error_text,
    get_adapter,
    get_request_workspace,
    invoke_litellm,
    is_context_window_error,
    is_response_format_error,
    lookup_model_cost,
    parse_model_uri,
    provider_instance,
)
from martian.evals._pinned_address import AddressPolicy, AddressRefused, pinned_session
from martian.evals.cache import HIT_KEY, key_address, key_headers

if TYPE_CHECKING:
    from martian.evals.cache import ModelCallCache

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
# "json_schema" when the reply was requested with structured output, "none" when the endpoint
# declined it and the stage asked again without.
RESPONSE_FORMAT_KEY = "martian.judge.response_format"
# The model the provider says answered, from the reply's own ``model`` field
# (``gpt-4o-mini-2024-07-18`` for ``openai:/gpt-4o-mini``). Absent when the reply names none.
MODEL_VERSION_KEY = "martian.judge.model_version"
ENDPOINT_SETTING = "the judge's endpoint"
_sleep = time.sleep
_clock = time.monotonic


class ProviderStatusError(ChatCompletionError):
    """A provider reply with an error status, with how long the provider asked the caller to wait."""

    def __init__(self, status_code: int, message: str, retry_after: float | None) -> None:
        super().__init__(status_code=status_code, message=message)
        self.retry_after = retry_after


class NotAChatCompletion(Exception):
    """A successful reply that is not an OpenAI chat completion: the address is not a chat endpoint."""


class StructuredReplyError(ValueError):
    """A received completion failed validation but may still report billable usage."""

    def __init__(self, message: str, metadata: dict[str, Any]) -> None:
        super().__init__(message)
        self.metadata = metadata


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
    return _chat_reply(
        requests.post(
            url=endpoint, headers={"Content-Type": "application/json", **headers}, json=payload, timeout=timeout
        )
    )


def _post_chat_pinned(
    endpoint: str, headers: dict[str, str], payload: dict[str, Any], *, address_policy: AddressPolicy
) -> dict[str, Any]:
    """:func:`_post_chat` over a connection that goes only to an address ``address_policy`` allows,
    the one the endpoint's name resolved to when the connection opened (:mod:`martian.evals._pinned_address`):
    no proxy, no redirect, and a refused address is :class:`AddressRefused`, never a call to the name."""
    timeout: float = MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS.get()  # type: ignore[no-untyped-call]
    with pinned_session(address_policy) as session:
        response = session.post(
            url=endpoint, headers={"Content-Type": "application/json", **headers}, json=payload, timeout=timeout
        )
    return _chat_reply(response)


def _chat_reply(response: requests.Response) -> dict[str, Any]:
    """The chat completion a provider's reply holds, or the error its status stands for."""
    if response.status_code == 400:
        message = error_text(response)
        raise ChatCompletionError(
            status_code=400, message=message, is_context_window_error=is_context_window_error(message)
        )
    if response.status_code >= 400:
        raise ProviderStatusError(response.status_code, error_text(response), _retry_after(response.headers))
    try:
        reply = response.json()
    except ValueError:
        raise NotAChatCompletion from None
    if not isinstance(reply, dict):
        raise NotAChatCompletion
    return reply


def _gateway_completion(
    provider_name: str,
    model: str,
    prompt: list[ChatMessage],
    schema: type[BaseModel],
    params: dict[str, Any] | None,
    extra_headers: Mapping[str, str],
    base_url: str | None = None,
    address_policy: AddressPolicy | None = None,
) -> tuple[Any, bool]:
    """The completion, and whether it was requested with structured output. ``base_url`` is an
    OpenAI-compatible endpoint's base address, used instead of the one in the environment. With an
    ``address_policy`` the request is made on a pinned connection (:func:`_post_chat_pinned`)."""
    # The pinned helper is annotated with BaseProvider, whose HTTP implementations
    # add the wire adapter and headers used by MLflow's own Gateway judge.
    provider: Any = provider_instance(provider_name, model)
    endpoint = f"{base_url.rstrip('/')}/chat/completions" if base_url else provider.get_endpoint_url("llm/v1/chat")
    headers = dict(provider.headers or {})
    if provider_name == "gateway":
        headers[MLFLOW_GATEWAY_CALLER_HEADER] = GatewayCaller.JUDGE.value
        if workspace := get_request_workspace():
            headers[WORKSPACE_HEADER_NAME] = workspace
    # The judge's own headers come last, as MLflow's judges apply extra_headers.
    headers.update(extra_headers)

    def send(*, structured: bool) -> dict[str, Any]:
        payload = build_chat_request(prompt, None, schema, structured, params)
        wire = provider.adapter_class.chat_to_model(payload, provider.config)
        if address_policy is None:
            return _post_chat(endpoint=endpoint, headers=headers, payload=wire)
        return _post_chat_pinned(endpoint, headers, wire, address_policy=address_policy)

    structured = True
    try:
        raw = send(structured=True)
    except ChatCompletionError as error:
        if error.status_code != 400 or error.is_context_window_error or not is_response_format_error(error.message):
            raise
        # A provider can decline the request format before returning a judgment.
        structured = False
        raw = send(structured=False)
    try:
        return provider.adapter_class.model_to_chat(raw, provider.config), structured
    except Exception:  # noqa: BLE001 - any reply the adapter cannot read is not a chat completion
        raise NotAChatCompletion from None


def _litellm_completion(
    model: str,
    prompt: list[ChatMessage],
    schema: type[BaseModel],
    params: dict[str, Any] | None,
    extra_headers: Mapping[str, str],
) -> tuple[Any, bool]:
    messages = [{"role": message.role, "content": message.content} for message in prompt]

    def send(*, structured: bool) -> Any:
        return invoke_litellm(
            litellm_model=model,
            messages=messages,
            tools=[],
            num_retries=0,
            response_format=schema,
            include_response_format=structured,
            inference_params=params,
            extra_headers=dict(extra_headers) or None,
        )

    try:
        return send(structured=True), True
    except Exception as error:
        if getattr(error, "status_code", None) != 400 or not is_response_format_error(str(error)):
            raise
        return send(structured=False), False


def _setting_names(provider: str, base_url: str | None = None) -> tuple[str, str]:
    """The settings that hold the provider's key and address, named in failure messages. The
    openai provider reads them as MLflow does: ``OPENAI_API_BASE``, else ``OPENAI_BASE_URL``,
    unless the judge names its endpoint."""
    if base_url:
        return "OPENAI_API_KEY", ENDPOINT_SETTING
    if provider == "openai":
        unset = os.environ.get("OPENAI_API_BASE") is None and os.environ.get("OPENAI_BASE_URL")
        return "OPENAI_API_KEY", "OPENAI_BASE_URL" if unset else "OPENAI_API_BASE"
    return f"the {provider} provider's key", f"the {provider} provider's address"


def _provider_failure(
    caller: str, error: Exception, provider: str, base_url: str | None = None, setting: str = "judge"
) -> ValueError:
    """The caller's own message for a failed request, naming the setting to check.

    MLflow's outer retry classifier inspects type, status attributes and text, so only these
    fixed messages may cross into its error assessment. The provider's error text is read to
    choose one and is never repeated. ``caller`` names who asked (``Judge draft``), and suite file
    settings are named as ``<setting>.<name>`` (``judge.model``, ``simulator.model``); in Python
    they are the caller's argument of the same name.
    """
    key, address = _setting_names(provider, base_url)
    status = getattr(error, "status_code", None)

    def failed(code: str, hint: str) -> ValueError:
        return ValueError(f"{caller} failed [{code}]. {hint}")

    if isinstance(error, AddressRefused):
        return failed(
            "judge_provider_address_refused",
            "The endpoint's name resolves to an address this installation does not allow a judge to call. "
            f"Check {address}.",
        )
    if isinstance(error, NotAChatCompletion):
        return failed("judge_provider_address", f"The address did not answer as a chat endpoint. Check {address}.")
    if isinstance(
        error, (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema, requests.exceptions.InvalidURL)
    ):
        return failed("judge_provider_address", f"Check {address}: it must be an http or https address.")
    if isinstance(error, requests.exceptions.ReadTimeout):
        return failed(
            "judge_provider_unreachable",
            f"The endpoint did not answer in time. Check {address}, or raise MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS.",
        )
    if isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, ConnectionError)):
        return failed("judge_provider_unreachable", f"The endpoint could not be reached. Check {address}.")
    if status in (400, 403, 404, 422) and names_unknown_model(str(error)):
        return failed(
            "judge_provider_model",
            f"The endpoint does not serve this model, or {key} may not use it. Check {setting}.model.",
        )
    if status in (401, 403):
        return failed(
            "judge_provider_auth", f"The endpoint refused the credentials. Check {key} and {setting}.headers."
        )
    if status in (404, 405):
        return failed(
            "judge_provider_address",
            f"The address has no chat completions route. "
            f"Check {address}: the endpoint's base address, usually ending in /v1.",
        )
    if status in (400, 422) and is_response_format_error(str(error)):
        # The judge asks again without structured output only after an HTTP 400 that names it.
        return failed(
            "judge_provider_structured_output",
            "The endpoint refused structured output (response_format), and the judge asks again without it "
            f"only after an HTTP 400. Use a {setting}.model on an endpoint that supports it.",
        )
    if status in (400, 422):
        return failed(
            "judge_provider_rejected", f"The endpoint rejected the request. Check {setting}.inference_params."
        )
    if status in _RATE_LIMIT_STATUS:
        return failed(
            "judge_provider_throttled", f"Check the provider quota, or raise {setting}.rate_limit_wait_seconds."
        )
    return failed(
        "judge_provider_error",
        f"The endpoint failed without a judgment; {setting}.max_retries sets how often it is retried.",
    )


def _price(model_uri: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """What a call cost, from the model catalogue MLflow reads, or None.

    MLflow's catalogue is a download from github.com, which it makes at the first priced call of a
    process, tries four times with a five-second timeout each, and makes again for every provider:
    a judged call that reaches only its endpoint cannot make it, and an installation with no route
    out would wait on it. So a call is priced only from a catalogue the operator named with
    ``MLFLOW_MODEL_CATALOG_URI`` (a mirror, or a ``file://`` folder), and is left unpriced without
    one. An entry needs explicit input and output rates. The tokens, model version and judgment
    are recorded independently of the price. The same setting prices a traced target's calls.
    """
    if not MLFLOW_MODEL_CATALOG_URI.get_raw():  # type: ignore[no-untyped-call]
        return None
    return lookup_model_cost(model_uri, prompt_tokens, completion_tokens)


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
    headers: Mapping[str, str] | None = None,
    endpoint: str | None = None,
    cache: ModelCallCache | None = None,
    judge_version: str = "",
    address_policy: AddressPolicy | None = None,
) -> Feedback:
    """One judge stage's request and its validated judgment (:func:`invoke_structured`).
    ``endpoint`` is the base address of the OpenAI-compatible endpoint that serves an ``openai:/``
    model, used instead of ``OPENAI_API_BASE``; the key is still ``OPENAI_API_KEY``. With an
    ``address_policy`` the call connects only to an address the policy allows, pinned
    (:mod:`martian.evals._pinned_address`), or is refused.

    With ``cache`` (:class:`martian.evals.cache.ModelCallCache`), a validated judgment is stored
    under the full request (:func:`_cache_request`, with ``judge_version``, the version of the
    judge that built the prompt) and replayed for the same request in a later run. The model, the
    endpoint and the key are checked first, as without the cache. A failure is raised as without
    the cache and never stored. A replayed judgment's metadata says so (``martian.cache: hit``),
    with no provider retries, token counts or cost, since no call was made."""
    caller = f"Judge {stage}"

    def fresh() -> Feedback:
        judgment, metadata = invoke_structured(
            model_uri=model_uri,
            prompt=prompt,
            response_format=response_format,
            inference_params=inference_params,
            caller=caller,
            setting="judge",
            max_retries=max_retries,
            rate_limit_wait_seconds=rate_limit_wait_seconds,
            headers=headers,
            endpoint=endpoint,
            address_policy=address_policy,
        )
        return Feedback(
            name=assessment_name,
            value=judgment.model_dump()["result"],
            source=AssessmentSource(source_type=AssessmentSourceType.LLM_JUDGE, source_id=model_uri),
            metadata=metadata,
        )

    if cache is None:
        return fresh()
    provider, _, _ = _route(
        model_uri, prompt, caller=caller, setting="judge", endpoint=endpoint, address_policy=address_policy
    )
    request = _cache_request(
        model_uri=model_uri,
        provider=provider,
        endpoint=endpoint,
        prompt=prompt,
        response_format=response_format,
        inference_params=inference_params,
        headers=dict(headers or {}),
        stage=stage,
        judge_version=judge_version,
    )

    def replay(stored: Any) -> Feedback:
        metadata = {**stored["metadata"], PROVIDER_RETRIES_KEY: 0, HIT_KEY: "hit"}
        return Feedback(
            name=assessment_name,
            value=stored["value"],
            source=AssessmentSource(source_type=AssessmentSourceType.LLM_JUDGE, source_id=model_uri),
            metadata=metadata,
        )

    return cache.call("judge", request, fresh, store=_stored_judgment, replay=replay)


# Feedback metadata a replayed judgment leaves out: they describe the call that produced it.
_PER_CALL_METADATA = frozenset(
    {
        PROVIDER_RETRIES_KEY,
        AssessmentMetadataKey.JUDGE_INPUT_TOKENS,
        AssessmentMetadataKey.JUDGE_OUTPUT_TOKENS,
        AssessmentMetadataKey.JUDGE_COST,
    }
)


def _stored_judgment(feedback: Feedback) -> dict[str, Any]:
    """What the cache keeps of a judgment: its value and the metadata that describes the reply."""
    metadata = {key: value for key, value in (feedback.metadata or {}).items() if key not in _PER_CALL_METADATA}
    return {"value": feedback.value, "metadata": metadata}


def _judge_address(provider: str, endpoint: str | None) -> str | None:
    """The address that serves the judge, as a cache key holds it: ``endpoint``, else for the
    openai provider the base address in the environment as MLflow reads it, else None (the
    provider's own address, which its name and the model URI fix)."""
    if endpoint is not None:
        return key_address(endpoint)
    if provider == "openai":
        base = os.environ.get("OPENAI_API_BASE") or os.environ.get("OPENAI_BASE_URL")
        return key_address(base) if base else None
    return None


def _cache_request(
    *,
    model_uri: str,
    provider: str,
    endpoint: str | None,
    prompt: list[ChatMessage],
    response_format: type[BaseModel],
    inference_params: dict[str, Any] | None,
    headers: Mapping[str, str],
    stage: str,
    judge_version: str,
) -> dict[str, Any]:
    """Everything that decides a judge stage's reply, as its cache key: the model, the address
    serving it, the messages (the judge's prompt with its criteria, the request and the reply
    under test), the reply schema, the inference parameters, the headers that are not
    credentials, the stage and the judge's version. No key and no credential header."""
    return {
        "model": model_uri,
        "address": _judge_address(provider, endpoint),
        "messages": [{"role": message.role, "content": message.content} for message in prompt],
        "schema": response_format.model_json_schema(),
        "inference_params": dict(inference_params or {}),
        "headers": key_headers(headers),
        "stage": stage,
        "judge": judge_version,
    }


def _route(
    model_uri: str,
    prompt: list[ChatMessage],
    *,
    caller: str,
    setting: str,
    endpoint: str | None,
    address_policy: AddressPolicy | None = None,
) -> tuple[str, str, Any]:
    """The provider, the model and MLflow's adapter for ``model_uri``, once the model URI, the
    endpoint and, for the openai provider, the key are checked. A problem is the caller's own
    ``ValueError`` naming the ``<setting>`` to check. With an ``address_policy`` only a provider
    whose requests this module sends itself, over a pinned connection, is accepted: the others call
    through LiteLLM, which opens its own."""
    # Reuse routing, credentials and wire transforms from the pinned MLflow.
    # Calling the judge adapter itself would dispatch tools and drop envelope fields.
    try:
        provider, model = parse_model_uri(model_uri)
        adapter = get_adapter(model_uri, prompt)
    except Exception:
        raise ValueError(
            f"{caller} failed [judge_provider_config]. Check {setting}.model: a model URI such as openai:/<model>."
        ) from None
    if not isinstance(adapter, (GatewayAdapter, LiteLLMAdapter)):
        raise ValueError(
            f"{caller} failed [judge_provider_config]. "
            f"Check {setting}.model: its provider must return a direct chat completion."
        )
    if address_policy is not None and not isinstance(adapter, GatewayAdapter):
        raise ValueError(
            f"{caller} failed [judge_provider_config]. "
            f"This installation lets a judge call only an OpenAI-compatible endpoint: check {setting}.model."
        )
    if endpoint is not None and (provider != "openai" or not isinstance(adapter, GatewayAdapter)):
        raise ValueError(
            f"{caller} failed [judge_provider_config]. "
            "An endpoint serves openai:/ models: check the judge's model and endpoint."
        )
    # MLflow's openai provider refuses to start without a key, so say which one before it does.
    if provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        raise ValueError(f"{caller} failed [judge_provider_key]. Set OPENAI_API_KEY.")
    return provider, model, adapter


def invoke_structured[Reply: BaseModel](
    *,
    model_uri: str,
    prompt: list[ChatMessage],
    response_format: type[Reply],
    inference_params: dict[str, Any] | None,
    caller: str,
    setting: str,
    max_retries: int = 0,
    rate_limit_wait_seconds: float = 0.0,
    headers: Mapping[str, str] | None = None,
    endpoint: str | None = None,
    address_policy: AddressPolicy | None = None,
) -> tuple[Reply, dict[str, Any]]:
    """One structured-output request on the judges' provider path, and its validated reply with
    the call's metadata (retries, the reply's format, the model the provider says answered, tokens
    and cost). The judges and the simulated user (:mod:`martian.evals.simulation`) share it, so
    they share the routing, the key, the retry rules and the failure messages: ``caller`` names who
    asked (``Judge draft``, ``Simulated user``) and ``setting`` the suite file block whose settings a
    failure names (``judge``, ``simulator``). ``endpoint`` is the base address of the
    OpenAI-compatible endpoint that serves an ``openai:/`` model, used instead of
    ``OPENAI_API_BASE``; the key is still ``OPENAI_API_KEY``. With an ``address_policy`` the request
    connects only to an address the policy allows (:mod:`martian.evals._pinned_address`), and a
    provider that cannot be pinned is refused. Only the judges' calls go through the cache of
    model calls (:func:`invoke_response_judge`); a simulated user's never do."""
    provider, model, adapter = _route(
        model_uri, prompt, caller=caller, setting=setting, endpoint=endpoint, address_policy=address_policy
    )
    extra_headers = dict(headers or {})

    retries = 0  # every retry, recorded in the feedback metadata
    failures = 0  # retries after a server error or connection failure, limited by max_retries
    started = _clock()
    while True:
        try:
            if isinstance(adapter, GatewayAdapter):
                completion, structured = _gateway_completion(
                    provider, model, prompt, response_format, inference_params, extra_headers, endpoint, address_policy
                )
            else:
                completion, structured = _litellm_completion(
                    f"{provider}/{model}", prompt, response_format, inference_params, extra_headers
                )
            break
        except Exception as error:
            # Only a failure that returned no judgment is retried. A reply that was received is
            # final, valid or not: a second attempt at it would be a retry towards a pass.
            if not _no_judgment_received(error):
                raise _provider_failure(caller, error, provider, endpoint, setting) from None
            asked = _asked_wait(error)
            backoff = min(_MAX_BACKOFF_SECONDS, 2.0**retries + random.uniform(0.0, 0.5))
            if getattr(error, "status_code", None) in _RATE_LIMIT_STATUS:
                # Wait as long as the provider asks; stop when that would outlast the budget.
                remaining = rate_limit_wait_seconds - (_clock() - started)
                wait = asked if asked is not None else min(backoff, remaining)
                if remaining <= 0 or wait > remaining:
                    raise _provider_failure(caller, error, provider, endpoint, setting) from None
            else:
                if failures >= max_retries:
                    raise _provider_failure(caller, error, provider, endpoint, setting) from None
                failures += 1
                wait = min(asked, _MAX_BACKOFF_SECONDS) if asked is not None else backoff
            _sleep(wait)
            retries += 1

    metadata: dict[str, Any] = {
        PROVIDER_RETRIES_KEY: retries,
        RESPONSE_FORMAT_KEY: "json_schema" if structured else "none",
    }
    reported = getattr(completion, "model", None)
    if isinstance(reported, str) and reported.strip():
        metadata[MODEL_VERSION_KEY] = reported.strip()[:200]
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
            and (cost := _price(model_uri, prompt_tokens, completion_tokens)) is not None
        ):
            metadata[AssessmentMetadataKey.JUDGE_COST] = cost

    if not completion.choices:
        raise StructuredReplyError(f"{caller} returned no choices [judge_invalid_response]", metadata)
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
        raise StructuredReplyError(
            f"{caller} returned a tool call [judge_unexpected_tool_call]. "
            f"Use a {setting}.model whose endpoint returns structured output as message text.",
            metadata,
        )

    # A reply cut off at max_tokens is the likeliest reason for missing or incomplete JSON.
    cut_off = (
        f" It was cut off: raise {setting}.inference_params.max_tokens." if choice.finish_reason == "length" else ""
    )
    content = message.content
    if isinstance(content, list):
        content = "\n".join(part.text for part in content if getattr(part, "text", None))
    if not isinstance(content, str) or not content.strip():
        raise StructuredReplyError(f"{caller} returned no text [judge_invalid_response].{cut_off}", metadata)
    content = content.strip()
    # Allow a single enclosing fence from JSON-in-text providers, with no surrounding prose.
    fence, _, body = content.partition("\n")
    if fence.strip().lower() in {"```", "```json"} and body.endswith("\n```"):
        content = body.removesuffix("\n```")
    try:
        reply = response_format.model_validate_json(content)
    except ValidationError:
        hint = cut_off or (
            f" The endpoint may ignore structured output (response_format): check {setting}.model."
            if structured
            else " The endpoint declined structured output, and the reply without it was not the JSON asked for: "
            f"use a {setting}.model that supports response_format or follows the JSON instructions."
        )
        raise StructuredReplyError(
            f"{caller} reply failed schema validation [judge_invalid_schema].{hint}", metadata
        ) from None
    return reply, metadata
