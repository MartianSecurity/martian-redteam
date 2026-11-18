"""Suite audits: the core's own routes, which the server's frozen contract does not have.

``POST /v1/evals/suites/{suite_version_id}/audit`` audits a saved suite version over a published
dataset (:func:`martian.evals.audit.audit_saved_suite`): the suite's built-in code scorers grade
the replies of the built-in adversarial targets, a do-nothing and a cheating set, in place of a
target's. Nothing is called, neither a target nor a model, so it runs in the request. The result is
stored with that suite version, replacing its earlier audit, and ``GET`` on the same path reads it.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from martian.evals.audit import audit_saved_suite
from martian.evals.model import Case
from martian.evals.records import SuiteAudit
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, StoreDep, require_capability
from martian.evals.server.projection import DEFAULT_PROJECT, user_out
from martian.evals.server.scoring import find_suite_version

router = APIRouter(prefix="/v1/evals", tags=["evals"])


class SuiteAuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_name: str = Field(min_length=1, max_length=200)
    max_items: int = Field(default=100, ge=1, le=1000)


def audit_out(suite_version_id: uuid.UUID, audit: SuiteAudit) -> dict[str, Any]:
    return {
        "suite_version_id": str(suite_version_id),
        "suite_id": audit.suite_id,
        "version": audit.suite_version,
        "suite_digest": audit.suite_digest,
        "dataset": {"name": audit.dataset_name, "digest": audit.dataset_digest},
        "flagged": audit.flagged,
        "actor_id": user_out(audit.created_by) if audit.created_by else None,
        "created_at": audit.created_at,
        "result": audit.result,
    }


@router.post(
    "/suites/{suite_version_id}/audit",
    status_code=201,
    response_model=None,
    dependencies=[Depends(require_capability("saveSuites"))],
)
async def audit_suite_version(
    suite_version_id: uuid.UUID, body: SuiteAuditRequest, principal: PrincipalDep, records: RecordsDep, store: StoreDep
) -> dict[str, Any]:
    suite = await find_suite_version(records, principal, suite_version_id)
    if suite is None:
        raise HTTPException(404, "Not found")
    dataset = await store.find_dataset(principal, DEFAULT_PROJECT, body.dataset_name)
    if dataset is None:
        raise HTTPException(422, "Unknown dataset")
    cases: list[Case] = []
    while len(cases) < body.max_items:
        page = await store.read_cases(
            principal, DEFAULT_PROJECT, dataset.id, offset=len(cases), limit=min(100, body.max_items - len(cases))
        )
        cases.extend(page.items)
        if page.next_cursor is None or not page.items:
            break
    result = await asyncio.to_thread(audit_saved_suite, suite, cases)
    audit = SuiteAudit(
        suite_id=suite.id,
        suite_version=suite.version,
        suite_digest=suite.digest,
        dataset_name=dataset.name,
        dataset_digest=dataset.digest,
        flagged=result["flagged"],
        result=result,
    )
    return audit_out(suite_version_id, await records.put_suite_audit(principal, DEFAULT_PROJECT, audit))


@router.get("/suites/{suite_version_id}/audit", response_model=None)
async def suite_version_audit(
    suite_version_id: uuid.UUID, principal: PrincipalDep, records: RecordsDep
) -> dict[str, Any]:
    suite = await find_suite_version(records, principal, suite_version_id)
    found = (
        None if suite is None else await records.get_suite_audit(principal, DEFAULT_PROJECT, suite.id, suite.version)
    )
    if found is None:
        raise HTTPException(404, "Not found")
    return audit_out(suite_version_id, found)


__all__ = ["SuiteAuditRequest", "audit_out", "router"]
