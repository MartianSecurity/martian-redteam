"""What each run's process opens in the process worker tests, and the same work on threads.

:func:`services` is the ``ProcessWorker`` setup: the store and the record store in a folder the
test shares between processes, an agent instrumented with MLflow tracing, and the write route
tests' deterministic judge. It points MLflow tracing at the store's own database with workspaces
on and selects the project's experiment, as any MLflow evaluation or traced agent in a run's
process would: what it selects lands in the process's workspace. Nothing calls a model.

The agent waits, at its first case, until every organisation's run has reached its first case, so
the runs are in flight at once.

Run as ``python -m tests.evals.process_fixture FOLDER WORK...``, this module runs each piece of
work (JSON) on a thread of its own in this one process, each thread fixing its organisation's
workspace as a run's process has it from its start: the threaded worker that one process per run
replaces.

The target tests' setups are here too. :func:`http_services` gives a run's process only the
stores and the credentials of HTTP targets, read from ``target-keys.json`` in the folder as the
local edition keeps them; the worker sends the HTTP targets themselves with the work.
:func:`agent_services` adds :class:`FakeMartianAgents`, a stand-in for the Martian server's
martian-agents path, which appends each call it makes to ``chats.jsonl`` in the folder.
:func:`scorer_services` installs the custom Python scorers of the custom scorer tests, and
:func:`hooked_services` the case hooks and the installed check of the case hook tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import mlflow

from martian.evals.model import Case, Run
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, Principal, organisation_principal
from martian.evals.server.projection import prompt_text
from martian.evals.store import StoreError
from martian.evals.store.mlflow_local import MlflowStore, workspace_name
from martian.evals.worker import CaseContext, Credentials, Work, WorkerServices, load_work
from martian.evals.worker.process import WORKSPACE_ENV, _run_work
from tests.evals.write_fixture import AGENT, ANSWERS, judge

ORGANISATIONS = ("org-a", "org-b")
ARRIVAL_SECONDS = 30
HTTP_TARGET = str(uuid.UUID(int=8))
REGISTERED_AGENT = str(uuid.UUID(int=9))
KEY_NAME = "TARGET_KEY"
LEASE_PREFIX = "fake-target-lease-"


def member(organisation: str) -> Principal:
    return organisation_principal(f"member-of-{organisation}", organisation, frozenset({"member"}))


def answer(message: str) -> str:
    return f"The answer to {message}."


def cases(organisation: str) -> list[Case]:
    """Three cases whose every prompt names the organisation, so each trace says whose it is."""
    return [
        Case(
            id=f"{organisation}-{number}",
            inputs={"message": f"{organisation} question {number}", "organisation": organisation},
            expectations={"expected_response": answer(f"{organisation} question {number}")},
        )
        for number in range(3)
    ]


def open_stores(folder: Path) -> tuple[MlflowStore, SqlRecordStore]:
    return MlflowStore(f"sqlite:///{folder / 'mlflow.db'}", folder / "artifacts"), SqlRecordStore(
        f"sqlite:///{folder / 'martian.db'}"
    )


async def _all_arrived(folder: Path, organisation: str) -> None:
    (folder / f"arrived-{organisation}").touch()
    deadline = time.monotonic() + ARRIVAL_SECONDS
    while not all((folder / f"arrived-{name}").exists() for name in ORGANISATIONS):
        if time.monotonic() > deadline:
            raise TimeoutError("the other organisation's run never reached its first case")
        await asyncio.sleep(0.05)


def services(folder: str) -> WorkerServices:
    home = Path(folder)
    store, records = open_stores(home)
    os.environ["MLFLOW_ENABLE_WORKSPACES"] = "true"
    mlflow.set_tracking_uri(f"sqlite:///{home / 'mlflow.db'}")
    mlflow.set_experiment(DEFAULT_PROJECT)

    @mlflow.trace(name="agent")
    def reply(message: str) -> str:
        return answer(message)

    async def agent(case: Case) -> str:
        await _all_arrived(home, str(case.inputs["organisation"]))
        return reply(str(case.inputs["message"]))

    return WorkerServices(store, records, targets={AGENT: agent}, judge=judge, target_timeout_seconds=ARRIVAL_SECONDS)


def broken(folder: str) -> WorkerServices:
    """A setup that fails in the run's process before the work starts."""
    raise RuntimeError("the run's process could not open its stores")


def stored_keys(folder: Path) -> Credentials:
    """Credentials from ``target-keys.json`` in ``folder``: each target's key by its id, as
    ``${TARGET_KEY}``. The file is read when the worker asks, as ``LocalTargets.key`` reads it."""

    def credentials(principal: Principal, target_id: str, name: str, /) -> str | None:
        keys: dict[str, str] = json.loads((folder / "target-keys.json").read_text(encoding="utf-8"))["keys"]
        return keys.get(target_id) if name == KEY_NAME else None

    return credentials


def http_services(folder: str) -> WorkerServices:
    """The stores and the HTTP targets' credentials: no function, no judge, no martian-agents."""
    home = Path(folder)
    store, records = open_stores(home)
    return WorkerServices(store, records, credentials=stored_keys(home), target_timeout_seconds=ARRIVAL_SECONDS)


