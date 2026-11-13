"""Response-only invocation for the pinned MLflow provider adapters."""

from __future__ import annotations

from typing import Any, Literal

from mlflow.entities import Feedback
from mlflow.entities.assessment_source import AssessmentSource, AssessmentSourceType
from mlflow.gateway.constants import MLFLOW_GATEWAY_CALLER_HEADER, GatewayCaller
from mlflow.genai.judges.adapters.gateway_adapter import GatewayAdapter, _build_request
from mlflow.genai.judges.adapters.litellm_adapter import LiteLLMAdapter, _invoke_litellm
from mlflow.genai.judges.adapters.utils import (
    ChatCompletionError,
    get_adapter,
    is_response_format_error,
    send_chat_request,
)
from mlflow.genai.utils.llm_utils import _lookup_model_cost
from mlflow.metrics.genai.model_utils import _get_provider_instance, _parse_model_uri
from mlflow.tracing.constant import AssessmentMetadataKey
from mlflow.types.llm import ChatMessage
from mlflow.utils.workspace_context import get_request_workspace
from mlflow.utils.workspace_utils import WORKSPACE_HEADER_NAME
from pydantic import BaseModel, ValidationError


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
        return send_chat_request(
            endpoint=endpoint,
            headers=headers,
            payload=provider.adapter_class.chat_to_model(payload, provider.config),
            num_retries=0,
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
) -> Feedback:
    # Reuse routing, credentials and wire transforms from MLflow 3.16.1.
    # Calling the judge adapter itself would dispatch tools and drop envelope fields.
    try:
        provider, model = _parse_model_uri(model_uri)
        adapter = get_adapter(model_uri, prompt)
    except Exception:
        raise ValueError(f"Judge {stage} failed [judge_provider_config]. Check the provider/model URI.") from None

    try:
        if isinstance(adapter, GatewayAdapter):
            completion = _gateway_completion(provider, model, prompt, response_format, inference_params)
        elif isinstance(adapter, LiteLLMAdapter):
            completion = _litellm_completion(f"{provider}/{model}", prompt, response_format, inference_params)
        else:
            raise ValueError("A direct chat completion provider is required")
    except Exception as error:
        raise _provider_failure(stage, error) from None

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

    metadata: dict[str, Any] = {}
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
        metadata=metadata or None,
    )
