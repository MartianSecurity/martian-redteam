"""Explicit response-ID mappings. No argument inspection or text extraction."""

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from martian_redteam.runtime_schema import ResourceAccess, ResourceAction, ResourceName, ResourceReport


class RuntimeResourceRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: ResourceName
    resource_type: ResourceName
    action: ResourceAction
    path: tuple[ResourceName, ...] = Field(default=(), max_length=4)
    many: bool = False
    id_field: ResourceName


def describe_resources(
    result: dict[str, Any],
    rules: Sequence[RuntimeResourceRule],
    reference: Callable[[str, str, str], str],
) -> ResourceReport:
    if not rules or result.get("isError"):
        return ResourceReport()
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        return ResourceReport()
    resources: list[ResourceAccess] = []
    seen: set[tuple[str, str, str, str]] = set()
    omitted = 0
    for rule in rules:
        selected: Any = payload
        for part in rule.path:
            selected = selected.get(part) if isinstance(selected, dict) else None
        if (rule.many and not isinstance(selected, list)) or (not rule.many and not isinstance(selected, dict)):
            omitted += 1
            continue
        for item in selected if rule.many else [selected]:
            native_id = item.get(rule.id_field) if isinstance(item, dict) else None
            if not isinstance(native_id, str) or not 1 <= len(native_id) <= 256:
                omitted += 1
                continue
            ref = reference(rule.service, rule.resource_type, native_id)
            key = (rule.service, rule.resource_type, ref, rule.action)
            if key in seen:
                continue
            seen.add(key)
            if len(resources) == 10:
                omitted += 1
                continue
            resources.append(
                ResourceAccess(
                    service=rule.service,
                    resource_type=rule.resource_type,
                    resource_ref=ref,
                    action=rule.action,
                )
            )
    return ResourceReport(status="partial" if omitted else "reported", items=resources, omitted=omitted)
