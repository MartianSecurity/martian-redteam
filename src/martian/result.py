"""Safe, versioned CI receipts; no target inputs, responses or credentials."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from martian._privacy import RedactionPolicy, redact_data
from martian._version import __version__


class TestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: Literal["dataset", "checks", "redteam", "scan"]
    passed: bool
    provenance: Literal["client_executed_server_scored", "client_executed_client_scored", "server_evaluated_gate"]
    run_id: str | None = None


def provenance_of(
    kind: str,
) -> Literal["client_executed_server_scored", "client_executed_client_scored", "server_evaluated_gate"]:
    """Who decided a test of ``kind``: the server scored a dataset run, the server evaluated a scan's gate,
    and the runner judged checks and red team probes."""
    if kind == "dataset":
        return "client_executed_server_scored"
    return "server_evaluated_gate" if kind == "scan" else "client_executed_client_scored"


class SessionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    sdk_version: str = __version__
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    state: Literal["running", "uploading", "passed", "failed", "offline"] = "running"
    exit_code: int = 1
    session_id: str | None = None
    verdict: Literal["passed", "relaxed", "failed"] | None = None
    upload: Literal["not_started", "pending", "persisted", "failed", "offline"] = "not_started"
    export_state: str = "unknown"
    tests: list[TestResult] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    _redaction: RedactionPolicy = PrivateAttr(default_factory=RedactionPolicy)

    def _bind_redaction(self, policy: RedactionPolicy) -> SessionResult:
        self._redaction = self._redaction.combine(policy)
        return self

    @property
    def passed(self) -> bool:
        return self.state == "passed" and self.exit_code == 0 and self.upload == "persisted"

    def save(self, path: str | Path) -> Path:
        """Atomic replacement retains the last complete receipt if the process is interrupted."""
        destination = Path(path)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = redact_data(json.loads(self.model_dump_json()), policy=self._redaction)
        fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=destination.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return destination
