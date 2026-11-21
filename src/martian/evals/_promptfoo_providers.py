"""Convert declarative Promptfoo providers to native suite targets without calling them."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from martian.evals._promptfoo_inputs import PromptfooConfigError
from martian.evals.cache import is_credential
from martian.evals.model import HEADER_NAME, ResponseMode, Target

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_ENV_TEMPLATE = re.compile(rf"\{{\{{\s*env\.({_NAME})\s*\}}\}}")
_ENV_REF = re.compile(rf"\$\{{({_NAME})\}}")
_CREDENTIAL_REF = re.compile(rf"(?:(?:Bearer|Basic) )?{_ENV_REF.pattern}", re.IGNORECASE)
_TEMPLATE = re.compile(r"\{\{|\{%|\{#")
_ENV_IN_BODY = re.compile(r"\{\{\s*env\b")
_RUNTIME_VAR = re.compile(r"\{\{\s*(?:evaluationId|__testIdx|__promptIdx|__providerIdx)\s*\}\}")
_SELECTOR = re.compile(r"""\.([A-Za-z_$][A-Za-z0-9_$]*)|\[(0|[1-9][0-9]*|'[A-Za-z0-9_$-]+'|"[A-Za-z0-9_$-]+")\]""")
_SELECTOR_ROOT = re.compile(r"(?:(?:\(\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\))\s*=>\s*)?([A-Za-z_$][A-Za-z0-9_$]*)")
_CHAT_FIELDS = frozenset(
    {
        "seed",
        "max_tokens",
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "stop",
        "response_format",
    }
)


def _loads(text: str) -> Any:
    def invalid(value: str) -> Any:
        raise ValueError(f"{value} is not a JSON constant")

    return json.loads(text, parse_constant=invalid)


def _json_value(value: str) -> Any:
    """Decode nested JSON containers as Promptfoo's HTTP JSON body renderer does."""
    if value.lstrip().startswith(("{", "[")):
        try:
            return _loads(value)
        except ValueError:
            pass
    return value


def _chat_messages(prompt: str) -> list[dict[str, Any]]:
    if prompt.lstrip().startswith("- role:"):
        raise PromptfooConfigError("use a JSON array for chat messages")
    try:
        value = _loads(prompt)
    except ValueError:
        if prompt.lstrip().startswith(("[", "{")):
            raise PromptfooConfigError("the chat prompt is not a valid JSON array") from None
        return [{"role": "user", "content": prompt}]
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(message, dict) and isinstance(message.get("role"), str) for message in value)
    ):
        raise PromptfooConfigError("the chat prompt must be plain text or a JSON array of messages")
    return value


@dataclass(frozen=True)
class NativeProvider:
    target: Target
    fields: tuple[str, ...] = ()
    chat: bool = False

    def inputs(
        self, prompt: str, variables: dict[str, Any], render: Callable[[str, dict[str, Any]], str | None]
    ) -> dict[str, Any]:
        if self.chat:
            return {"promptfoo_messages": _chat_messages(prompt)}
        found = {}
        for index, template in enumerate(self.fields):
            value = render(template, {**variables, "prompt": prompt})
            if value is None:
                raise PromptfooConfigError("the HTTP request uses an unsupported body template")
            found[f"promptfoo_field_{index}"] = _json_value(value)
        return found


def provider_display(identifier: str) -> str:
    """Show an HTTP provider's address without user info, query or fragment."""
    if not identifier.startswith(("http://", "https://")):
        return identifier
    try:
        parts = urlsplit(identifier)
    except ValueError:
        return "HTTP provider"
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def _env_template(value: str, setting: str) -> str:
    if _ENV_REF.search(value):
        raise PromptfooConfigError(f"use Promptfoo environment references for {setting}")
    result = _ENV_TEMPLATE.sub(lambda match: "${" + match[1] + "}", value)
    if _TEMPLATE.search(result) or "\r" in result or "\n" in result:
        raise PromptfooConfigError(f"{setting} supports literal text and environment references")
    return result


