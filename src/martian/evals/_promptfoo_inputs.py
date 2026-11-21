"""Read the data files used by the Promptfoo suite converter."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Collection
from pathlib import Path
from typing import Any, NoReturn

import yaml
from yaml.nodes import MappingNode

from martian.evals._promptfoo_yaml import CoreYamlLoader

FILE_PREFIX = "file://"
TEST_EXTENSIONS = frozenset({".yaml", ".yml", ".json", ".jsonl", ".csv"})
_MERGE = "tag:yaml.org,2002:merge"
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_THRESHOLD_COLUMN = re.compile(r"__config:__expected([1-9][0-9]*)?:threshold")
_SCRIPT_PREFIXES = {"fn": "javascript", "eval": "javascript", "grade": "llm-rubric"}


class PromptfooConfigError(Exception):
    """A Promptfoo file cannot be read, represented or written."""


def case_id(test: int, prompt: int, *, prompts: int, repeat: int | None = None) -> str:
    """A case ID shared by result imports and config conversion."""
    found = f"test {test + 1}" if prompts == 1 else f"test {test + 1}, prompt {prompt + 1}"
    return found if not repeat else f"{found} (repeat {repeat + 1})"


def prompt_group(position: int) -> str:
    """The same group label for a converted prompt and its exported column."""
    return f"Prompt {position + 1}"


def inline_prompt(value: Any) -> tuple[str, str] | None:
    """The authored text and label of a prompt with no external source."""
    raw: Any
    label: Any
    if isinstance(value, str):
        raw, label = value, value
    elif isinstance(value, dict):
        raw = value.get("raw")
        label = value.get("label")
        label = raw if label is None else label
    else:
        return None
    if not isinstance(raw, str) or not isinstance(label, str):
        return None
    if raw.startswith(("file://", "exec:", "portkey://", "langfuse://", "helicone://")):
        return None
    return raw, label


class _YamlLoader(CoreYamlLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            return super().construct_mapping(node, deep=deep)
        seen: set[str] = set()
        for key_node, _ in node.value:
            key = "<<" if key_node.tag == _MERGE else self.construct_object(key_node)
            if not isinstance(key, str):
                raise yaml.constructor.ConstructorError(None, None, "expected a text key", key_node.start_mark)
            if key in seen:
                raise yaml.constructor.ConstructorError(None, None, "duplicate mapping key", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def local_file(reference: str, base: Path, extensions: Collection[str]) -> Path | None:
    """Resolve a literal local data reference. It cannot run code or fetch a remote resource."""
    name = reference.removeprefix(FILE_PREFIX)
    if "://" in name or any(mark in name for mark in ("*", "?", "[", "{{")):
        return None
    path = base / name
    return path if path.suffix.lower() in extensions else None


def read_text(path: Path) -> str:
    try:
        if not path.is_file():
            raise PromptfooConfigError(f"{path.name}: no regular file at this path")
        with path.open(encoding="utf-8-sig", newline="" if path.suffix.lower() == ".csv" else None) as source:
            return source.read()
    except OSError as error:
        raise PromptfooConfigError(f"cannot read {path.name}: {error.strerror or type(error).__name__}") from None
    except UnicodeError:
        raise PromptfooConfigError(f"{path.name}: expected UTF-8 text") from None


def yaml_data(text: str, source: str) -> Any:
    try:
        return yaml.load(text, Loader=_YamlLoader)
    except yaml.MarkedYAMLError as error:
        position = f" at line {error.problem_mark.line + 1}" if error.problem_mark else ""
        raise PromptfooConfigError(
            f"{source}: invalid YAML{position}: {error.problem or 'cannot read document'}"
        ) from None
    except (yaml.YAMLError, ValueError, RecursionError):
        raise PromptfooConfigError(f"{source}: invalid YAML") from None


def _not_json_constant(_token: str) -> NoReturn:
    raise ValueError("non-finite numbers are not JSON")


def _json_data(text: str, source: str) -> Any:
    try:
        return json.loads(text, parse_constant=_not_json_constant)
    except (ValueError, RecursionError):
        raise PromptfooConfigError(f"{source}: invalid JSON") from None


def _finite_number(text: str) -> float | None:
    if _NUMBER.fullmatch(text.strip()):
        number = float(text)
        if math.isfinite(number):
            return number
    return None


def _assertion_parser(known: Collection[str]) -> re.Pattern[str]:
    kinds = "|".join(re.escape(kind) for kind in sorted(known, key=lambda item: (-len(item), item)))
    return re.compile(rf"(not-)?({kinds})(?:\(([0-9]+(?:\.[0-9]+)?)\))?(?::(.*))?", re.DOTALL)


def _csv_assertion(cell: str, expected: re.Pattern[str]) -> dict[str, Any]:
    prefix, separator, value = cell.partition(":")
    if separator and prefix in _SCRIPT_PREFIXES:
        return {"type": _SCRIPT_PREFIXES[prefix], "value": value.strip()}
    if cell.startswith(FILE_PREFIX):
        file = cell.removeprefix(FILE_PREFIX).partition(":")[0]
        # These are scorer references in Promptfoo, even though the assertion has no explicit type.
        if file.endswith(".py"):
            return {"type": "python", "value": cell}
        if file.endswith((".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")):
            return {"type": "javascript", "value": cell}
    if parsed := expected.fullmatch(cell):
        inverse, kind, threshold, value = parsed.groups()
        result: dict[str, Any] = {"type": (inverse or "") + kind}
        if value is not None:
            result["value"] = value.strip()
        if threshold is not None:
            result["threshold"] = _finite_number(threshold)
        elif kind in ("cost", "latency", "levenshtein", "not-levenshtein"):
            result["threshold"] = 0.75
        return result
    return {"type": "equals", "value": cell}


def _from_csv(cells: dict[str, str], *, expected: re.Pattern[str], source: str) -> dict[str, Any]:
    variables: dict[str, str] = {}
    assertions: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    options: dict[str, Any] = {}
    case: dict[str, Any] = {"vars": variables, "assert": assertions}
    thresholds: dict[int, float] = {}
    for key, value in cells.items():
        if key.startswith("__expected"):
            if value.strip():
                assertions.append(_csv_assertion(value.strip(), expected))
        elif key in ("__description", "__providerOutput"):
            if value:
                case[key[2:]] = value
        elif key in ("__prefix", "__suffix"):
            options[key[2:]] = value
        elif key == "__threshold":
            if (limit := _finite_number(value)) is not None:
                case["threshold"] = limit
        elif key.startswith("__metadata:"):
            name = key.removeprefix("__metadata:")
            if value.strip():
                if name.endswith("[]"):
                    metadata[name[:-2]] = [part.strip().replace(r"\,", ",") for part in re.split(r"(?<!\\),", value)]
                else:
                    metadata[name] = value
        elif key.startswith("__config:"):
            if not (match := _THRESHOLD_COLUMN.fullmatch(key)) or (limit := _finite_number(value)) is None:
                raise PromptfooConfigError(f"{source}: {key} needs a numeric assertion threshold")
            thresholds[int(match[1] or "1") - 1] = limit
        elif key not in ("__metadata", "__metric"):
            variables[key] = value
    for index, assertion in enumerate(assertions):
        if cells.get("__metric"):
            assertion["metric"] = cells["__metric"]
        if index in thresholds:
            assertion["threshold"] = thresholds[index]
    if metadata:
        case["metadata"] = metadata
    if options:
        case["options"] = options
    return case


def _csv_tests(text: str, path: Path, known_assertions: Collection[str]) -> list[tuple[str, Any]]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    expected = _assertion_parser(known_assertions)
    try:
        names = [name.strip() for name in next(reader, [])]
        if not names or not all(names) or len(set(names)) != len(names):
            raise PromptfooConfigError(f"{path.name}: expected unique nonempty CSV column names")
        tests = []
        for record in reader:
            source = f"{path.name}:{reader.line_num}"
            if len(record) != len(names):
                raise PromptfooConfigError(f"{source}: CSV row has {len(record)} cells, expected {len(names)}")
            tests.append((source, _from_csv(dict(zip(names, record, strict=True)), expected=expected, source=source)))
        return tests
    except csv.Error as error:
        raise PromptfooConfigError(f"{path.name}:{reader.line_num}: invalid CSV: {error}") from None


def read_test_file(path: Path, known_assertions: Collection[str]) -> list[tuple[str, Any]]:
    """Read a local fixture as (source, test) rows in document order."""
    text = read_text(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _csv_tests(text, path, known_assertions)
    if suffix == ".jsonl":
        rows = []
        for number, line in enumerate(text.split("\n"), 1):
            if line.strip():
                source = f"{path.name}:{number}"
                rows.append((source, _json_data(line, source)))
        return rows
    value = _json_data(text, path.name) if suffix == ".json" else yaml_data(text, path.name)
    tests = value if isinstance(value, list) else [value]
    return [(f"{path.name} row {number}", row) for number, row in enumerate(tests, 1)]
