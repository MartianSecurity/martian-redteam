"""Prompts and their versions: the evals core's own routes, which the server's frozen contract
does not have (MorningHQ/martian-notes#207).

- ``GET /prompts``: every prompt, by name, with its number of versions (``version_count``), its
  latest version without its text (``latest``), and the number of runs that used one of its
  versions (``run_count``).
- ``POST /prompts``: create a prompt (``201``). The same name and description again returns it; the
  same name with another description is ``409``.
- ``GET /prompts/{name}``: the prompt as the list has it, and ``versions``: every version, oldest
  first, each with its text, its commit message, its author and time, and ``runs``, the runs that
  used it, newest first, from every project, each in the run list's shape with the axis and value
  that held the prompt (``prompt``).
- ``POST /prompts/{name}/versions``: add a version (``201``, ``added: true``). Text the latest version
  already has adds nothing and answers ``200`` with that version and ``added: false``.
  ``base_version``, the latest version the change was made from (``0`` for none), is ``409`` when
  the prompt has a newer one meanwhile.

A prompt belongs to the organisation, or to the single local user, not to a project, as in MLflow's
prompt registry. A run used a version when its ``provenance.prompts`` names it
(:mod:`martian.evals.prompts`). The writes need the role that saves suites (``saveSuites``).

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field

from martian.evals.model import MAX_PROMPT_LENGTH, PROMPT_NAME, Prompt, PromptVersion, Run
from martian.evals.prompts import PROVENANCE_KEY, push_prompt
from martian.evals.records import RecordStore
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, StoreDep, require_capability
from martian.evals.server.principal import Principal
from martian.evals.server.projection import run_out, user_out
from martian.evals.server.runs import EPOCH, project_keys, project_runs
from martian.evals.store import EvalStore

router = APIRouter(prefix="/v1/evals", tags=["prompts"])

PromptPath = Annotated[str, Path(pattern=PROMPT_NAME)]


class PromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=PROMPT_NAME)
    description: str = Field(default="", max_length=1000)


class PromptVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)
    message: str = Field(default="", max_length=1000)
    base_version: int | None = Field(default=None, ge=0)


def version_out(version: PromptVersion, *, text: bool = True) -> dict[str, Any]:
    found: dict[str, Any] = {
        "name": version.name,
        "version": version.version,
        "digest": version.digest,
        "message": version.message,
        "created_at": version.created_at,
        "created_by": user_out(version.created_by) if version.created_by else None,
    }
    if text:
        found["text"] = version.text
    return found


def prompt_out(prompt: Prompt, versions: list[PromptVersion], runs: int) -> dict[str, Any]:
    return {
        "name": prompt.name,
        "description": prompt.description,
        "created_at": prompt.created_at,
        "created_by": user_out(prompt.created_by) if prompt.created_by else None,
        "version_count": len(versions),
        "latest": version_out(versions[-1], text=False) if versions else None,
        "run_count": runs,
    }


def used(run: Run) -> list[dict[str, Any]]:
    """The prompt versions a run's provenance names."""
    entries = run.provenance.get(PROVENANCE_KEY)
    if not isinstance(entries, list):
        return []
    return [
        entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("version"), int)
    ]


async def prompted_runs(
    store: EvalStore, records: RecordStore, principal: Principal
) -> list[tuple[Run, dict[str, Any]]]:
    """Every run of every project that names a prompt version, newest first, once for each version
    it names."""
    found: list[tuple[Run, dict[str, Any]]] = []
    for key in await project_keys(store, principal):
        for run in await project_runs(records, principal, key):
            found.extend((run, entry) for entry in used(run))
    return sorted(found, key=lambda pair: pair[0].created_at or EPOCH, reverse=True)


async def _prompt(store: EvalStore, principal: Principal, name: str) -> Prompt:
    prompt = await store.get_prompt(principal, name)
    if prompt is None:
        raise HTTPException(404, "Prompt not found")
    return prompt


@router.get("/prompts", response_model=None)
async def list_prompts(principal: PrincipalDep, store: StoreDep, records: RecordsDep) -> dict[str, Any]:
    runs: dict[str, set[str]] = {}
    for run, entry in await prompted_runs(store, records, principal):
        runs.setdefault(entry["name"], set()).add(run.id)
    items = []
    for prompt in await store.list_prompts(principal):
        versions = await store.prompt_versions(principal, prompt.name)
        items.append(prompt_out(prompt, versions, len(runs.get(prompt.name, ()))))
    return {"items": items}


@router.post("/prompts", status_code=201, response_model=None, dependencies=[Depends(require_capability("saveSuites"))])
async def create_prompt(body: PromptRequest, principal: PrincipalDep, store: StoreDep) -> dict[str, Any]:
    prompt = await store.create_prompt(principal, body.name, description=body.description)
    return prompt_out(prompt, await store.prompt_versions(principal, prompt.name), 0)


@router.get("/prompts/{name}", response_model=None)
async def get_prompt(name: PromptPath, principal: PrincipalDep, store: StoreDep, records: RecordsDep) -> dict[str, Any]:
    prompt = await _prompt(store, principal, name)
    versions = await store.prompt_versions(principal, name)
    runs: dict[int, list[dict[str, Any]]] = {}
    for run, entry in await prompted_runs(store, records, principal):
        if entry["name"] == name:
            shown = run_out(run).model_dump(mode="json")
            runs.setdefault(entry["version"], []).append(
                {**shown, "prompt": {"axis": entry.get("axis"), "value": entry.get("value")}}
            )
    total = len({run["id"] for found in runs.values() for run in found})
    return {
        **prompt_out(prompt, versions, total),
        "versions": [{**version_out(version), "runs": runs.get(version.version, [])} for version in versions],
    }


@router.post(
    "/prompts/{name}/versions",
    status_code=201,
    response_model=None,
    dependencies=[Depends(require_capability("saveSuites"))],
)
async def add_version(
    name: PromptPath, body: PromptVersionRequest, response: Response, principal: PrincipalDep, store: StoreDep
) -> dict[str, Any]:
    await _prompt(store, principal, name)
    versions = await store.prompt_versions(principal, name)
    latest = versions[-1].version if versions else 0
    same = bool(versions) and versions[-1].text == body.text
    if body.base_version is not None and body.base_version != latest and not same:
        raise HTTPException(
            409, f"The prompt has version {latest}, newer than version {body.base_version} this change is based on"
        )
    pushed = await push_prompt(store, principal, name, body.text, message=body.message, create=False)
    if not pushed.added:
        response.status_code = 200
    return {"added": pushed.added, "version": version_out(pushed.version)}


__all__ = ["prompt_out", "prompted_runs", "router", "used", "version_out"]
