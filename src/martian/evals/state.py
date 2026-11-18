"""Per-session record of what the gates saw, for the plugin to upload and enforce."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from martian.evals.types import EvaluationResultLike, RedteamReport
from martian.result import SessionResult, TestResult, provenance_of


@dataclass
class GateRecord:
    """One regression test's gate: a pinned-dataset gate (`dataset`), scorer checks (`checks`), red team
    probes (`redteam`) or a Scanner gate the server evaluated (`scan`)."""

    test: str
    kind: Literal["dataset", "checks", "redteam", "scan"]
    dataset_name: str = ""
    dataset_id: str = ""
    items_digest: str = ""
    truncated: bool = False
    threshold: float | None = None
    minimum_coverage: float = 1.0
    # Dataset gates: rows to upload. None when the result frame was unusable (nothing safe to send).
    rows: list[dict[str, Any]] | None = None
    # Checks: rows of the runner's own scorer results.
    check_rows: list[dict[str, Any]] | None = None
    redteam: RedteamReport | None = None
    # Scan: the gate evaluation the server stored and its scan, or the scan and why it has no verdict.
    scan: dict[str, Any] | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    failure: str = ""


@dataclass
class SessionState:
    records: list[GateRecord] = field(default_factory=list)
    skipped: int = 0
    enforce: bool = False
    worker: bool = False
    store_dir: str = ""
    current_test: str = ""
    notes: list[str] = field(default_factory=list)
    worker_errors: int = 0
    # Results evaluate() returned during the running test, to confirm each reached a gate.
    evaluated: list[EvaluationResultLike] = field(default_factory=list)
    receipt: SessionResult | None = None
    receipt_path: str = ""

    def reset(self) -> None:
        self.records.clear()
        self.skipped = 0
        self.enforce = False
        self.worker = False
        self.store_dir = ""
        self.current_test = ""
        self.notes.clear()
        self.worker_errors = 0
        self.evaluated.clear()
        self.receipt = None
        self.receipt_path = ""

    def test_name(self) -> str:
        """The running test's node id, made unique when one test records several gates."""
        base = self.current_test or "unnamed-test"
        if len(base) > 300:
            import hashlib

            base = base[:291] + "…" + hashlib.sha1(base.encode()).hexdigest()[:8]
        taken = {r.test for r in self.records}
        if base not in taken:
            return base
        n = 2
        while f"{base}#{n}" in taken:
            n += 1
        return f"{base}#{n}"

    def record(self, record: GateRecord) -> None:
        self.records.append(record)
        self.checkpoint()

    def checkpoint(self) -> None:
        if self.receipt is None or not self.receipt_path:
            return
        self.receipt.tests = [
            TestResult(
                name=r.test,
                kind=r.kind,
                passed=not bool(r.failure),
                provenance=provenance_of(r.kind),
            )
            for r in self.records
        ]
        self.receipt.problems = [r.failure for r in self.records if r.failure]
        self.receipt.save(self.receipt_path)


STATE = SessionState()
