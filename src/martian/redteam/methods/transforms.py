"""Static transforms: local changes of an attack's text. No model call, no network.

Each transform is a pure function of the text. Used as an attack (``attacks: [{name: Base64}]``) it
rewrites a prompt injection the attacker wrote, as the version 0.2 encodings do; used as a layer
(``layers: [Base64]``) it changes whatever another method sends.
"""

from __future__ import annotations

from martian.redteam.methods.base import Transform
from martian_redteam.encoding import apply_encoding


def _encoding(method: str) -> Transform:
    labels = {"rot13": ("ROT13", "ROT13"), "base64": ("Base64", "Base64"), "leetspeak": ("Leetspeak", "Leetspeak")}
    name, label = labels[method]
    tries = {
        "rot13": "A filter that matches plain text: the attack is ROT13 text the model can still read.",
        "base64": "A filter that matches plain text: the attack is Base64 the model can still decode.",
        "leetspeak": "A filter that matches words: letters become digits (a 4, e 3, i 1, o 0, s 5, t 7).",
    }[method]
    return Transform(method, name, label, lambda text: apply_encoding(text, method), tries)


TRANSFORMS: dict[str, Transform] = {item.id: item for item in (_encoding(m) for m in ("rot13", "base64", "leetspeak"))}
"""Every transform, by id."""


__all__ = ["TRANSFORMS"]
