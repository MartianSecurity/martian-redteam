"""Bounded static metadata inspection. Findings are review signals, never action authority."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from jsonschema import Draft202012Validator, SchemaError

SCANNER_VERSION = "martian-static-metadata-v2"
MAX_BYTES = 262144
TEXT_RULES = (
    (
        "instruction_override_language",
        re.compile(
            r"\b(?:ignore|disregard|override)\s+(?:(?:all|any|the|your|previous|prior|system|developer)\s+){0,4}"
            r"(?:instructions?|rules?|polic(?:y|ies))\b",
            re.I,
        ),
        "Text asks to disregard instructions or policy; review its context and authority.",
    ),
    (
        "concealment_language",
        re.compile(r"\b(?:do not|don't|never)\s+(?:tell|inform|notify)\s+(?:the\s+)?user\b", re.I),
        "Text requests concealment from the user; quotations and legitimate guidance can also match.",
    ),
    (
        "invisible_direction_control",
        re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]"),
        "Text contains invisible or bidirectional control characters.",
    ),
)


def inspect_artifact(kind: Literal["mcp_tool", "skill"], value: Any) -> dict[str, Any]:
    """Inspect exactly the supplied artifact without executing it, fetching URLs or following references."""
    if kind not in {"mcp_tool", "skill"}:
        raise ValueError("Unsupported artifact kind")
    # Bound attacker-supplied structure before recursive encoding or schema validation.
    pending_values = [(value, 0)]
    nodes = characters = 0
    while pending_values:
        node, depth = pending_values.pop()
        nodes += 1
        if nodes > 8192 or depth > 64:
            raise ValueError("Artifact exceeds inspection node or depth bound")
        if isinstance(node, dict):
            if len(node) > 8192 or len(pending_values) + len(node) > 8192:
                raise ValueError("Artifact exceeds inspection node bound")
            for key, child in node.items():
                if not isinstance(key, str):
                    raise ValueError("Artifact keys must be strings")
                characters += len(key)
                pending_values.append((child, depth + 1))
        elif isinstance(node, list):
            if len(node) > 8192 or len(pending_values) + len(node) > 8192:
                raise ValueError("Artifact exceeds inspection node bound")
            pending_values.extend((item, depth + 1) for item in node)
        elif isinstance(node, str):
            characters += len(node)
        elif node is not None and not isinstance(node, (bool, int, float)):
            raise ValueError("Artifact must contain only JSON values")
        if characters > MAX_BYTES:
            raise ValueError("Artifact exceeds 256 KiB")
    raw = (
        value.encode("utf-8")
        if kind == "skill" and isinstance(value, str)
        else json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )
    if len(raw) > MAX_BYTES:
        raise ValueError("Artifact exceeds 256 KiB")
    findings: list[dict[str, Any]] = []

    def add(rule: str, path: str, message: str, severity: str = "review") -> None:
        if len(findings) < 128:
            findings.append({"rule_id": rule, "path": path, "severity": severity, "message": message})

    texts: list[tuple[str, str]] = []
    checks = ["bounded_text_patterns", "unicode_controls"]
    if kind == "skill":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Skill content must be nonempty UTF-8 text")
        texts.append(("/content", value))
    else:
        if not isinstance(value, dict) or set(value) != {"name", "description", "input_schema"}:
            raise ValueError("MCP artifact requires exactly name, description and input_schema")
        if not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 256:
            raise ValueError("MCP tool name must be 1–256 characters")
        if not isinstance(value["description"], str):
            raise ValueError("MCP description must be text")
        texts.append(("/description", value["description"]))
        schema = value["input_schema"]
        checks.extend(["json_schema_2020_12", "local_reference_scope", "object_argument_boundary"])
        if not isinstance(schema, dict) or schema.get("type") != "object":
            add("input_schema_not_object", "/input_schema", "MCP arguments require an object schema.", "error")
        pending = [("/input_schema", schema, 0)]
        visited = 0
        while pending:
            path, node, depth = pending.pop()
            visited += 1
            if visited > 4096 or depth > 32:
                raise ValueError("Schema exceeds inspection node or depth bound")
            if isinstance(node, dict):
                for key, child in node.items():
                    if not isinstance(key, str):
                        raise ValueError("Schema keys must be strings")
                    child_path = path + "/" + key.replace("~", "~0").replace("/", "~1")
                    if key in {"$ref", "$dynamicRef"} and isinstance(child, str) and not child.startswith("#"):
                        add(
                            "external_schema_reference",
                            child_path,
                            "External schema references are not fetched or verified by this inspection.",
                        )
                    if key in {"description", "title"} and isinstance(child, str):
                        texts.append((child_path, child))
                    pending.append((child_path, child, depth + 1))
            elif isinstance(node, list):
                pending.extend((path + f"/{index}", item, depth + 1) for index, item in enumerate(node))
        try:
            Draft202012Validator.check_schema(schema)
        except (SchemaError, TypeError):
            add("invalid_json_schema", "/input_schema", "Input schema is not valid JSON Schema 2020-12.", "error")
        if isinstance(schema, dict) and schema.get("additionalProperties") is not False:
            add(
                "open_argument_properties",
                "/input_schema",
                "The root schema does not forbid undeclared properties; task grants must constrain arguments.",
            )
    for path, text in texts:
        for rule, expression, message in TEXT_RULES:
            match = expression.search(text)
            if match:
                add(rule, path, message)
    return {
        "schema_version": 1,
        "scanner_version": SCANNER_VERSION,
        "artifact_kind": kind,
        "artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "content_bytes": len(raw),
        "status": "invalid"
        if any(x["severity"] == "error" for x in findings)
        else "review_required"
        if findings
        else "no_signal",
        "checks": checks,
        "findings": findings,
        "finding_limit": 128,
        "finding_limit_reached": len(findings) == 128,
        "coverage": "Static metadata only; no code execution, contextual model or behavioral safety verdict.",
    }
