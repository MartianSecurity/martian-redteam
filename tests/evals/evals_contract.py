"""The server's frozen evals API contract, for testing the core against it.

``contracts/evals-api-v8.json`` is an unchanged copy of ``contracts/evals-api.json`` in
MartianSecurity/MartianBackend at version 8 (``info.version``), generated there by
``scripts/evals-contract.py`` from the server's routes. When the server raises the version, copy
the file again under the new version's name and update ``VERSION``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

VERSION = "8"
CONTRACT: dict[str, Any] = json.loads(
    (Path(__file__).parent / "contracts" / f"evals-api-v{VERSION}.json").read_text("utf-8")
)

FORMATS = FormatChecker(["uuid"])


@FORMATS.checks("date-time", raises=ValueError)
def _date_time(value: object) -> bool:
    # RFC 3339: a date, a time and an offset. jsonschema checks date-time only with an extra package.
    return not isinstance(value, str) or ("T" in value and datetime.fromisoformat(value).tzinfo is not None)


def operation(method: str, path: str) -> dict[str, Any]:
    found: dict[str, Any] = CONTRACT["paths"][path][method.lower()]
    return found


def response_schema(method: str, path: str, status: int = 200) -> dict[str, Any]:
    schema: dict[str, Any] = operation(method, path)["responses"][str(status)]["content"]["application/json"]["schema"]
    return schema


def validate_response(method: str, path: str, body: Any, status: int = 200) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``body`` is a response the contract allows,
    with UUIDs and date-times checked."""
    schema = {**response_schema(method, path, status), "components": CONTRACT["components"]}
    Draft202012Validator(schema, format_checker=FORMATS).validate(body)


def validate_request(schema_name: str, body: Any) -> None:
    schema = {"$ref": f"#/components/schemas/{schema_name}", "components": CONTRACT["components"]}
    Draft202012Validator(schema, format_checker=FORMATS).validate(body)


def references(value: Any) -> set[str]:
    """The component schemas ``value`` names, directly or through other schemas."""
    found: set[str] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            ref = item.get("$ref")
            if isinstance(ref, str) and (name := ref.rsplit("/", 1)[-1]) not in found:
                found.add(name)
                pending.append(CONTRACT["components"]["schemas"].get(name, {}))
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found
