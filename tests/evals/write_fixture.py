"""The evals core with an in-process worker, a fake agent and a deterministic judge, for the write
route tests. Nothing calls a model or a network target.

The default project has one published dataset, ``support-faq``: a case the agent answers exactly, a
case it answers wrongly, and a case with no expected text, which a run skips.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from martian.evals.judge_settings import JudgeSettings, MemoryJudgeSettings
from martian.evals.model import Case, Project
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER, Entitlements, Principal, create_app
from martian.evals.store import EvalStore
from martian.evals.worker import (
    AgentExecutor,
    Credentials,
    InProcessWorker,
    Judge,
    JudgeRequest,
    JudgeResult,
    Verdict,
    Work,
)

AGENT = str(uuid.UUID(int=7))
ANSWERS = {"How do I reset my password?": "Use the reset link on the sign-in page."}
CASES = [
    Case(
        id="faq-1",
        inputs={"message": "How do I reset my password?"},
        expectations={"expected_response": "Use the reset link on the sign-in page."},
    ),
    Case(
        id="faq-2",
        inputs={"message": "Can I change my username?"},
        expectations={"expected_response": "Yes, under account settings."},
    ),
    Case(id="faq-3", inputs={"message": "Where is my invoice?"}),
]
JUDGE_MODEL = "fake-judge@1"


async def agent(case: Case) -> str:
    """The agent under test: right about passwords, unsure about everything else."""
    return ANSWERS.get(case.inputs.get("message", ""), "I am not sure.")


async def judge(request: JudgeRequest) -> JudgeResult:
    """A judge that passes an output only when it repeats the expected text. It answers from the
    model the request names, which reports the version ``2026-11``, or from ``JUDGE_MODEL``, which
    reports none."""
    passed = request.expected.casefold() in request.output.casefold()
    return JudgeResult(
        model=request.model or JUDGE_MODEL,
        model_version="2026-11" if request.model else "",
        verdicts={
            s.name: Verdict(passed, "Repeats the expected text." if passed else "Differs.") for s in request.scorers
        },
    )


def request_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Served:
    client: TestClient
    store: EvalStore
    records: SqlRecordStore
    worker: InProcessWorker
    principal: dict[str, Principal] = field(default_factory=dict)
    judge_settings: JudgeSettings | None = None

    def drain(self) -> None:
        """Wait for every run and scoring job the worker was given."""
        assert self.client.portal is not None
        self.client.portal.call(self.worker.drain)

    def call(self, method: str, path: str, status: int, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        assert response.status_code == status, response.text
        return response.json() if response.content else None

    def act_as(self, principal: Principal) -> None:
        self.principal["current"] = principal

    def launch(self, **body: Any) -> dict[str, Any]:
        return self.call("POST", "/v1/evals", 202, json={"agent_id": AGENT, "dataset_name": "support-faq", **body})

    def completed_run(self) -> dict[str, Any]:
        run = self.launch()
        self.drain()
        return self.call("GET", f"/v1/evals/{run['id']}", 200)


async def publish(store: EvalStore, principal: Principal = SINGLE_USER) -> None:
    await store.create_project(principal, Project(id=DEFAULT_PROJECT, name="Default"))
    await store.create_dataset(principal, DEFAULT_PROJECT, "support-faq", CASES)


def serve(
    store: EvalStore,
    records: SqlRecordStore,
    *,
    entitlements: Callable[[Principal], Entitlements] | None = None,
    with_judge: bool = True,
    targets: dict[str, Any] | None = None,
    principal: Principal = SINGLE_USER,
    judge_with: Judge = judge,
    judge_settings: JudgeSettings | None = None,
    credentials: Credentials | None = None,
    executor: AgentExecutor | None = None,
    target_timeout_seconds: float = 2,
) -> tuple[TestClient, Served]:
    """The core with a dataset published for ``principal``, who makes the requests until
    ``act_as`` names another. Use the client as a context manager, so the worker's tasks run
    between requests. The default judge model is kept in memory unless ``judge_settings`` names
    other settings."""
    asyncio.run(publish(store, principal))
    worker = InProcessWorker(
        store,
        records,
        organisation_id=principal.organisation_id,
        targets=targets or {AGENT: agent},
        judge=judge_with if with_judge else None,
        target_timeout_seconds=target_timeout_seconds,
        credentials=credentials,
        executor=executor,
    )
    holder: dict[str, Principal] = {"current": principal}
    options: dict[str, Any] = {} if entitlements is None else {"entitlements": entitlements}
    settings = judge_settings if judge_settings is not None else MemoryJudgeSettings()
    app = create_app(
        store=store,
        records=records,
        principal=lambda: holder["current"],
        worker=worker,
        judge_settings=settings,
        **options,
    )
    client = TestClient(app)
    served = Served(
        client=client, store=store, records=records, worker=worker, principal=holder, judge_settings=settings
    )
    return client, served


class Inert:
    """A worker that accepts work and never runs it, as a queue does before its worker takes it."""

    def __init__(self) -> None:
        self.submitted: list[Work] = []

    @property
    def judges(self) -> bool:
        return True

    async def check_target(self, principal, target):
        return {"kind": target.kind, "agent_id": target.agent_id}

    async def submit(self, work: Work) -> None:
        self.submitted.append(work)
