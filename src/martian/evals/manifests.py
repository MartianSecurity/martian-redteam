"""Manifest-pinned dataset versions: a published version is used only while the store holds exactly
what publishing wrote.

Publishing a draft records a :class:`~martian.evals.records.DatasetManifest` in the record store
(the version's digest and each case's id and digest, in order) before it writes the version to the
store, reads the version back, and only then marks the manifest ``published``. A run reads every
case of a version that has a manifest and refuses to start when the cases differ from it, so a
version edited in the store after publishing (in MLflow's own UI, for example) is never evaluated
as if it were the published one. A version without a manifest (one Martian did not publish) is
read as it is.

While a publication is ``publishing``, new runs are refused, as the server's dataset publication
pauses admission. A publication that has been ``publishing`` for longer than
:data:`PUBLICATION_LEASE` no longer holds runs back: publishing the same draft again finishes it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from martian.evals.model import Case, DatasetVersion, digest
from martian.evals.records import DatasetManifest, RecordStore
from martian.evals.store import EvalStore, StoreError

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

PUBLICATION_LEASE = timedelta(minutes=10)
CASE_PAGE = 500
CHANGED = "Managed dataset content changed since it was published; publish it again"
PAUSED = "A dataset is being published; new runs are paused until it finishes"


def case_digest(case: Case) -> str:
    return digest(case.model_dump(mode="json"))


def manifest_for(
    manifest_id: str,
    project_id: str,
    name: str,
    cases: list[Case],
    *,
    draft_id: str | None = None,
    published_by: str | None = None,
) -> DatasetManifest:
    """The manifest of a version that will hold ``cases``; its digest is the store's digest of them."""
    return DatasetManifest(
        id=manifest_id,
        project_id=project_id,
        name=name,
        digest=digest([case.model_dump(mode="json") for case in cases]),
        case_ids=[case.id for case in cases],
        case_digests=[case_digest(case) for case in cases],
        draft_id=draft_id,
        published_by=published_by,
    )


def matches(manifest: DatasetManifest, dataset: DatasetVersion, cases: list[Case]) -> bool:
    """Whether the store's version and its cases are exactly what the manifest recorded."""
    return (
        dataset.digest == manifest.digest
        and [case.id for case in cases] == manifest.case_ids
        and [case_digest(case) for case in cases] == manifest.case_digests
    )


async def all_cases(store: EvalStore, principal: Principal, project_id: str, dataset_id: str) -> list[Case]:
    found: list[Case] = []
    while True:
        page = await store.read_cases(principal, project_id, dataset_id, offset=len(found), limit=CASE_PAGE)
        found.extend(page.items)
        if page.next_cursor is None or not page.items:
            return found


async def checked_cases(
    store: EvalStore, records: RecordStore, principal: Principal, project_id: str, dataset: DatasetVersion
) -> list[Case]:
    """Every case of the version, in order; ``conflict`` when it has a manifest it no longer matches."""
    cases = await all_cases(store, principal, project_id, dataset.id)
    manifest = await records.manifest_of(principal, project_id, dataset.id)
    if manifest is not None and not matches(manifest, dataset, cases):
        raise StoreError("conflict", CHANGED)
    return cases


async def publishing(records: RecordStore, principal: Principal, project_id: str, now: datetime) -> bool:
    """Whether a publication of the project is in progress and still within its lease."""
    return any(
        m.state == "publishing" and m.started_at is not None and m.started_at > now - PUBLICATION_LEASE
        for m in await records.list_publications(principal, project_id)
    )


__all__ = [
    "CHANGED",
    "PAUSED",
    "PUBLICATION_LEASE",
    "all_cases",
    "case_digest",
    "checked_cases",
    "manifest_for",
    "matches",
    "publishing",
]
