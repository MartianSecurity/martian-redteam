"""Published datasets: a port of the dataset routes of the server's ``app/api/evals.py`` and its
MLflow adapter (MartianSecurity/MartianBackend) onto the store.

Published datasets belong to the organisation, not to one project: they are the dataset versions
of the default project. A name lists its newest version; ``version`` is that version's content
digest. A case's ``status`` is its ``martian.status`` tag, ``ACTIVE`` by default.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import math
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from martian.evals.model import Case, DatasetVersion
from martian.evals.server.dependencies import PrincipalDep, StoreDep
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT, expected_text, prompt_text
from martian.evals.server.schemas import EvalDatasetItemOut, EvalDatasetItemsOut, EvalDatasetOut
from martian.evals.store import EvalStore

router = APIRouter(prefix="/v1/evals", tags=["evals"])

GLOBAL_PREFIX = "martian:"
SEARCH_LIMIT = 4000
CASE_PAGE = 1000
PREVIEW = 360


def dataset_out(dataset: DatasetVersion) -> EvalDatasetOut:
    return EvalDatasetOut(
        id=dataset.id,
        name=dataset.name,
        item_count=dataset.case_count,
        scope="global" if dataset.name.startswith(GLOBAL_PREFIX) else "org",
        version=dataset.digest,
    )


def dataset_item(case: Case) -> EvalDatasetItemOut:
    status = str(case.tags.get("martian.status", "ACTIVE")).upper()
    prompt, expected = prompt_text(case.inputs), expected_text(case.expectations)
    return EvalDatasetItemOut(
        id=case.id,
        status=status,
        prompt=prompt,
        expected=expected,
        usable=status == "ACTIVE" and bool(prompt) and bool(expected),
    )


async def published(store: EvalStore, principal: Principal) -> list[DatasetVersion]:
    """The newest version of each name in the default project, by name; none before it exists."""
    if await store.get_project(principal, DEFAULT_PROJECT) is None:
        return []
    newest: dict[str, DatasetVersion] = {}
    for dataset in await store.list_datasets(principal, DEFAULT_PROJECT):
        current = newest.get(dataset.name)
        if current is None or (dataset.version or 0) > (current.version or 0):
            newest[dataset.name] = dataset
    return [newest[name] for name in sorted(newest)]


async def find_published(store: EvalStore, principal: Principal, name: str) -> DatasetVersion:
    found = None
    if await store.get_project(principal, DEFAULT_PROJECT) is not None:
        found = await store.find_dataset(principal, DEFAULT_PROJECT, name)
    if found is None:
        raise HTTPException(404, "Unknown dataset")
    return found


@router.get("/datasets", response_model=list[EvalDatasetOut])
async def list_datasets(principal: PrincipalDep, store: StoreDep) -> list[EvalDatasetOut]:
    return [dataset_out(dataset) for dataset in await published(store, principal)]


@router.get("/datasets/{dataset_name}/items", response_model=EvalDatasetItemsOut)
async def list_dataset_items(
    dataset_name: str,
    principal: PrincipalDep,
    store: StoreDep,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    q: Annotated[str, Query(max_length=200)] = "",
) -> EvalDatasetItemsOut:
    # A search reads the first 4,000 cases and matches the id, prompt, expected text and status.
    if page * limit > SEARCH_LIMIT:
        raise HTTPException(
            422, "Dataset pages are limited to the first 4,000 records; narrow the search or use the operator export"
        )
    dataset = await find_published(store, principal, dataset_name)
    needle = q.strip().casefold()
    truncated = False
    if needle:
        cases: list[Case] = []
        while len(cases) <= SEARCH_LIMIT:
            found = await store.read_cases(principal, DEFAULT_PROJECT, dataset.id, offset=len(cases), limit=CASE_PAGE)
            cases.extend(found.items)
            if found.next_cursor is None:
                break
        truncated = len(cases) > SEARCH_LIMIT
        rows = [
            item
            for item in map(dataset_item, cases[:SEARCH_LIMIT])
            if needle in " ".join((item.id, item.prompt, item.expected, item.status)).casefold()
        ]
        total = len(rows)
        shown = rows[(page - 1) * limit : page * limit]
    else:
        found = await store.read_cases(principal, DEFAULT_PROJECT, dataset.id, offset=(page - 1) * limit, limit=limit)
        total = dataset.case_count
        shown = [dataset_item(case) for case in found.items]
    return EvalDatasetItemsOut(
        name=dataset_name,
        page=page,
        limit=limit,
        query=q,
        total=None if needle else total,
        total_pages=None if needle else math.ceil(total / limit),
        has_next=page * limit < total,
        scan_truncated=truncated,
        items=[
            item.model_copy(update={"prompt": item.prompt[:PREVIEW], "expected": item.expected[:PREVIEW]})
            for item in shown
        ],
    )


@router.get("/datasets/{dataset_name}", response_model=EvalDatasetOut)
async def get_dataset(dataset_name: str, principal: PrincipalDep, store: StoreDep) -> EvalDatasetOut:
    return dataset_out(await find_published(store, principal, dataset_name))
