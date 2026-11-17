"""The reply contract: what a target answered, read into a :class:`~martian.redteam.targets.TargetReply`.

A reply has text and, optionally, the tool calls the target made or asked for. A target reports its
tool calls in one of these places:

- an HTTP or MCP target: the list at the target's ``tool_calls_path`` in its JSON answer;
- a function target or a callback: an object ``{"text": ..., "tool_calls": [...]}`` as its return
  value, or the version 0.2 :class:`martian_redteam.types.Turn` with ``tool_calls`` or the older
  ``tools_called`` names.

Each tool call is an object, or a tool's name as text (the version 0.2 form, which says nothing about
the arguments or whether the call ran). An object's fields:

- ``name``, the tool's name. OpenAI's form, ``{"function": {"name", "arguments"}}``, is read too.
- ``arguments`` (or ``args``, or Anthropic's ``input``): an object, or JSON text of one.
- ``id`` or ``call_id``: the call's id, when the target has one.
- ``executed``: ``true`` when the call ran, ``false`` when it was refused or blocked; left out when
  the target only asked for it. A check that needs an executed call never counts a call without it.
- ``result``: what the tool returned, as text or any JSON.

A tool call that cannot be read is the target's error, never an empty list: an item without a name,
or arguments that are not an object.

A reply's text is a string as it is; any other JSON value is kept as its JSON text, so a
``response_path`` that names an object still gives the attacker and the checks everything it holds.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from martian.evals._digits import ascii_digits
from martian.evals.runner import TargetCallError
from martian.redteam.model import ToolCall
from martian.redteam.targets import TargetReply

REPLY_KEYS = frozenset({"text", "tool_calls"})
"""The keys of a function's or a callback's structured reply."""


def reply_text(value: Any) -> str:
    """The text of a reply value: a string as it is, anything else as JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def read_path(value: Any, steps: Sequence[str]) -> Any:
    """The value at ``steps`` (a ``response_path`` split at dots) in ``value``, or ``None`` when it has
    none. A step of the digits 0-9 indexes a list."""
    found = value
    for step in steps:
        if isinstance(found, dict) and step in found:
            found = found[step]
        elif isinstance(found, list) and ascii_digits(step) and int(step) < len(found):
            found = found[int(step)]
        else:
            return None
    return found


def _arguments(value: Any, where: str) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise TargetCallError(f"{where}: the arguments are not JSON") from None
    if not isinstance(value, dict):
        raise TargetCallError(f"{where}: the arguments are not an object")
    return value


def tool_call(item: Any, where: str = "tool_calls[0]") -> ToolCall:
    """One tool call of a reply, in any form the module lists. ``where`` names it in an error."""
    if isinstance(item, str):
        if not item.strip():
            raise TargetCallError(f"{where} has no name")
        return ToolCall(name=item)
    if not isinstance(item, Mapping):
        raise TargetCallError(f"{where} is not an object or a tool's name")
    found = dict(item)
    if isinstance(found.get("function"), Mapping):  # OpenAI's form
        found = {**found, **found["function"]}
    name = found.get("name")
    if not isinstance(name, str) or not name.strip():
        raise TargetCallError(f"{where} has no name")
    arguments = next((found[key] for key in ("arguments", "args", "input") if key in found), None)
    call_id = found.get("call_id", found.get("id"))
    executed = found.get("executed")
    if executed is not None and not isinstance(executed, bool):
        raise TargetCallError(f"{where}: executed is true, false or left out")
    result = found.get("result")
    return ToolCall(
        name=name,
        arguments=_arguments(arguments, where),
        call_id=None if call_id is None else str(call_id),
        executed=executed,
        result=None if result is None else reply_text(result),
    )


def tool_calls(value: Any, where: str = "tool_calls") -> list[ToolCall]:
    """The tool calls in a reply's list. ``None`` is no calls; anything but a list is an error."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise TargetCallError(f"{where} is not a list")
    return [tool_call(item, f"{where}[{index}]") for index, item in enumerate(value)]


def reply_from(value: Any, *, session: str | None = None, latency_ms: float | None = None) -> TargetReply:
    """What a function or a callback returned, as a reply: text; an object with ``text`` and
    ``tool_calls`` and nothing else; a version 0.2 ``Turn``; or a ``TargetReply``. Any other value is
    the text of its JSON, with no tool calls."""
    from martian_redteam.types import Turn

    update: dict[str, Any] = {"session": session, "latency_ms": latency_ms}
    if isinstance(value, TargetReply):
        return value.model_copy(update={key: found for key, found in update.items() if found is not None})
    if isinstance(value, Turn):
        calls = [ToolCall.model_validate(call.model_dump()) for call in value.tool_calls] or tool_calls(
            value.tools_called, "tools_called"
        )
        return TargetReply(text=value.content, tool_calls=calls, **update)
    if isinstance(value, Mapping) and isinstance(value.get("text"), str) and set(value) <= REPLY_KEYS:
        return TargetReply(text=value["text"], tool_calls=tool_calls(value.get("tool_calls")), **update)
    return TargetReply(text=reply_text(value), **update)


__all__ = ["REPLY_KEYS", "read_path", "reply_from", "reply_text", "tool_call", "tool_calls"]
