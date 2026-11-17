"""What a target answered, read into a :class:`~martian.redteam.targets.TargetReply`.

A reply's text is a string as it is; any other JSON value is kept as its JSON text, so a
``response_path`` that names an object still gives the attacker and the checks everything it holds.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from martian.evals._digits import ascii_digits
from martian.evals.runner import TargetCallError


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


__all__ = ["TargetCallError", "read_path", "reply_text"]
