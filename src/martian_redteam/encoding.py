"""Local encoding transforms. No LLM calls."""

from __future__ import annotations

import base64
import codecs

_LEET = str.maketrans(
    {
        "a": "4",
        "A": "4",
        "e": "3",
        "E": "3",
        "i": "1",
        "I": "1",
        "o": "0",
        "O": "0",
        "s": "5",
        "S": "5",
        "t": "7",
        "T": "7",
    }
)


def apply_encoding(attack: str, method: str) -> str:
    if method == "rot13":
        return codecs.encode(attack, "rot_13")
    if method == "base64":
        return base64.b64encode(attack.encode()).decode()
    if method == "leetspeak":
        return attack.translate(_LEET)
    raise ValueError(f"unknown encoding method: {method}")
