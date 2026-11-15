"""Reading an endpoint's error text to choose the SDK's own message, never to repeat it."""

from __future__ import annotations

# How OpenAI-compatible servers say a model is unknown or not allowed for the key: OpenAI and
# vLLM answer 404 "does not exist", Ollama 404 "not found", LiteLLM 400 "Invalid model name"
# or, for a key limited to some models, 403 "not available for this API key", and Bedrock's
# OpenAI-compatible endpoint 400 "The provided model identifier is invalid".
_UNKNOWN_MODEL = (
    "does not exist",
    "not found",
    "invalid model",
    "model name is invalid",
    "model identifier is invalid",
    "not available",
    "unknown model",
    "no such model",
    "model_not_found",
)


def names_unknown_model(text: str) -> bool:
    """True when an endpoint's error text says the requested model is unknown or not allowed."""
    lower = text.lower()
    return "model" in lower and any(phrase in lower for phrase in _UNKNOWN_MODEL)
