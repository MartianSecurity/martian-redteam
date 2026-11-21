"""Find JSON objects in a reply, with the optional Promptfoo flow-YAML fallback."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Hashable, Iterator, Mapping
from string import ascii_letters
from typing import Any, Literal, NoReturn

import yaml
from yaml.nodes import MappingNode, Node, SequenceNode

from martian.evals import _json_schema
from martian.evals._promptfoo_yaml import CoreYamlLoader

EmbeddedJson = Literal["strict", "promptfoo"]

_FRAGMENT_LIMIT = 100_000  # UTF-16 code units, as in Promptfoo.
_SCAN_LIMIT = 8_000_000
_YAML_DEPTH = 128
_YAML_NODES = 50_000
_BRACE = re.compile(r"[{}]")
_OPEN = re.compile(r"\{")
_SLASHES = re.compile(r"/+")
_JS_SPACE = re.compile(r"[ \t\r\n\v\f\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]")


def _reject_literal(_token: str) -> NoReturn:
    raise ValueError("a nonfinite constant is not JSON syntax")


_JSON = json.JSONDecoder(parse_constant=_reject_literal)
_JS_JSON = json.JSONDecoder(parse_int=float, parse_constant=_reject_literal)


class _ReplyLoader(CoreYamlLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        if not isinstance(node, MappingNode):
            return super().construct_mapping(node, deep=deep)
        self.flatten_mapping(node)
        fields: dict[Hashable, Any] = {}
        for source_key, source_value in node.value:
            key = self.construct_object(source_key, deep=deep)
            if not isinstance(key, str):
                if key is None or isinstance(key, bool):
                    key = json.dumps(key)
                elif isinstance(key, int) and abs(key) < 2**53:
                    key = str(key)
                else:
                    raise ValueError("an embedded YAML object has an unsupported key")
            fields[key] = self.construct_object(source_value, deep=deep)
        return fields


def _slash_comments(line: str) -> str:
    if "//" not in line:
        return line
    quote = ""
    escaped = False
    url = False
    for index, char in enumerate(line):
        previous = line[index - 1] if index else " "
        following = line[index + 1 : index + 2] or " "
        if char == ":" and line.startswith("://", index):
            url = True
        if quote:
            if char == quote and not escaped and (quote == '"' or following not in ascii_letters):
                quote = ""
            escaped = char == "\\" and not escaped
        elif char == '"' or (char == "'" and previous not in ascii_letters):
            quote = char
        elif char == "/" and following == "/" and not url:
            slashes = _SLASHES.match(line, index)
            assert slashes is not None
            count = (slashes.end() - index) // 2
            return line[:index] + "#" * count + line[slashes.end() :]
        if _JS_SPACE.fullmatch(char):
            url = False
    return line


def _bound_yaml(root: Node) -> None:
    # Walk the expanded tree before construction so recursive aliases and merges cannot amplify it.
    stack = [(root, 0)]
    remaining = _YAML_NODES
    while stack:
        node, depth = stack.pop()
        remaining -= 1
        if remaining < 0 or depth > _YAML_DEPTH:
            raise ValueError("embedded YAML exceeds the nesting or expansion limit")
        if isinstance(node, MappingNode):
            for key, value in node.value:
                stack.extend(((key, depth + 1), (value, depth + 1)))
        elif isinstance(node, SequenceNode):
            stack.extend((child, depth + 1) for child in node.value)


def _repair(fragment: str) -> dict[str, Any] | None:
    missing = fragment.count("{") - fragment.count("}")
    fixed = fragment + "}" * missing
    try:
        loader = _ReplyLoader("\n".join(_slash_comments(line) for line in fixed.split("\n")))
        try:
            root = loader.get_single_node()
            if not isinstance(root, MappingNode):
                return None
            _bound_yaml(root)
            parsed = loader.construct_document(root)
            return parsed if isinstance(parsed, dict) else None
        finally:
            loader.dispose()
    except yaml.YAMLError:
        return None
    except RecursionError:
        raise ValueError("embedded YAML exceeds the nesting or expansion limit") from None


def _flow_fragment(text: str, start: int) -> str:
    stop = min(len(text), start + _FRAGMENT_LIMIT)
    depth = 1
    for brace in _BRACE.finditer(text, start + 1, stop):
        depth += 1 if brace[0] == "{" else -1
        if depth == 0:
            stop = brace.end()
            break
    fragment = text[start:stop]
    if not fragment.isascii():
        raw = fragment.encode("utf-16-le", errors="surrogatepass")
        fragment = raw[: 2 * _FRAGMENT_LIMIT].decode("utf-16-le", errors="surrogatepass")
    return fragment


def _cost(remaining: int, amount: int) -> int:
    remaining -= amount
    if remaining < 0:
        raise ValueError("embedded JSON exceeds the fragment scan limit")
    return remaining


def _objects(text: str, mode: EmbeddedJson) -> Iterator[dict[str, Any]]:
    """Yield nonoverlapping objects in order. Each decode reads at most a two-fragment window."""
    decoder = _JS_JSON if mode == "promptfoo" else _JSON
    work = min(_SCAN_LIMIT, max(4 * _FRAGMENT_LIMIT, 12 * len(text)))
    start_of_window, skip_until = -1, 0
    window = ""
    for brace in _OPEN.finditer(text):
        start = brace.start()
        if start < skip_until:
            continue
        block = start // _FRAGMENT_LIMIT * _FRAGMENT_LIMIT
        if block != start_of_window:
            start_of_window = block
            window = text[block : block + 2 * _FRAGMENT_LIMIT]
        offset = start - block
        try:
            value, end = decoder.raw_decode(window, offset)
        except json.JSONDecodeError as error:
            reached = len(window) if error.msg.startswith("Unterminated string") else error.pos
            work = _cost(work, max(1, reached - offset))
        except RecursionError:
            raise ValueError("an embedded JSON object is nested too deeply") from None
        except ValueError:
            work = _cost(work, min(_FRAGMENT_LIMIT, len(window) - offset))
        else:
            size = end - offset
            work = _cost(work, size)
            if isinstance(value, dict) and size <= _FRAGMENT_LIMIT:
                fragment = window[offset:end]
                fits = (
                    fragment.isascii()
                    or len(fragment.encode("utf-16-le", errors="surrogatepass")) <= 2 * _FRAGMENT_LIMIT
                )
                if fits:
                    yield value
                    skip_until = block + end
                    continue
        if mode == "promptfoo":
            fragment = _flow_fragment(window, offset)
            work = _cost(work, len(fragment))
            if len(fragment) > 1 and (repaired := _repair(fragment)) is not None:
                yield repaired
                skip_until = start + len(fragment)


def _schema_values(value: Any) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _schema_values(child)
    elif isinstance(value, list):
        for child in value:
            _schema_values(child)
    elif not (
        value is None
        or isinstance(value, (bool, str))
        or (isinstance(value, int) and abs(value) < 2**53)
        or (isinstance(value, float) and math.isfinite(value))
    ):
        raise ValueError("an embedded object has a value outside the JSON Schema compatibility range")


def matches(output: Any, schema: Mapping[str, Any], mode: EmbeddedJson) -> bool:
    """Whether any embedded object matches the schema. Raise when validation is uncertain."""
    text = output if isinstance(output, str) else json.dumps(output, separators=(",", ":"), ensure_ascii=False)
    for value in _objects(text, mode):
        if not schema:
            return True
        try:
            if mode == "promptfoo":
                _schema_values(value)
            if _json_schema.problem(value, schema) is None:
                return True
        except RecursionError:
            raise ValueError("the embedded object or its schema is nested too deeply") from None
    return False
