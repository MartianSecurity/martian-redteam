"""Experimental local content contract. No inference result is an execution permit."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

ShortText = Annotated[str, Field(min_length=1, max_length=4096)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(Record):
    source_id: str = Field(min_length=1, max_length=128)
    kind: Literal["ticket", "email", "document", "crm_note", "mcp_description"]
    authenticity: Literal["application_verified", "unverified"]
    instruction_authority: Literal["task", "evidence_only"]
    confidentiality: Literal["synthetic_public", "internal", "restricted"]
    text: ShortText


class ActionContext(Record):
    schema_version: Literal[1] = 1
    task: ShortText
    authorized_scope: dict[str, JsonValue]
    sources: tuple[Source, ...] = Field(max_length=4)
    system: str = Field(min_length=1, max_length=24)
    operation: str = Field(min_length=1, max_length=256)
    arguments: dict[str, JsonValue]
    rules: tuple[ShortText, ...] = Field(min_length=1, max_length=8)
    evidence_status: Literal["complete", "missing", "stale", "conflicting"]

    @model_validator(mode="after")
    def bounded(self) -> Self:
        # JSON serializers may silently convert NaN/Infinity to null; reject them
        # before any model sees a different action than the caller supplied.
        encoded = json.dumps(self.model_dump(mode="python"), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 32768:
            raise ValueError("Context exceeds 32 KiB")
        if (
            set(self.authorized_scope) != {"operation", "arguments"}
            or not isinstance(self.authorized_scope["operation"], str)
            or not self.authorized_scope["operation"]
            or len(self.authorized_scope["operation"]) > 256
            or not isinstance(self.authorized_scope["arguments"], dict)
        ):
            raise ValueError("Scope must contain an operation and exact argument object")
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError("Source identifiers must be unique")
        return self


class Estimate(Record):
    schema_version: Literal[1] = 1
    status: Literal["evaluated", "insufficient_evidence", "unsupported", "invalid_input"]
    model_release: str
    rule_id: Literal["task_scope"] = "task_scope"
    violation_probability: float | None = Field(default=None, ge=0, le=1)
    reason: str
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def score_status(self) -> Self:
        if (self.status == "evaluated") != (self.violation_probability is not None):
            raise ValueError("Only evaluated estimates have probabilities")
        return self


class ContextRejected(ValueError):
    def __init__(self, status: str, reason: str):
        self.status, self.reason = status, reason
        super().__init__(reason)


def serialize(context: ActionContext, tokenizer, *, max_tokens: int = 512, ablate_task: bool = False) -> list[int]:
    """Insert structural IDs ourselves; never silently truncate or promote source text."""
    if context.evidence_status != "complete":
        raise ContextRejected("insufficient_evidence", context.evidence_status)
    if not 32 <= max_tokens <= 1024:
        raise ValueError("Context budget must be 32–1024 tokens")
    sections = []
    if not ablate_task:
        sections.append(("AUTHORIZED TASK", {"task": context.task, "scope": context.authorized_scope}))
        sections.append(("SOURCE EVIDENCE", [s.model_dump(mode="json") for s in context.sources]))
    sections.extend(
        [
            (
                "PROPOSED ACTION",
                {"system": context.system, "operation": context.operation, "arguments": context.arguments},
            ),
            ("RULES", context.rules),
        ]
    )
    reserved = set(tokenizer.all_special_ids)
    ids = [tokenizer.cls_token_id]
    for title, value in sections:
        text = (
            title + "\n" + json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        )
        tokens = tokenizer.encode(text, add_special_tokens=False)
        if reserved.intersection(tokens):
            raise ContextRejected("invalid_input", "content_contains_reserved_token")
        ids.extend(tokens)
        ids.append(tokenizer.sep_token_id)
    if len(ids) > max_tokens:
        raise ContextRejected("insufficient_evidence", "context_budget_exceeded")
    return ids
