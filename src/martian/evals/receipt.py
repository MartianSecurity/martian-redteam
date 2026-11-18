"""Validate acknowledged Backend results before trusting a CI gate."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictBool, ValidationError, model_validator

from martian.evals.client import BackendError


class _Test(BaseModel):
    name: str
    kind: Literal["dataset", "checks", "redteam", "scan"]
    passed: StrictBool


class SessionReceipt(BaseModel):
    session_id: UUID
    verdict: Literal["passed", "relaxed", "failed"]
    test_count: int = Field(ge=1, strict=True)
    passed_count: int = Field(ge=0, strict=True)
    tests: list[_Test]
    export_state: str = "unknown"

    @model_validator(mode="after")
    def consistent(self) -> SessionReceipt:
        if self.test_count != len(self.tests) or self.passed_count != sum(t.passed for t in self.tests):
            raise ValueError("inconsistent receipt counts")
        if self.verdict != "failed" and self.passed_count != self.test_count:
            raise ValueError("incomplete passing receipt")
        if len({t.name for t in self.tests}) != self.test_count:
            raise ValueError("duplicate receipt test")
        return self


def session_receipt(response: dict[str, Any], tests: list[dict[str, Any]]) -> SessionReceipt:
    try:
        receipt = SessionReceipt.model_validate(response)
        if {(t.name, t.kind) for t in receipt.tests} != {(str(t["name"]), str(t["kind"])) for t in tests}:
            raise ValueError("receipt did not acknowledge every test")
    except (ValidationError, ValueError):
        raise BackendError(0, "Backend returned an incomplete or inconsistent session receipt") from None
    return receipt