def _address(value: Any, *, chat: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise PromptfooConfigError("the provider needs an explicit HTTP URL")
    url = _env_template(value, "the provider URL") if not chat else value
    if _TEMPLATE.search(url) or any(ch.isspace() for ch in url) or (chat and _ENV_REF.search(url)):
        raise PromptfooConfigError("the provider URL must be a literal address")
    dummy = _ENV_REF.sub("example.invalid", url)
    if _ENV_REF.match(url):
        dummy = "http://" + dummy
    try:
        parts = urlsplit(dummy)
        port = parts.port
    except ValueError:
        raise PromptfooConfigError("the provider URL is invalid") from None
    if parts.scheme not in {"http", "https"} or not parts.hostname or port == 0 or parts.fragment:
        raise PromptfooConfigError("the provider URL needs an HTTP origin and no fragment")
    if parts.username is not None or parts.password is not None:
        raise PromptfooConfigError("put credentials in environment-backed headers")
    query = url.partition("?")[2]
    if any(is_credential(name) and not _ENV_REF.fullmatch(value) for name, value in parse_qsl(query)):
        raise PromptfooConfigError("use an environment reference for URL credentials")
    if chat and (query or _ENV_REF.match(url)):
        raise PromptfooConfigError("OpenAI chat conversion needs a literal API base URL without query parameters")
    return url


def _headers(value: Any, *, templated: bool) -> dict[str, str]:
    if not isinstance(value, dict):
        raise PromptfooConfigError("provider headers must be a mapping")
    headers = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not re.fullmatch(HEADER_NAME, name) or not isinstance(raw, str):
            raise PromptfooConfigError("provider headers need valid names and text values")
        name = name.lower()
        if name in headers:
            raise PromptfooConfigError("provider headers contain a duplicate name")
        if not templated and _TEMPLATE.search(raw):
            raise PromptfooConfigError("use apiKeyEnvar or a literal non-credential OpenAI header")
        text = _env_template(raw, "provider headers")
        if is_credential(name) and not _CREDENTIAL_REF.fullmatch(text):
            raise PromptfooConfigError("use environment references for credential headers")
        headers[name] = text
    return headers


def _response_path(value: Any) -> str | None:
    if not isinstance(value, str):
        raise PromptfooConfigError("the response selector must be a JSON property path or text")
    text = value.strip()
    if text == "text":
        return None
    root = _SELECTOR_ROOT.match(text)
    if not root or root[2] != (root[1] or "json"):
        raise PromptfooConfigError("the response selector must be a JSON property path or text")
    position = root.end()
    steps = []
    while step := _SELECTOR.match(text, position):
        position = step.end()
        steps.append(step[1] or step[2].strip("'\""))
    if not steps or position != len(text):
        raise PromptfooConfigError("the response selector must be a JSON property path or text")
    return ".".join(steps)


def _body_template(body: dict[str, Any] | list[Any]) -> tuple[dict[str, Any] | list[Any], tuple[str, ...]]:
    fields: list[str] = []

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, str):
            if _ENV_IN_BODY.search(value) or _RUNTIME_VAR.search(value):
                raise PromptfooConfigError(
                    "HTTP body templates need case variables or prompt; put credentials in headers"
                )
            if _TEMPLATE.search(value):
                slot = "{{inputs.promptfoo_field_" + str(len(fields)) + "}}"
                fields.append(value)
                return slot
            return _json_value(value)
        return value

    template = walk(body)
    if not fields:
        raise PromptfooConfigError("the HTTP request body needs a prompt or case variable")
    return template, tuple(fields)


def _only(config: dict[str, Any], allowed: set[str] | frozenset[str]) -> None:
    if fields := sorted(set(config) - allowed):
        raise PromptfooConfigError(f"unsupported provider settings: {', '.join(fields)}")


