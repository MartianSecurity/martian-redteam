"""Typed boundaries around vendor evaluation results and the Backend wire contract."""

from __future__ import annotations

from typing import Any, NotRequired, Protocol, TypedDict


class EvaluationResultLike(Protocol):
    # MLflow attaches its pandas result frame dynamically. Only this vendor boundary is dynamic.
    @property
    def result_df(self) -> Any: ...


class DatasetRow(TypedDict):
    item_id: str
    output: str
    error: str
    latency_ms: float | None


class Score(TypedDict):
    value: bool | None
    rationale: str


class CheckRow(TypedDict):
    input: str
    output: str
    error: str
    scores: dict[str, Score]


class RedteamGroup(TypedDict):
    vulnerability: str
    vulnerability_type: str
    gated: bool
    expected: int


class RedteamRow(TypedDict):
    vulnerability: str
    vulnerability_type: str
    attack_method: str
    value: bool | None
    input: str
    output: str
    rationale: str
    error: str
    graph_revision: str
    policy_revision: NotRequired[str]
    simulation_operation_id: NotRequired[str | None]
    evaluation_operation_id: NotRequired[str | None]


class RedteamReport(TypedDict):
    catalog_version: str
    graph_revision: str
    policy_revision: NotRequired[str]
    threshold: float
    groups: list[RedteamGroup]
    rows: list[RedteamRow]
