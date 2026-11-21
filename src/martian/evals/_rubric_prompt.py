"""Text-only message templates for a scored rubric's judge."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import suppress
from typing import Annotated, Any, Literal, NoReturn, TypedDict

from mlflow.types.llm import ChatMessage
from pydantic import AfterValidator, ConfigDict, TypeAdapter, with_config

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_TAG = re.compile(r"\{[\{%#]")


@with_config(ConfigDict(extra="forbid", strict=True))
class _Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


def _validate(prompt: str | list[_Message]) -> str | list[_Message]:
    if not prompt or (isinstance(prompt, str) and not prompt.strip()):
        raise ValueError("a judge prompt must contain text or at least one chat message")
    messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
    for message in messages:
        text = message["content"]
        if _TAG.search(_VARIABLE.sub("", text)):
            raise ValueError("a judge prompt supports only simple {{ name }} placeholders")
        if message["role"] != "user" and "output" in _VARIABLE.findall(text):
            raise ValueError("a judge prompt puts {{ output }} in a user message")
    return prompt


JudgePrompt = Annotated[str | list[_Message], AfterValidator(_validate)]
JUDGE_PROMPT = TypeAdapter(JudgePrompt)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Promptfoo stringifies object elements before Nunjucks joins the array.
        return ",".join(_json(item) if isinstance(item, (dict, list)) else _text(item) for item in value)
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return _json(value)


def _invalid_json_constant(_value: str) -> NoReturn:
    raise ValueError("non-finite numbers are not JSON")


def render_judge_prompt(
    prompt: JudgePrompt, variables: Mapping[str, Any], *, outputs: Any, rubric: str
) -> list[ChatMessage]:
    output = outputs
    if isinstance(outputs, str):
        with suppress(ValueError, RecursionError):
            output = json.loads(outputs, parse_constant=_invalid_json_constant)
    context = {**variables, "output": output, "rubric": rubric}

    def fill(content: str) -> str:
        return _VARIABLE.sub(lambda match: _text(context.get(match[1])), content)

    if isinstance(prompt, str):
        return [ChatMessage(role="user", content=fill(prompt))]
    return [ChatMessage(role=message["role"], content=fill(message["content"])) for message in prompt]