class FakeMartianAgents:
    """Registered agents through a stand-in for the Martian server's martian-agents path.

    Admission pins the agent's approved connection at its ``revision``, as ``select_eval_target``
    does on the server. Each call carries a target lease minted for that run and case, which
    martian-agents would exchange for the agent's credential; it is appended, with the rest of the
    chat, to ``chats.jsonl`` and never returned. The agent answers as the write route tests' agent
    does."""

    def __init__(self, folder: Path, agents: tuple[str, ...] = (REGISTERED_AGENT,), revision: int = 1) -> None:
        self._chats = folder / "chats.jsonl"
        self._agents = agents
        self._revision = revision

    async def check(self, principal: Principal, agent_id: str) -> dict[str, Any]:
        if agent_id not in self._agents:
            raise StoreError("not_found", "Agent not found")
        return {
            "kind": "dynamic",
            "target_id": f"agent-{agent_id}",
            "connection_id": "connection-1",
            "revision": self._revision,
        }

    async def call(self, principal: Principal, run: Run, case: Case, *, timeout_seconds: float) -> Any:
        message = prompt_text(case.inputs)
        chat = {
            "organisation": principal.organisation_id,
            "target_id": run.provenance["target"]["target_id"],
            "conversation_id": f"eval:{run.id}:{case.id}",
            "message": message,
            "target_lease": f"{LEASE_PREFIX}{uuid.uuid4().hex}",
            "timeout_seconds": timeout_seconds,
        }
        with self._chats.open("a", encoding="utf-8") as file:
            file.write(json.dumps(chat) + "\n")
        return ANSWERS.get(message, "I am not sure.")

    def chats(self) -> list[dict[str, Any]]:
        if not self._chats.exists():
            return []
        return [json.loads(line) for line in self._chats.read_text(encoding="utf-8").splitlines()]


def agent_services(folder: str) -> WorkerServices:
    """:func:`http_services` with registered agents through :class:`FakeMartianAgents`."""
    home = Path(folder)
    store, records = open_stores(home)
    return WorkerServices(
        store,
        records,
        credentials=stored_keys(home),
        executor=FakeMartianAgents(home),
        target_timeout_seconds=ARRIVAL_SECONDS,
    )


SCORERS = ("names_organisation", "words", "broken")
"""The custom scorers :func:`scorer_services` installs, from ``scorers.py`` in the folder."""


def scorer_services(folder: str) -> WorkerServices:
    """The stores, an agent that answers every case, and the custom scorers in ``scorers.py`` in
    the folder. The scorer file is imported here, inside the setup, in the work's own process: the
    process that serves the API imports this module to check the setup's import path, and must
    never import the scorer file."""
    from martian.evals._custom import function_of, load_module, module_name

    home = Path(folder)
    store, records = open_stores(home)
    file = home / "scorers.py"
    loaded = load_module(file, what="scorer", module=module_name(file))
    scorers = {name: function_of(loaded, name, what="scorer") for name in SCORERS}

    async def agent(case: Case) -> str:
        return answer(str(case.inputs["message"]))

    return WorkerServices(store, records, targets={AGENT: agent}, scorers=scorers)


HOOK_LOG = "hooks.jsonl"
"""Where :func:`hooked_services`'s hooks write each step, in the folder."""


def hooked_services(folder: str) -> WorkerServices:
    """The stores, an agent that ends each answer with the token its case's setup gave it, a hook
    that writes each setup and teardown to ``hooks.jsonl`` in the folder, with the process it ran
    in, and the check ``carries_token``, which reads the token from the case's state."""
    home = Path(folder)
    store, records = open_stores(home)

    def log(*step: Any) -> None:
        with (home / HOOK_LOG).open("a", encoding="utf-8") as steps:
            steps.write(json.dumps([*step, os.getpid()]) + "\n")

    class Tokens:
        async def setup(self, context: CaseContext) -> None:
            token = f"token-{context.case.id}"
            context.state.target_inputs["token"] = token
            context.state.values["token"] = token
            log("setup", context.case.id)

        async def teardown(self, context: CaseContext, error: str) -> None:
            log("teardown", context.case.id, error)

    async def agent(case: Case) -> str:
        return f"{answer(str(case.inputs['message']))} {case.inputs['token']}"

    def carries_token(outputs: str, state: Any) -> tuple[bool, str]:
        return outputs.endswith(state.values["token"]), "the reply ends with the case's token"

    return WorkerServices(
        store, records, targets={AGENT: agent}, hooks=(Tokens(),), checks={"carries_token": carries_token}
    )


def run_on_threads(folder: str, works: list[Work]) -> None:
    """Each piece of work on a thread of its own in this process, as a threaded worker would run it.
    The setups take turns (opening a record store migrates it, which is not thread-safe); the work
    itself runs at once."""
    opening = threading.Lock()

    def one(work: Work) -> None:
        with opening:
            os.environ[WORKSPACE_ENV] = workspace_name(work.principal)
            opened = services(folder)
        asyncio.run(_run_work(opened, work))

    threads = [threading.Thread(target=one, args=(work,)) for work in works]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    run_on_threads(sys.argv[1], [load_work(json.loads(argument)) for argument in sys.argv[2:]])
