"""A JSON Schema subset for the ``json_schema`` check, with no dependency beyond the standard library.

The keywords below are checked as JSON Schema 2020-12 defines them. Annotations that never change
whether a value is valid are accepted and ignored, ``format`` among them, as the standard does by
default. Any other keyword is refused when the check is created, so a schema is never half
applied: a constraint this module cannot check is an error, not a pass.

- Any value: ``type``, ``enum``, ``const``, ``allOf``, ``anyOf``, ``oneOf``, ``not``, ``$ref``.
- Objects: ``properties``, ``required``, ``additionalProperties``.
- Arrays: ``items``, ``minItems``, ``maxItems``, ``uniqueItems``.
- Strings: ``minLength``, ``maxLength``, ``pattern`` (a Python regular expression, searched).
- Numbers: ``minimum``, ``maximum``, ``exclusiveMinimum``, ``exclusiveMaximum``.

``$ref`` points into the same schema (``#/$defs/Name``, as Pydantic's ``model_json_schema()``
writes it); a reference to another document is refused.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import unquote

from martian.evals._digits import ascii_digits

TYPES = ("null", "boolean", "integer", "number", "string", "array", "object")
# Keywords that describe a value without constraining it.
ANNOTATIONS = frozenset(
    {
        "$schema",
        "$comment",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "format",
        # OpenAPI's hint for a oneOf, which Pydantic adds; the oneOf itself does the checking.
        "discriminator",
    }
)
_SCHEMA_LISTS = ("allOf", "anyOf", "oneOf")
_SCHEMAS = ("not", "items", "additionalProperties")
_COUNTS = ("minItems", "maxItems", "minLength", "maxLength")
# Each range keyword, and when a number is outside it.
_OUTSIDE: dict[str, Callable[[Any, Any], bool]] = {
    "minimum": operator.lt,
    "maximum": operator.gt,
    "exclusiveMinimum": operator.le,
    "exclusiveMaximum": operator.ge,
}
_BOUNDS = tuple(_OUTSIDE)
_DEFINITIONS = ("$defs", "definitions")
KEYWORDS = frozenset(
    {"type", "enum", "const", "$ref", "properties", "required", "uniqueItems", "pattern"}
    | set(_SCHEMA_LISTS)
    | set(_SCHEMAS)
    | set(_COUNTS)
    | set(_BOUNDS)
    | set(_DEFINITIONS)
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_integer(value: Any) -> bool:
    return _is_number(value) and float(value).is_integer()


# --- the schema itself ------------------------------------------------------------------------


def check_schema(schema: Any) -> None:
    """Raise ``ValueError`` naming the first part of ``schema`` this subset cannot check."""
    if not isinstance(schema, Mapping):
        raise ValueError("the schema must be a JSON object")
    try:
        _check_node(schema, schema, "schema", top=True)
    except RecursionError:
        raise ValueError("the schema is nested too deeply") from None


def _check_node(node: Any, root: Mapping[str, Any], where: str, *, top: bool = False) -> None:
    if isinstance(node, bool):
        return
    if not isinstance(node, Mapping):
        raise ValueError(f"{where} must be a JSON object, true or false")
    for keyword in node:
        # $id names the whole document; inside it, it would change what a $ref points to.
        if keyword not in KEYWORDS and keyword not in ANNOTATIONS and not (keyword == "$id" and top):
            raise ValueError(f"{where}: the json_schema check does not support {keyword!r}")
    if "type" in node:
        kinds = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if not kinds or not all(isinstance(kind, str) and kind in TYPES for kind in kinds):
            raise ValueError(f"{where}.type must be one of {', '.join(TYPES)}, or a list of them")
    if "enum" in node and not isinstance(node["enum"], list):
        raise ValueError(f"{where}.enum must be a list")
    required = node.get("required", [])
    if not isinstance(required, list) or not all(isinstance(key, str) for key in required):
        raise ValueError(f"{where}.required must be a list of strings")
    for keyword in _COUNTS:
        if keyword in node and not (_is_integer(node[keyword]) and node[keyword] >= 0):
            raise ValueError(f"{where}.{keyword} must be a whole number, 0 or more")
    for keyword in _BOUNDS:
        if keyword in node and not _is_number(node[keyword]):
            raise ValueError(f"{where}.{keyword} must be a number")
    if not isinstance(node.get("uniqueItems", False), bool):
        raise ValueError(f"{where}.uniqueItems must be true or false")
    if "pattern" in node:
        if not isinstance(node["pattern"], str):
            raise ValueError(f"{where}.pattern must be a string")
        try:
            re.compile(node["pattern"])
        except re.error as error:
            raise ValueError(f"{where}.pattern is not a valid regular expression: {error}") from None
    if "$ref" in node:
        _resolve(node["$ref"], root, where)
    for keyword in _SCHEMA_LISTS:
        if keyword in node:
            if not isinstance(node[keyword], list) or not node[keyword]:
                raise ValueError(f"{where}.{keyword} must be a nonempty list of schemas")
            for position, item in enumerate(node[keyword]):
                _check_node(item, root, f"{where}.{keyword}.{position}")
    for keyword in _SCHEMAS:
        if keyword in node:
            _check_node(node[keyword], root, f"{where}.{keyword}")
    for keyword in ("properties", *_DEFINITIONS):
        if keyword in node:
            if not isinstance(node[keyword], Mapping):
                raise ValueError(f"{where}.{keyword} must be a JSON object of schemas")
            for name, item in node[keyword].items():
                _check_node(item, root, f"{where}.{keyword}.{name}")


def _resolve(reference: Any, root: Mapping[str, Any], where: str) -> Any:
    """The schema a ``$ref`` points to: a JSON pointer into this same schema."""
    if not isinstance(reference, str) or not reference.startswith("#"):
        raise ValueError(f"{where}.$ref must point into this schema, starting with '#'")
    node: Any = root
    pointer = unquote(reference[1:])
    for part in pointer.split("/")[1:] if pointer else []:
        step = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and step in node:
            node = node[step]
        elif isinstance(node, list) and ascii_digits(step) and int(step) < len(node):
            node = node[int(step)]
        else:
            raise ValueError(f"{where}.$ref {reference!r} points to nothing in this schema")
    if not isinstance(node, (Mapping, bool)):
        raise ValueError(f"{where}.$ref {reference!r} does not point to a schema")
    return node


# --- a value against it -----------------------------------------------------------------------

# Checks one value against one schema at a place in the output: (value, schema, place) -> problem.
_Inner = Callable[..., str | None]


def _same(one: Any, other: Any) -> bool:
    """JSON equality: 1 equals 1.0, but true is not 1."""
    if isinstance(one, bool) or isinstance(other, bool):
        return isinstance(one, bool) and isinstance(other, bool) and one == other
    if _is_number(one) and _is_number(other):
        return bool(one == other)
    if isinstance(one, list) and isinstance(other, list):
        return len(one) == len(other) and all(_same(a, b) for a, b in zip(one, other, strict=True))
    if isinstance(one, dict) and isinstance(other, dict):
        return one.keys() == other.keys() and all(_same(one[key], other[key]) for key in one)
    return type(one) is type(other) and bool(one == other)


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if _is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _has_type(value: Any, kind: str) -> bool:
    if kind == "integer":
        return _is_integer(value)
    return _kind(value) == kind


def problem(value: Any, schema: Mapping[str, Any]) -> str | None:
    """The first way ``value`` does not match ``schema`` (a schema :func:`check_schema` accepted),
    with the place in the value, or ``None`` when it matches."""
    try:
        return _problem(value, schema, schema, "$", frozenset())
    except RecursionError:
        raise ValueError("the output is nested too deeply to check against the schema") from None


def _problem(value: Any, node: Any, root: Mapping[str, Any], at: str, active: frozenset[tuple[int, str]]) -> str | None:
    if node is True:
        return None
    if node is False:
        return f"{at}: the schema allows no value here"
    # A $ref chain that comes back to the same schema at the same place never ends.
    key = (id(node), at)
    if key in active:
        raise ValueError("the schema refers to itself without end")
    active = active | {key}

    def inner(item: Any, subschema: Any, place: str = at) -> str | None:
        return _problem(item, subschema, root, place, active)

    if "$ref" in node and (found := inner(value, _resolve(node["$ref"], root, "schema"))):
        return found
    if "type" in node:
        kinds = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if not any(_has_type(value, kind) for kind in kinds):
            return f"{at}: expected {' or '.join(kinds)}, got {_kind(value)}"
    if "const" in node and not _same(value, node["const"]):
        return f"{at}: not the value the schema requires"
    if "enum" in node and not any(_same(value, option) for option in node["enum"]):
        return f"{at}: not one of the {len(node['enum'])} values the schema allows"
    for subschema in node.get("allOf", []):
        if found := inner(value, subschema):
            return found
    if "anyOf" in node and all(inner(value, subschema) for subschema in node["anyOf"]):
        return f"{at}: matches none of the anyOf schemas"
    if "oneOf" in node and (count := sum(inner(value, s) is None for s in node["oneOf"])) != 1:
        return f"{at}: matches {count} of the oneOf schemas, not exactly one"
    if "not" in node and inner(value, node["not"]) is None:
        return f"{at}: matches the schema under not"
    if isinstance(value, dict):
        return _object_problem(value, node, inner, at)
    if isinstance(value, list):
        return _array_problem(value, node, inner, at)
    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            return f"{at}: shorter than {node['minLength']} characters"
        if "maxLength" in node and len(value) > node["maxLength"]:
            return f"{at}: longer than {node['maxLength']} characters"
        if "pattern" in node and re.search(node["pattern"], value) is None:
            return f"{at}: does not match the pattern"
    if _is_number(value):
        for keyword, outside in _OUTSIDE.items():
            if keyword in node and outside(value, node[keyword]):
                return f"{at}: {value!r} is outside {keyword} {node[keyword]!r}"
    return None


def _object_problem(value: dict[str, Any], node: Mapping[str, Any], inner: _Inner, at: str) -> str | None:
    if missing := [key for key in node.get("required", []) if key not in value]:
        return f"{at}: missing required {', '.join(missing)}"
    properties = node.get("properties", {})
    for key, item in value.items():
        place = f"{at}.{key}"
        if key in properties:
            found = inner(item, properties[key], place)
        elif "additionalProperties" in node:
            if node["additionalProperties"] is False:
                return f"{at}: {key} is not a property the schema allows"
            found = inner(item, node["additionalProperties"], place)
        else:
            found = None
        if found:
            return found
    return None


def _array_problem(value: list[Any], node: Mapping[str, Any], inner: _Inner, at: str) -> str | None:
    if "minItems" in node and len(value) < node["minItems"]:
        return f"{at}: fewer than {node['minItems']} items"
    if "maxItems" in node and len(value) > node["maxItems"]:
        return f"{at}: more than {node['maxItems']} items"
    if node.get("uniqueItems") and any(
        _same(value[i], value[j]) for i in range(len(value)) for j in range(i + 1, len(value))
    ):
        return f"{at}: items are not unique"
    if "items" in node:
        for position, item in enumerate(value):
            if found := inner(item, node["items"], f"{at}[{position}]"):
                return found
    return None


__all__ = ["ANNOTATIONS", "KEYWORDS", "check_schema", "problem"]
