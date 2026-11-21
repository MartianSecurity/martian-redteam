"""Validate XML output and find XML elements in an otherwise textual response."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from xml.parsers import expat

from martian.evals._digits import ascii_digits

type Elements = dict[str, list[Elements]]
type Path = tuple[str, ...]

_NAMESPACE = "\x1f"
_NAME = re.compile(r"""(/?)([^\s<>=/'"!?]+)""")
_MARKS = re.compile(r"""[<>"'\[]""")
_SPECIAL = (("<!--", "-->"), ("<![CDATA[", "]]>"), ("<?", "?>"))
_DTD_REASON = "DTD internal subsets are unsupported"


class _InternalSubset(ValueError):
    """An XML declaration contains its own DTD."""


def paths(required: Sequence[str]) -> tuple[Path, ...]:
    """Read dot-separated element paths with optional indices for repeated siblings."""
    if isinstance(required, str):
        raise ValueError("required_elements must be a list of paths")
    result = []
    for path in required:
        if not isinstance(path, str) or not path or any(char.isspace() for char in path):
            raise ValueError("required_elements must contain nonempty dot-separated paths without whitespace")
        parts = tuple(path.split("."))
        if any(not part or part.startswith(("@", "#")) for part in parts):
            raise ValueError("required_elements paths must name XML elements and optional sibling indices")
        result.append(parts)
    return tuple(result)


def _parse(text: str, *, fragment: bool) -> Elements:
    """Parse once, retaining only the element names and the order of repeated siblings."""
    parser = expat.ParserCreate(encoding="UTF-8", namespace_separator=None if fragment else _NAMESPACE)
    parser.namespace_prefixes = True
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    root: Elements = {}
    parents = [root]

    def start(name: str, _attributes: dict[str, str]) -> None:
        parts = name.split(_NAMESPACE)
        element = f"{parts[2]}:{parts[1]}" if len(parts) == 3 else parts[-1]
        children: Elements = {}
        parents[-1].setdefault(element, []).append(children)
        parents.append(children)

    def end(_name: str) -> None:
        parents.pop()

    def doctype(_name: str, _system: str | None, _public: str | None, internal: int) -> None:
        if internal:
            raise _InternalSubset(_DTD_REASON)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = doctype
    parser.Parse(text, True)
    return root


def _has_path(elements: Elements, path: Path) -> bool:
    position = 0
    while position < len(path):
        siblings = elements.get(path[position])
        if siblings is None:
            return False
        position += 1
        if position == len(path):
            return True
        if len(siblings) == 1:
            elements = siblings[0]
            continue
        index = path[position]
        if not ascii_digits(index) or (len(index) > 1 and index.startswith("0")):
            return False
        if len(index) > len(str(len(siblings) - 1)) or int(index) >= len(siblings):
            return False
        elements = siblings[int(index)]
        position += 1
    return True


@dataclass(frozen=True)
class _Candidate:
    start: int
    end: int
    parent: int | None


def _tag_end(text: str, start: int, *, doctype: bool) -> tuple[int, bool]:
    """End a tag at its first unquoted delimiter, or resume scanning at the next opening bracket."""
    quote = ""
    for mark in _MARKS.finditer(text, start):
        char = mark[0]
        if char == "<":
            return mark.start(), False
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == ">":
            return mark.end(), True
        elif doctype and char == "[":
            raise _InternalSubset(_DTD_REASON)
    return len(text), False


def _candidates(text: str) -> list[_Candidate]:
    """Locate balanced outer fragments. Markup inside comments, CDATA and instructions is text."""
    stack: list[tuple[str, int]] = []
    closed: list[_Candidate] = []
    absent: set[str] = set()
    position = 0
    while (start := text.find("<", position)) >= 0:
        position = start + 1
        special = next(((prefix, suffix) for prefix, suffix in _SPECIAL if text.startswith(prefix, start)), None)
        if special is not None:
            prefix, suffix = special
            if suffix not in absent:
                end = text.find(suffix, start + len(prefix))
                if end >= 0:
                    position = end + len(suffix)
                else:
                    absent.add(suffix)
            continue
        if text.startswith("<!DOCTYPE", start):
            position, _ = _tag_end(text, position, doctype=True)
            continue
        tag = _NAME.match(text, position)
        if tag is None:
            continue
        position, complete = _tag_end(text, tag.end(), doctype=False)
        if not complete:
            continue
        closing, name = tag.groups()
        if closing:
            if not stack or stack[-1][0] != name:
                stack.clear()
                continue
            _, start = stack.pop()
        elif not text.endswith("/>", start, position):
            stack.append((name, start))
            continue
        closed.append(_Candidate(start, position, stack[-1][1] if stack else None))
    starts = {candidate.start for candidate in closed}
    return [candidate for candidate in closed if candidate.parent not in starts]


def _has_fragment(text: str, required: tuple[Path, ...]) -> bool:
    peer_groups: dict[int | None, Elements] = {}
    for candidate in _candidates(text):
        try:
            elements = _parse(text[candidate.start : candidate.end], fragment=True)
        except expat.ExpatError:
            continue
        if not required:
            return True
        peers = peer_groups.setdefault(candidate.parent, {})
        for name, siblings in elements.items():
            peers.setdefault(name, []).extend(siblings)
    return any(all(_has_path(peers, path) for path in required) for peers in peer_groups.values())


def validate(output: object, required: tuple[Path, ...], *, fragment: bool) -> tuple[bool, str]:
    if not isinstance(output, str):
        return False, "XML output must be text"
    try:
        if fragment:
            if _has_fragment(output, required):
                return True, "XML fragment matches the required elements"
            return False, "no XML fragment matched the required elements"
        elements = _parse(output, fragment=False)
    except _InternalSubset:
        return False, _DTD_REASON
    except (expat.ExpatError, UnicodeError):
        return False, "the output is not a well-formed XML document"
    missing = [".".join(path) for path in required if not _has_path(elements, path)]
    if missing:
        return False, f"XML is missing required elements: {', '.join(missing)}"
    return True, "XML document matches the required elements"
