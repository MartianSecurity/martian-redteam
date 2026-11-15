"""Saved suites and registered judges: a port of ``GET`` and ``POST /suites`` of the server's
``app/api/evals_quality.py`` and ``GET`` and ``POST /judges`` of ``app/api/evals_explorer.py``
(MartianSecurity/MartianBackend).

- A suite here is the console's: the built-in scorers it runs and the policies that gate them. It is
  a ``Suite`` in the record store's default project, and each change is a new version. A version's
  API id is derived from the suite's id and its version.
- A judge is a registered scorer in the store, in the default project or the one asked for: an
  ``llm_judge`` scorer of type ``instructions`` whose ``criteria`` hold the definition and the
  ``operation_id`` (the ``request_id``) that registered it, and whose ``model`` and ``endpoint``
  are the model it runs on. Each registration is a new version.
- The default judge model, for judges that name none, is the edition's ``JudgeSettings``: an
  organisation's on the server, a local setting in the local edition.

Route functions have no docstrings: FastAPI would publish one as the operation's description, and
the contract's operations have none.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from martian.evals.judge_settings import JudgeModel
from martian.evals.model import Policy, ScorerVersion, Suite, suite_digest
from martian.evals.server.dependencies import (
    JudgeSettingsDep,
    PrincipalDep,
    RecordsDep,
    StoreDep,
    require_capability,
)
from martian.evals.server.projection import (
    DEFAULT_PROJECT,
    derived_id,
    policy_out,
    project_key,
    suite_version_id,
    user_out,
)
from martian.evals.server.runs import writable_project
from martian.evals.server.write_schemas import JudgeModelOut, JudgeModelRequest, JudgeVersionRequest, SuiteRequest
from martian.evals.worker.scorers import builtin_scorer

router = APIRouter(prefix="/v1/evals", tags=["evals"])
explorer = APIRouter(prefix="/v1/evals", tags=["evaluation explorer"])

CRITERIA_REVISION = "martian-eval-criteria-v1"
JUDGE_TYPE = "instructions"
JUDGE_FIELDS = ("name", "description", "instructions", "kind", "options", "minimum", "maximum")
MODEL_FIELDS = ("model", "endpoint")
MODEL_POLICY = (
    "Each judge runs on its own model, or on the default judge model when it names none. "
    "The model and version that produced it are recorded on every executed assessment."
)


# --- suites -----------------------------------------------------------------------------------


def suite_out(suite: Suite) -> dict[str, Any]:
    return {
        "id": str(suite_version_id(suite.id, suite.version)),
        "suite_id": suite.id,
        "version": suite.version,
        "name": suite.name,
        "definition": {
            "scorers": sorted(scorer.name for scorer in suite.scorers),
            "guidelines": next(
                (str(s.criteria.get("guidelines", "")) for s in suite.scorers if s.name == "policy"), ""
            ),
            "revision": CRITERIA_REVISION,
            "policies": [policy_out(p) for p in sorted(suite.policies, key=lambda p: p.scorer)],
        },
        "digest": suite.digest,
        "actor_id": user_out(suite.created_by) if suite.created_by else None,
        "created_at": suite.created_at,
    }


@router.get("/suites", response_model=None)
async def suites(
    principal: PrincipalDep,
    records: RecordsDep,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    versions: list[Suite] = []
    for latest in await records.list_suites(principal, DEFAULT_PROJECT):
        for number in range(1, latest.version + 1):
            if found := await records.get_suite(principal, DEFAULT_PROJECT, latest.id, version=number):
                versions.append(found)
    versions.sort(key=lambda s: (s.created_at is not None, s.created_at, s.id, s.version), reverse=True)
    return {
        "items": [suite_out(s) for s in versions[(page - 1) * limit : page * limit]],
        "total": len(versions),
        "page": page,
        "limit": limit,
        "has_more": page * limit < len(versions),
    }


@router.post("/suites", status_code=201, response_model=None, dependencies=[Depends(require_capability("saveSuites"))])
async def save_suite(body: SuiteRequest, principal: PrincipalDep, records: RecordsDep) -> dict[str, Any]:
    suite_id = str(body.suite_id) if body.suite_id else str(derived_id("suite", str(body.request_id)))
    latest = await records.get_suite(principal, DEFAULT_PROJECT, suite_id)
    if body.suite_id is not None and latest is None:
        raise HTTPException(404, "Not found")
    draft = Suite(
        id=suite_id,
        name=body.name.strip(),
        scorers=[builtin_scorer(name, guidelines=body.guidelines) for name in sorted(body.scorers)],
        policies=[
            Policy(scorer=p.name, direction=p.direction, threshold=p.threshold, minimum_coverage=p.minimum_coverage)
            for p in sorted(body.policies, key=lambda p: p.name)
        ],
        digest="0" * 64,
    )
    suite = draft.model_copy(update={"digest": suite_digest(draft)})
    if (
        latest is not None
        and latest.version == body.expected_version + 1
        and latest.digest == suite.digest
        and latest.created_by == principal.user_id
    ):
        return suite_out(latest)  # the same request again
    if (latest.version if latest else 0) != body.expected_version:
        raise HTTPException(409, "Suite changed; reload before creating a version")
    return suite_out(await records.save_suite(principal, DEFAULT_PROJECT, suite))


# --- judges -----------------------------------------------------------------------------------


def is_judge(scorer: ScorerVersion) -> bool:
    return (
        scorer.kind == "llm_judge"
        and scorer.type == JUDGE_TYPE
        and all(field in scorer.criteria for field in JUDGE_FIELDS)
        and scorer.version.isdigit()
    )


def judge_out(scorer: ScorerVersion) -> dict[str, Any]:
    return {
        "name": scorer.name,
        "version": int(scorer.version),
        "native_id": None,
        "operation_id": scorer.criteria.get("operation_id"),
        "definition": {
            **{field: scorer.criteria[field] for field in JUDGE_FIELDS},
            "model": scorer.model,
            "endpoint": scorer.endpoint,
        },
        "created_at_ms": None,
    }


@explorer.get("/judges", response_model=None)
async def judges(
    principal: PrincipalDep,
    store: StoreDep,
    project_id: uuid.UUID | None = None,
    name: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]{0,59}$")] = None,
) -> dict[str, Any]:
    key = project_key(project_id)
    if await store.get_project(principal, key) is None:
        if key != DEFAULT_PROJECT:
            raise HTTPException(404, "Not found")
        found: list[ScorerVersion] = []
    elif name:
        found = await store.scorer_versions(principal, key, name)
    else:
        found = await store.list_scorers(principal, key)
    supported = [s for s in found if is_judge(s)]
    return {
        "items": [judge_out(s) for s in sorted(supported, key=lambda s: (s.name, int(s.version)))],
        "unsupported": len(found) - len(supported),
        "model_policy": MODEL_POLICY,
    }


@explorer.post(
    "/judges", status_code=201, response_model=None, dependencies=[Depends(require_capability("manageJudges"))]
)
async def register_judge(body: JudgeVersionRequest, principal: PrincipalDep, store: StoreDep) -> dict[str, Any]:
    key = await writable_project(store, principal, body.project_id)
    definition = body.model_dump(mode="json", include={*JUDGE_FIELDS, *MODEL_FIELDS})
    versions = await store.scorer_versions(principal, key, body.name)
    supported = [v for v in versions if is_judge(v)]
    if mine := next((v for v in supported if v.criteria.get("operation_id") == str(body.request_id)), None):
        if judge_out(mine)["definition"] != definition:
            raise HTTPException(409, "request_id belongs to another judge version")
        return judge_out(mine)
    latest = max((int(v.version) for v in supported), default=0)
    if len(supported) != len(versions) or latest != body.expected_version:
        raise HTTPException(409, "Judge changed or has unsupported versions; reload before registering")
    registered = await store.register_scorer(
        principal,
        key,
        ScorerVersion(
            name=body.name,
            kind="llm_judge",
            type=JUDGE_TYPE,
            model=body.model,
            endpoint=body.endpoint,
            criteria={
                **{field: definition[field] for field in JUDGE_FIELDS},
                "operation_id": str(body.request_id),
            },
        ),
    )
    return judge_out(registered)


# --- the default judge model ---------------------------------------------------------------------


@explorer.get("/judge-model", response_model=JudgeModelOut)
async def judge_model(principal: PrincipalDep, settings: JudgeSettingsDep) -> JudgeModelOut:
    found = await settings.default_model(principal) if settings is not None else JudgeModel()
    # The core's worker gives each judge the model it names, so the choice is always offered.
    return JudgeModelOut(model=found.model, endpoint=found.endpoint, selectable=True)


@explorer.put("/judge-model", response_model=JudgeModelOut, dependencies=[Depends(require_capability("manageJudges"))])
async def set_judge_model(
    body: JudgeModelRequest, principal: PrincipalDep, settings: JudgeSettingsDep
) -> JudgeModelOut:
    if settings is None:
        raise HTTPException(503, "This installation keeps no default judge model")
    saved = await settings.set_default_model(principal, JudgeModel(model=body.model, endpoint=body.endpoint))
    return JudgeModelOut(model=saved.model, endpoint=saved.endpoint, selectable=True)


__all__ = ["explorer", "is_judge", "router", "suite_out"]