def _http(identifier: str, config: dict[str, Any]) -> NativeProvider:
    _only(config, {"url", "method", "headers", "body", "transformResponse", "responseParser", "maxRetries"})
    if config.get("method") != "POST":
        raise PromptfooConfigError("HTTP conversion requires method: POST")
    if config.get("maxRetries", 0) != 0:
        raise PromptfooConfigError("the SDK sends each HTTP request once; provider retries need review")
    address = _address(config.get("url", identifier))
    headers = _headers(config.get("headers", {}), templated=True)
    raw = config.get("body")
    content_type = headers.get("content-type")
    if isinstance(raw, str) and content_type == "application/json":
        try:
            raw = _loads(raw)
        except ValueError:
            raise PromptfooConfigError("the provider's body must be a JSON object or array template") from None
    if not isinstance(raw, dict | list) or (content_type is not None and content_type != "application/json"):
        raise PromptfooConfigError("the provider's body must be a JSON object or array template")
    body, fields = _body_template(raw)
    parser = config.get("transformResponse") or config.get("responseParser")
    path = _response_path(parser) if parser is not None else None
    mode: ResponseMode | None = None if isinstance(parser, str) and parser.strip() == "text" else "json_or_text"
    return NativeProvider(
        Target(kind="http", url=address, headers=headers, body=body, response_path=path, response_mode=mode),
        fields=fields,
    )


def _openai_chat(model: str, config: dict[str, Any]) -> NativeProvider:
    _only(
        config,
        _CHAT_FIELDS
        | {
            "apiBaseUrl",
            "apiKeyEnvar",
            "apiKeyRequired",
            "useDefaultApiKey",
            "headers",
            "omitDefaults",
            "maxRetries",
        },
    )
    if not model or re.search(r"(?:^|/)(?:gpt-[56]|o[134](?:[-:]|$))|audio", model, re.IGNORECASE):
        raise PromptfooConfigError("this chat model uses provider-specific request or output options")
    if config.get("maxRetries", 0) != 0:
        raise PromptfooConfigError("the SDK sends each HTTP request once; provider retries need review")
    base = _address(config.get("apiBaseUrl"), chat=True)
    headers = _headers(config.get("headers", {}), templated=False)
    if headers.get("content-type", "application/json") != "application/json":
        raise PromptfooConfigError("OpenAI chat needs a JSON request")
    key = config.get("apiKeyEnvar")
    if key is not None and (not isinstance(key, str) or not re.fullmatch(_NAME, key)):
        raise PromptfooConfigError("apiKeyEnvar must name an environment variable")
    required = config.get("apiKeyRequired", True)
    default_key = config.get("useDefaultApiKey", True)
    omit = config.get("omitDefaults", False)
    if any(not isinstance(flag, bool) for flag in (required, default_key, omit)):
        raise PromptfooConfigError("apiKeyRequired, useDefaultApiKey and omitDefaults need boolean values")
    if key is None and default_key:
        if not required:
            raise PromptfooConfigError("set useDefaultApiKey: false for an unauthenticated endpoint")
        if urlsplit(base).netloc.lower() != "api.openai.com" or not base.startswith("https://"):
            raise PromptfooConfigError("set apiKeyEnvar for the configured OpenAI-compatible endpoint")
        key = "OPENAI_API_KEY"
    if key is None and required:
        raise PromptfooConfigError("set apiKeyEnvar or use an explicitly unauthenticated endpoint")
    if key is not None:
        headers.setdefault("authorization", "Bearer ${" + key + "}")
    options = {name: config[name] for name in sorted(_CHAT_FIELDS & set(config))}
    if _TEMPLATE.search(json.dumps(options, allow_nan=False)):
        raise PromptfooConfigError("OpenAI generation options must be literal JSON values")
    if not omit:
        options.setdefault("temperature", 0)
        options.setdefault("max_tokens", 1024)
    body = {"model": model, "messages": "{{inputs.promptfoo_messages}}", **options}
    return NativeProvider(
        Target(
            kind="http",
            url=base.rstrip("/") + "/chat/completions",
            headers=headers,
            body=body,
            response_path="choices.0.message.content",
        ),
        chat=True,
    )


def native_provider(declaration: Any) -> NativeProvider | None:
    options = declaration if isinstance(declaration, dict) else {"id": declaration}
    identifier = options.get("id")
    if not isinstance(identifier, str):
        return None
    http = identifier in {"http", "https"} or identifier.startswith(("http://", "https://"))
    chat = identifier.startswith(("openai:chat:", "openai:gpt-4"))
    if not (http or chat):
        return None
    _only(options, {"id", "label", "config"})
    config = options.get("config", {})
    if not isinstance(config, dict):
        raise PromptfooConfigError("the provider config must be a mapping")
    if http:
        return _http(identifier, config)
    if not config:
        return None
    return _openai_chat(identifier.removeprefix("openai:").removeprefix("chat:"), config)
