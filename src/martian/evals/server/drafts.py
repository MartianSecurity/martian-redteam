"""Dataset authoring: a port of the organisation routes of the server's ``app/api/evals_datasets.py``
and ``app/evals_dataset_drafts.py`` (MorningHQ/MartianBackend) onto the record store and the store.

A draft is an editable working copy in the record store's default project. Its records are the
server's (``id``, ``input``, ``expected_output``, ``status``, ``metadata``), kept as cases:

- ``input`` is the case's ``inputs`` when it is an object, ``inputs.message`` when it is text, and
  ``inputs.input`` otherwise; the tag ``martian.input_form`` says which, so a record reads back as
  it was written. ``expected_output`` is ``expectations.expected_response``, and ``status`` is the
  ``martian.status`` tag, as for published datasets.
- Every edit names the revision it was made against and raises it.
- Publishing writes the draft's cases as a dataset version of the default project, named as the
  draft, and marks the draft published. It is one store write, made in the request, so it ends
  ``published`` at once: a dataset operation is the record of a published draft, and none is ever
  left to resume or abort. ``sha256`` is the dataset version's content digest.

FastAPI publishes a route's docstring as the operation's description, so a route here has one
exactly when the contract's operation has a description, and it is that description.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from martian.evals.manifests import all_cases as stored_cases
from martian.evals.manifests import manifest_for, matches
from martian.evals.model import Case, DatasetDraft, digest
from martian.evals.records import RecordStore
from martian.evals.server.datasets import GLOBAL_PREFIX, find_published
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, StoreDep, require_capability
from martian.evals.server.principal import Principal
from martian.evals.server.projection import (
    DEFAULT_PROJECT,
    EXPECTED_KEYS,
    PROMPT_KEYS,
    derived_id,
    first_text,
    item_out,
    result_id,
    user_uuid,
)
from martian.evals.server.runs import find_run, run_results, writable_project
from martian.evals.server.write_schemas import (
    AbortIn,
    DeleteIn,
    DraftCreateIn,
    DraftOut,
    DraftPatchIn,
    FromEvalRunIn,
    MergeIn,
    PublishIn,
    RecordIn,
    RecordPatchIn,
    ResumeIn,
)
from martian.evals.store import EvalStore

router = APIRouter(prefix="/v1/evals", tags=["eval dataset authoring"])

MAX_RECORDS = 100_000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_REPORTED_ERRORS = 20
ITEM_KEYS = {"id", "input", "expected_output", "expectedOutput", "status", "metadata"}
STATUSES = ("ACTIVE", "ARCHIVED")
PAGE = 1000
FORM = "martian.input_form"


@dataclass
class ParsedUpload:
    name: str | None
    description: str | None
    items: list[dict[str, Any]]
    generated_ids: int


def _generated_id() -> str:
    return "item-" + uuid.uuid4().hex[:16]


def validate_name(given: str) -> str:
    """A dataset name: 1-200 characters without '/' or control characters, and not the reserved
    global prefix. '/' would break the single-segment ``/v1/evals/datasets/{name}`` routes."""
    name = given.strip()
    if not 1 <= len(name) <= 200 or "/" in name or any(ord(c) < 32 for c in name):
        raise HTTPException(422, "Dataset names are 1-200 characters without '/' or control characters")
    if name.casefold().startswith(GLOBAL_PREFIX):
        raise HTTPException(422, f"Names starting with '{GLOBAL_PREFIX}' are reserved for global datasets")
    return name


# --- records as cases -------------------------------------------------------------------------


def _normalise(raw: Any, row: int, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        errors.append(f"row {row}: record must be an object")
        return None
    if unknown := set(raw) - ITEM_KEYS:
        errors.append(f"row {row}: unsupported fields {sorted(unknown)}")
        return None
    if "expected_output" in raw and "expectedOutput" in raw:
        errors.append(f"row {row}: use expected_output or expectedOutput, not both")
        return None
    identity = raw.get("id")
    if identity is not None and (not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 80):
        errors.append(f"row {row}: id must be a 1-80 character string")
        return None
    status = str(raw.get("status") or "ACTIVE").upper()
    if status not in STATUSES:
        errors.append(f"row {row}: status must be ACTIVE or ARCHIVED")
        return None
    metadata = raw.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append(f"row {row}: metadata must be an object")
        return None
    return {
        "id": identity.strip() if identity else None,
        "input": raw.get("input"),
        "expected_output": raw.get("expected_output", raw.get("expectedOutput")),
        "status": status,
        "metadata": metadata,
    }


def _one(raw: Any) -> dict[str, Any]:
    errors: list[str] = []
    found = _normalise(raw, 1, errors)
    if found is None:
        raise HTTPException(422, errors[0].removeprefix("row 1: "))
    return found


def to_case(item: dict[str, Any]) -> Case:
    raw = item.get("input")
    tags = {"martian.status": item.get("status") or "ACTIVE"}
    if isinstance(raw, dict) and raw:
        inputs: dict[str, Any] = raw
    elif isinstance(raw, str):
        inputs, tags[FORM] = {"message": raw}, "text"
    else:
        inputs, tags[FORM] = {"input": raw}, "raw"
    expected = item.get("expected_output")
    return Case(
        id=item["id"],
        inputs=inputs,
        expectations={} if expected is None else {"expected_response": expected},
        tags=tags,
        metadata=item.get("metadata") or {},
    )


def from_case(case: Case) -> dict[str, Any]:
    form = case.tags.get(FORM)
    raw = case.inputs.get("message") if form == "text" else case.inputs.get("input") if form == "raw" else case.inputs
    expected = case.expectations.get("expected_response", case.expectations or None)
    return {
        "id": case.id,
        "input": raw,
        "expected_output": expected,
        "status": str(case.tags.get("martian.status", "ACTIVE")).upper(),
        "metadata": case.metadata or None,
    }


def _prompt(raw: Any) -> str | None:
    return first_text(raw, PROMPT_KEYS)


def _expected(raw: Any) -> str | None:
    return first_text(raw, EXPECTED_KEYS)


def record_out(case: Case, ordinal: int, updated_at: datetime | None) -> dict[str, Any]:
    item = from_case(case)
    return {
        "id": case.id,
        "ordinal": ordinal,
        "input": item["input"],
        "expected_output": item["expected_output"],
        "status": item["status"],
        "metadata": item["metadata"],
        "usable": item["status"] == "ACTIVE"
        and bool(_prompt(item["input"]))
        and _expected(item["expected_output"]) is not None,
        "updated_at": updated_at,
    }


# --- uploads ----------------------------------------------------------------------------------


def _csv_value(value: str) -> Any:
    text = value.strip()
    if text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except ValueError:
            return value
    return value


def _csv_rows(content: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or "input" not in reader.fieldnames:
        raise HTTPException(422, "CSV needs a header row with at least an 'input' column")
    rows = []
    for raw in reader:
        row: dict[str, Any] = {}
        metadata: dict[str, Any] = {}
        for key, value in raw.items():
            if key is None or value is None:
                raise HTTPException(422, f"CSV row {reader.line_num - 1} has more cells than the header")
            key = key.strip()
            if key.startswith("metadata."):
                if value != "":
                    metadata[key.removeprefix("metadata.")] = _csv_value(value)
            elif key in ("input", "expected_output"):
                row[key] = _csv_value(value) if value != "" else None
            elif value != "":
                row[key] = value
        if metadata:
            row["metadata"] = metadata
        rows.append(row)
    return rows


def parse_upload(fmt: str, *, content: str | None = None, dataset: Any = None) -> ParsedUpload:
    """JSON (``{name?, description?, items}`` or a bare list), JSONL or CSV. The whole upload is
    refused if any row is invalid, naming the first rows that failed, so a merge never half-applies."""
    if content is not None and len(content.encode()) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Uploads are limited to 20 MiB")
    name = description = None
    try:
        if fmt == "json":
            value = json.loads(content) if content is not None else dataset
            if isinstance(value, dict):
                if set(value) - {"name", "description", "items"}:
                    raise HTTPException(422, "JSON datasets accept only name, description and items")
                name, description, rows = value.get("name"), value.get("description"), value.get("items")
            else:
                rows = value
        elif fmt == "jsonl":
            if content is None:
                raise HTTPException(422, "JSONL uploads need content")
            rows = [json.loads(line) for line in content.splitlines() if line.strip()]
        else:
            if content is None:
                raise HTTPException(422, "CSV uploads need content")
            rows = _csv_rows(content)
    except ValueError as error:
        raise HTTPException(422, f"Upload is not valid {fmt.upper()}: {str(error)[:160]}") from None
    if not isinstance(rows, list):
        raise HTTPException(422, "Upload must contain a list of records")
    if len(rows) > MAX_RECORDS:
        raise HTTPException(422, f"Uploads are limited to {MAX_RECORDS:,} records")
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    generated = 0
    for index, raw in enumerate(rows, start=1):
        item = _normalise(raw, index, errors)
        if item is None:
            if len(errors) >= MAX_REPORTED_ERRORS:
                break
            continue
        if item["id"] is None:
            item["id"], generated = _generated_id(), generated + 1
        items.append(item)
    if errors:
        raise HTTPException(422, "Upload rejected: " + "; ".join(errors[:MAX_REPORTED_ERRORS]))
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Upload rejected: record ids must be unique within one upload")
    return ParsedUpload(
        name if isinstance(name, str) else None,
        description if isinstance(description, str) else None,
        items,
        generated,
    )


async def _limit_body(request: Request) -> None:
    """Refuse an oversized upload from its Content-Length before the body is read."""
    length = request.headers.get("content-length")
    # JSON string escaping can grow a 20 MiB file; allow headroom, parse_upload enforces the limit.
    if length and length.isdigit() and int(length) > MAX_UPLOAD_BYTES * 2:
        raise HTTPException(413, "Uploads are limited to 20 MiB")


# --- drafts -----------------------------------------------------------------------------------


def operation_id(draft: DatasetDraft) -> uuid.UUID | None:
    if draft.state != "published" or draft.published_digest is None:
        return None
    return derived_id("dataset-operation", draft.id, draft.published_digest)


def draft_out(draft: DatasetDraft, names: dict[str, str]) -> DraftOut:
    created = draft.created_at or datetime.now(UTC)
    return DraftOut(
        id=uuid.UUID(draft.id),
        scope="org",
        name=draft.name,
        public_name=draft.name,
        description=draft.description,
        source_dataset=names.get(draft.source_dataset_id or ""),
        revision=draft.revision,
        state=draft.state,
        record_count=draft.case_count,
        operation_id=operation_id(draft),
        published_sha256=draft.published_digest,
        created_by=user_uuid(draft.created_by or ""),
        updated_by=user_uuid(draft.updated_by or draft.created_by or ""),
        created_at=created,
        updated_at=draft.updated_at or created,
    )


async def dataset_names(store: EvalStore, principal: Principal) -> dict[str, str]:
    """The name of every dataset version of the default project, by id."""
    if await store.get_project(principal, DEFAULT_PROJECT) is None:
        return {}
    return {d.id: d.name for d in await store.list_datasets(principal, DEFAULT_PROJECT)}


async def get_draft(records: RecordStore, principal: Principal, draft_id: uuid.UUID) -> DatasetDraft:
    draft = await records.get_draft(principal, DEFAULT_PROJECT, str(draft_id))
    if draft is None:
        # 404, not 403, so another organisation's draft ids reveal nothing.
        raise HTTPException(404, "Dataset draft not found")
    return draft


async def editable(records: RecordStore, principal: Principal, draft_id: uuid.UUID, base_revision: int) -> DatasetDraft:
    draft = await get_draft(records, principal, draft_id)
    if draft.state != "editing":
        raise HTTPException(409, f"Draft is {draft.state}; open a new draft to make changes")
    if base_revision != draft.revision:
        raise HTTPException(409, "Draft changed since you loaded it; reload and try again")
    return draft


async def all_cases(records: RecordStore, principal: Principal, draft_id: str) -> list[Case]:
    cases: list[Case] = []
    while True:
        page = await records.draft_cases(principal, DEFAULT_PROJECT, draft_id, offset=len(cases), limit=PAGE)
        cases.extend(page.items)
        if page.next_cursor is None:
            return cases


async def _out(store: EvalStore, principal: Principal, draft: DatasetDraft) -> DraftOut:
    return draft_out(draft, await dataset_names(store, principal))


def published_cases(cases: list[Case]) -> list[Case]:
    """The cases a published version holds: each record as it reads back, nothing else."""
    return [to_case(from_case(case)) for case in cases]


def summarise(draft: DatasetDraft, cases: list[Case]) -> dict[str, Any]:
    counts = {"active": 0, "archived": 0, "usable": 0, "missing_input": 0, "missing_expected_output": 0}
    for case in cases:
        item = from_case(case)
        if item["status"] == "ARCHIVED":
            counts["archived"] += 1
            continue
        counts["active"] += 1
        if not _prompt(item["input"]):
            counts["missing_input"] += 1
        elif _expected(item["expected_output"]) is None:
            counts["missing_expected_output"] += 1
        else:
            counts["usable"] += 1
    return {
        "public_name": draft.name,
        "record_count": len(cases),
        "sha256": digest([case.model_dump(mode="json") for case in published_cases(cases)]),
        **counts,
    }


@router.get("/dataset-drafts", response_model=list[DraftOut], dependencies=[Depends(require_capability("editDrafts"))])
async def drafts(
    principal: PrincipalDep, store: StoreDep, records: RecordsDep, include_published: bool = True
) -> list[DraftOut]:
    found = [
        d for d in await records.list_drafts(principal, DEFAULT_PROJECT) if include_published or d.state == "editing"
    ]
    found.sort(key=lambda d: (d.updated_at or datetime.min.replace(tzinfo=UTC), d.id), reverse=True)
    names = await dataset_names(store, principal)
    return [draft_out(d, names) for d in found[:200]]


@router.post(
    "/dataset-drafts",
    response_model=DraftOut,
    status_code=201,
    dependencies=[Depends(require_capability("editDrafts"))],
)
async def create(body: DraftCreateIn, principal: PrincipalDep, store: StoreDep, records: RecordsDep) -> DraftOut:
    name, cases, source = body.name, [], None
    if body.from_dataset:
        dataset = await find_published(store, principal, body.from_dataset)
        source = dataset.id
        offset = 0
        while True:
            page = await store.read_cases(principal, DEFAULT_PROJECT, dataset.id, offset=offset, limit=PAGE)
            cases.extend(page.items)
            offset += len(page.items)
            if page.next_cursor is None:
                break
        # Editing keeps the name; a copy of a global dataset takes a name without the reserved prefix.
        name = name or body.from_dataset.removeprefix(GLOBAL_PREFIX)
    if not name:
        raise HTTPException(422, "Dataset name is required")
    draft = await records.create_draft(
        principal,
        DEFAULT_PROJECT,
        DatasetDraft(
            id=str(uuid.uuid4()),
            project_id=DEFAULT_PROJECT,
            name=validate_name(name),
            description=body.description,
            source_dataset_id=source,
            created_by=principal.user_id,
        ),
    )
    if cases:
        draft = await records.put_draft_cases(
            principal, DEFAULT_PROJECT, draft.id, cases, expected_revision=draft.revision
        )
    return await _out(store, principal, draft)


@router.get(
    "/dataset-drafts/{draft_id}", response_model=DraftOut, dependencies=[Depends(require_capability("editDrafts"))]
)
async def detail(draft_id: uuid.UUID, principal: PrincipalDep, store: StoreDep, records: RecordsDep) -> DraftOut:
    return await _out(store, principal, await get_draft(records, principal, draft_id))


@router.patch(
    "/dataset-drafts/{draft_id}", response_model=DraftOut, dependencies=[Depends(require_capability("editDrafts"))]
)
async def patch(
    draft_id: uuid.UUID, body: DraftPatchIn, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> DraftOut:
    await editable(records, principal, draft_id, body.base_revision)
    draft = await records.edit_draft(
        principal,
        DEFAULT_PROJECT,
        str(draft_id),
        expected_revision=body.base_revision,
        name=validate_name(body.name) if body.name is not None else None,
        description=body.description,
    )
    return await _out(store, principal, draft)


@router.delete("/dataset-drafts/{draft_id}", status_code=204, dependencies=[Depends(require_capability("editDrafts"))])
async def remove(draft_id: uuid.UUID, principal: PrincipalDep, records: RecordsDep) -> Response:
    await get_draft(records, principal, draft_id)
    await records.delete_draft(principal, DEFAULT_PROJECT, str(draft_id))
    return Response(status_code=204)


@router.get(
    "/dataset-drafts/{draft_id}/validation",
    response_model=None,
    dependencies=[Depends(require_capability("editDrafts"))],
)
async def validation(draft_id: uuid.UUID, principal: PrincipalDep, records: RecordsDep) -> dict[str, Any]:
    """Preview what publishing would produce, including the hash to pass back to publish."""
    draft = await get_draft(records, principal, draft_id)
    return {
        "draft_id": draft.id,
        "revision": draft.revision,
        **summarise(draft, await all_cases(records, principal, draft.id)),
    }


@router.get(
    "/dataset-drafts/{draft_id}/records", response_model=None, dependencies=[Depends(require_capability("editDrafts"))]
)
async def records(
    draft_id: uuid.UUID,
    principal: PrincipalDep,
    records: RecordsDep,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    q: Annotated[str, Query(max_length=200)] = "",
) -> dict[str, Any]:
    draft = await get_draft(records, principal, draft_id)
    rows = list(enumerate(await all_cases(records, principal, draft.id)))
    needle = q.strip().casefold()
    if needle:
        rows = [
            (ordinal, case)
            for ordinal, case in rows
            if any(
                needle in json.dumps(value, ensure_ascii=False, default=str).casefold()
                for value in (case.id, from_case(case)["input"], from_case(case)["expected_output"])
            )
        ]
    shown = rows[(page - 1) * limit : page * limit]
    return {
        "draft_id": draft.id,
        "revision": draft.revision,
        "page": page,
        "limit": limit,
        "query": q,
        "total": len(rows),
        "has_next": page * limit < len(rows),
        "items": [record_out(case, ordinal, draft.updated_at) for ordinal, case in shown],
    }


async def _write(
    records: RecordStore, principal: Principal, draft: DatasetDraft, items: list[dict[str, Any]]
) -> tuple[DatasetDraft, list[Case]]:
    updated = await records.put_draft_cases(
        principal, DEFAULT_PROJECT, draft.id, [to_case(item) for item in items], expected_revision=draft.revision
    )
    if updated.case_count > MAX_RECORDS:
        raise HTTPException(422, f"Datasets are limited to {MAX_RECORDS:,} records")
    return updated, await all_cases(records, principal, draft.id)


def _record(cases: list[Case], case_id: str, updated: DatasetDraft) -> dict[str, Any]:
    ordinal, case = next((i, c) for i, c in enumerate(cases) if c.id == case_id)
    return record_out(case, ordinal, updated.updated_at)


@router.post(
    "/dataset-drafts/{draft_id}/records",
    status_code=201,
    response_model=None,
    dependencies=[Depends(require_capability("editDrafts"))],
)
async def add(draft_id: uuid.UUID, body: RecordIn, principal: PrincipalDep, records: RecordsDep) -> dict[str, Any]:
    item = _one(body.record)
    item["id"] = item["id"] or _generated_id()
    draft = await editable(records, principal, draft_id, body.base_revision)
    if any(case.id == item["id"] for case in await all_cases(records, principal, draft.id)):
        raise HTTPException(409, "A record with this id already exists; edit it instead")
    updated, cases = await _write(records, principal, draft, [item])
    return {"revision": updated.revision, "record": _record(cases, item["id"], updated)}


@router.patch(
    "/dataset-drafts/{draft_id}/records/{item_id}",
    response_model=None,
    dependencies=[Depends(require_capability("editDrafts"))],
)
async def edit(
    draft_id: uuid.UUID, item_id: str, body: RecordPatchIn, principal: PrincipalDep, records: RecordsDep
) -> dict[str, Any]:
    draft = await editable(records, principal, draft_id, body.base_revision)
    case = next((c for c in await all_cases(records, principal, draft.id) if c.id == item_id), None)
    if case is None:
        raise HTTPException(404, "Record not found")
    if "id" in body.changes:
        raise HTTPException(422, "Record ids cannot be changed; add a new record instead")
    item = _one({**from_case(case), **body.changes})
    updated, cases = await _write(records, principal, draft, [{**item, "id": item_id}])
    return {"revision": updated.revision, "record": _record(cases, item_id, updated)}


async def merge_records(
    records: RecordStore, principal: Principal, draft: DatasetDraft, parsed: ParsedUpload, *, dry_run: bool
) -> dict[str, Any]:
    """Add or replace records by id, as MLflow's ``merge_records`` does by inputs; a dry run only
    counts. Records keep their place; new ones go last."""
    existing = {case.id for case in await all_cases(records, principal, draft.id)}
    updated = sum(item["id"] in existing for item in parsed.items)
    inserted = len(parsed.items) - updated
    if draft.case_count + inserted > MAX_RECORDS:
        raise HTTPException(422, f"Datasets are limited to {MAX_RECORDS:,} records")
    result: dict[str, Any] = {
        "inserted": inserted,
        "updated": updated,
        "generated_ids": parsed.generated_ids,
        "dry_run": dry_run,
    }
    if dry_run:
        return {**result, "revision": draft.revision, "record_count": draft.case_count + inserted}
    written, _cases = await _write(records, principal, draft, parsed.items)
    return {**result, "revision": written.revision, "record_count": written.case_count}


@router.post(
    "/dataset-drafts/{draft_id}/records/merge",
    response_model=None,
    dependencies=[Depends(_limit_body), Depends(require_capability("editDrafts"))],
)
async def merge(draft_id: uuid.UUID, body: MergeIn, principal: PrincipalDep, records: RecordsDep) -> dict[str, Any]:
    """MLflow merge_records: upsert by record id; dry_run reports without writing."""
    parsed = parse_upload(body.format, content=body.content, dataset=body.dataset)
    draft = await editable(records, principal, draft_id, body.base_revision)
    return await merge_records(records, principal, draft, parsed, dry_run=body.dry_run)


@router.post(
    "/dataset-drafts/{draft_id}/records/delete",
    response_model=None,
    dependencies=[Depends(require_capability("editDrafts"))],
)
async def delete(draft_id: uuid.UUID, body: DeleteIn, principal: PrincipalDep, records: RecordsDep) -> dict[str, Any]:
    draft = await editable(records, principal, draft_id, body.base_revision)
    existing = {case.id for case in await all_cases(records, principal, draft.id)}
    removed = await records.delete_draft_cases(
        principal, DEFAULT_PROJECT, draft.id, body.item_ids, expected_revision=draft.revision
    )
    return {
        "deleted": len(existing & set(body.item_ids)),
        "revision": removed.revision,
        "record_count": removed.case_count,
    }


@router.post(
    "/dataset-drafts/{draft_id}/records/from-eval-run",
    response_model=None,
    dependencies=[Depends(require_capability("editDrafts"))],
)
async def from_eval_run(
    draft_id: uuid.UUID, body: FromEvalRunIn, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    """MLflow "add traces to dataset": copy saved eval results into the draft."""
    try:
        run = await find_run(store, records, principal, body.eval_run_id)
    except HTTPException:
        raise HTTPException(404, "Eval run not found") from None
    wanted = set(body.item_result_ids)
    results = [r for r in await run_results(records, principal, run) if result_id(run.id, r.case_id) in wanted]
    if len(results) != len(wanted):
        raise HTTPException(404, "Some eval results were not found in this run")
    items = []
    for result in results:
        shown = item_out(result)
        expected = shown.output if body.expected_from == "actual" else shown.expected
        items.append(
            {
                "id": "run-" + shown.id.hex[:20],
                "input": shown.prompt,
                "expected_output": expected or None,
                "status": "ACTIVE",
                "metadata": {
                    "source": "eval_run",
                    "eval_run_id": run.id,
                    "eval_item_result_id": str(shown.id),
                    "expected_from": body.expected_from,
                },
            }
        )
    draft = await editable(records, principal, draft_id, body.base_revision)
    return await merge_records(records, principal, draft, ParsedUpload(None, None, items, 0), dry_run=False)


def valid_rationale(rationale: str) -> str:
    if not 8 <= len(rationale.strip()) <= 2000:
        raise HTTPException(422, "Record a rationale of 8-2,000 characters")
    return rationale.strip()


@router.post(
    "/dataset-drafts/{draft_id}/publish",
    status_code=202,
    response_model=None,
    dependencies=[Depends(require_capability("publishDatasets"))],
)
async def publish(
    draft_id: uuid.UUID, body: PublishIn, principal: PrincipalDep, store: StoreDep, records: RecordsDep
) -> dict[str, Any]:
    valid_rationale(body.rationale)
    draft = await editable(records, principal, draft_id, body.base_revision)
    cases = await all_cases(records, principal, draft.id)
    if not cases:
        raise HTTPException(422, "Add at least one record before publishing")
    if summarise(draft, cases)["sha256"] != body.expected_sha256:
        raise HTTPException(409, "Draft content changed since validation; validate again")
    await writable_project(store, principal, None)
    # The manifest first, so new runs wait while the version is written and read back.
    to_publish = published_cases(cases)
    manifest = await records.begin_publication(
        principal,
        DEFAULT_PROJECT,
        manifest_for(
            str(derived_id("dataset-operation", draft.id, body.expected_sha256)),
            DEFAULT_PROJECT,
            draft.name,
            to_publish,
            draft_id=draft.id,
            published_by=principal.user_id,
        ),
    )
    dataset = await store.create_dataset(principal, DEFAULT_PROJECT, draft.name, to_publish)
    if not matches(manifest, dataset, await stored_cases(store, principal, DEFAULT_PROJECT, dataset.id)):
        raise HTTPException(409, "The published dataset did not read back as written; publish it again")
    await records.finish_publication(principal, DEFAULT_PROJECT, manifest.id, dataset.id)
    published = await records.publish_draft(
        principal, DEFAULT_PROJECT, draft.id, expected_revision=draft.revision, digest=dataset.digest
    )
    return {"scope": "org", "operation_id": operation_id(published), "state": "published"}


# --- dataset operations -----------------------------------------------------------------------


def operation_out(draft: DatasetDraft) -> dict[str, Any]:
    return {
        "operation_id": operation_id(draft),
        "state": "published",
        "stored_state": "published",
        "error_code": None,
        "record_count": draft.case_count,
        "sha256": draft.published_digest,
        "lease_until": None,
        "created_at": draft.updated_at,
        "updated_at": draft.updated_at,
        "draft_id": draft.id,
        "dataset_name": draft.name,
        "source": "draft",
    }


async def _operations(records: RecordStore, principal: Principal) -> list[DatasetDraft]:
    published = [d for d in await records.list_drafts(principal, DEFAULT_PROJECT) if operation_id(d) is not None]
    published.sort(key=lambda d: (d.updated_at or datetime.min.replace(tzinfo=UTC), d.id), reverse=True)
    return published[:50]


async def _operation(records: RecordStore, principal: Principal, wanted: uuid.UUID) -> DatasetDraft:
    found = next((d for d in await _operations(records, principal) if operation_id(d) == wanted), None)
    if found is None:
        raise HTTPException(404, "Dataset operation not found")
    return found


@router.get("/dataset-operations", response_model=None, dependencies=[Depends(require_capability("publishDatasets"))])
async def operations(principal: PrincipalDep, records: RecordsDep) -> dict[str, Any]:
    return {"items": [operation_out(d) for d in await _operations(records, principal)]}


@router.get(
    "/dataset-operations/{operation_id}",
    response_model=None,
    dependencies=[Depends(require_capability("publishDatasets"))],
)
async def operation(operation_id: uuid.UUID, principal: PrincipalDep, records: RecordsDep) -> dict[str, Any]:
    return operation_out(await _operation(records, principal, operation_id))


@router.post(
    "/dataset-operations/{operation_id}/resume",
    status_code=202,
    response_model=None,
    dependencies=[Depends(require_capability("publishDatasets"))],
)
async def resume(
    operation_id: uuid.UUID, body: ResumeIn, principal: PrincipalDep, records: RecordsDep
) -> dict[str, Any]:
    """Continue a stalled or needs-review operation from its checkpoints."""
    # Publishing finishes in its request, so no operation is ever left to continue.
    await _operation(records, principal, operation_id)
    raise HTTPException(409, "Dataset operation is already finished")


@router.post(
    "/dataset-operations/{operation_id}/abort",
    response_model=None,
    dependencies=[Depends(require_capability("publishDatasets"))],
)
async def abort(operation_id: uuid.UUID, body: AbortIn, principal: PrincipalDep, records: RecordsDep) -> dict[str, Any]:
    await _operation(records, principal, operation_id)
    raise HTTPException(409, "Dataset operation is already finished")


__all__ = ["from_case", "parse_upload", "router", "to_case"]
