"""A :class:`~martian.redteam.server.Launcher` that runs the red team engine in the server's own process.

An edition binds it as ``app.state.martian_redteam_launcher`` to serve ``POST /v1/redteam/runs`` and
``POST /v1/redteam/runs/{run_id}/stop``. ``start`` checks the run file, saves the run as ``running``
and answers at once; the engine runs as a task on the server's event loop and keeps the run in the
record store after each attempt. ``stop`` cancels the task: the run is kept as ``cancelled`` with the
attempts it finished.

A run file's relative ``file`` is read from ``base_dir``. The service is chosen as for the command
line (:func:`~martian.redteam.engine.service.select_service`) from ``environ``, the server's
environment by default, and ``${NAME}`` in the target's address and headers is read from it too.

The local edition also starts a run against one of its local targets
(:mod:`martian.evals.local_targets`), named by ``target_id``. The run file then reaches the target
at the target's own address and names its key as ``${MARTIAN_TARGET_KEY}``, as a run file run from the
command line does; the launcher fills it in from ``targets`` when it builds the target, in the
server's process. Neither the run file nor the stored run holds the key.

Each run keeps the run file it was started with, and the local target it names, in its provenance
under ``launch``, so that :meth:`LocalLauncher.resume` can go on with it after a stop, a budget
limit, a failure or a server that stopped: as ``martian redteam run FILE --resume RUN_ID`` does,
each finished attempt whose fingerprint is unchanged is kept and the rest run again.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator, Mapping
from contextlib import AsyncExitStack, ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from martian.config import ConfigError
from martian.evals.local_targets import KEY_REFERENCE, LocalTargets
from martian.evals.server.principal import Principal
from martian.redteam.engine.budget import BudgetExhausted, Ledger, ResumeError
from martian.redteam.engine.memory import StrategyLibraryError
from martian.redteam.engine.plan import PlanError
from martian.redteam.engine.report import for_report
from martian.redteam.engine.run import Engine
from martian.redteam.engine.runs import (
    SessionHandoverError,
    TargetUnavailable,
    build_service,
    build_target,
    run_canary,
    tool_boundary,
    unsupported,
)
from martian.redteam.engine.service import ServiceSelectionError
from martian.redteam.model import RedteamRun
from martian.redteam.runfile import RunFile
from martian.redteam.server import RunStart
from martian.redteam.server.push import CLIENT_EXECUTED
from martian.redteam.store import RedteamStore, finished
from martian.redteam.verify.canary import planted_run_file

RESUMABLE = ("cancelled", "stopped", "failed")
"""The states a run can be resumed from. A ``running`` run whose task is gone, because the server
that ran it stopped, can be resumed too."""


class LocalLauncher:
    """Starts, stops and resumes red team runs in this process, kept in ``records``. ``targets`` are
    the local edition's targets, which a run can name by id."""

    def __init__(
        self,
        records: Any,
        *,
        base_dir: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        targets: LocalTargets | None = None,
    ) -> None:
        self.store = RedteamStore(records)
        self.base_dir = Path(base_dir) if base_dir is not None else Path.cwd()
        self.environ = environ
        self.targets = targets
        self.tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    def _environ(self, target_id: str | None, run_file: RunFile) -> Mapping[str, str]:
        """Where ``${NAME}`` in the target is read from: the launcher's environment and, for a run
        against a local target, that target's key as ``MARTIAN_TARGET_KEY`` and nothing else by that
        name. The run file must reach the target at the target's own address, so the key goes
        nowhere else."""
        environ = dict(self.environ if self.environ is not None else os.environ)
        if target_id is None:
            return environ
        local = self.targets.get(target_id) if self.targets is not None else None
        if local is None or self.targets is None:
            raise HTTPException(422, "target_id names no local target")
        address = self.targets.model_target(local).url
        if run_file.target.kind != "http" or run_file.target.url != address:
            raise HTTPException(422, f"target.url must be the local target's address, {address}")
        environ.pop(KEY_REFERENCE, None)
        if (key := self.targets.key(target_id)) is not None:
            environ[KEY_REFERENCE] = key
        return environ

    def _orphan(self, run: RedteamRun) -> bool:
        """A run a launcher started that is still ``running`` with no task here: its server stopped."""
        return run.state == "running" and "launch" in run.provenance and (run.project_id, run.id) not in self.tasks

    def _run_file(self, mapping: dict[str, Any]) -> RunFile:
        run_file = RunFile.model_validate(mapping)
        if run_file.target.file is not None and not Path(run_file.target.file).is_absolute():
            target = run_file.target.model_copy(update={"file": str((self.base_dir / run_file.target.file).resolve())})
            run_file = run_file.model_copy(update={"target": target})
        sample = run_file.verification.calibration
        if sample is not None and not Path(sample).is_absolute():
            settings = run_file.verification.model_copy(update={"calibration": str((self.base_dir / sample).resolve())})
            run_file = run_file.model_copy(update={"verification": settings})
        folder = run_file.memory.folder
        if folder is not None and not Path(folder).expanduser().is_absolute():
            memory = run_file.memory.model_copy(update={"folder": str((self.base_dir / folder).resolve())})
            run_file = run_file.model_copy(update={"memory": memory})
        return run_file

    @contextmanager
    def _admission(self, principal: Principal, project_id: str, run_id: str) -> Iterator[tuple[ExitStack, bool]]:
        if (project_id, run_id) in self.tasks:
            raise HTTPException(409, "This red team run already has an active session")
        with ExitStack() as lease:
            try:
                exclusive = lease.enter_context(self.store.run_session(project_id, run_id, principal=principal))
            except ResumeError as error:
                raise HTTPException(409, str(error)) from None
            yield lease, exclusive

    async def start(
        self,
        principal: Principal,
        request: RunStart,
        *,
        target_id: str | None = None,
        earlier: RedteamRun | None = None,
    ) -> RedteamRun:
        """Start a run of ``request.run_file``, against the local target ``target_id`` if given, or
        go on with ``earlier``, a run of the same file (:meth:`resume`)."""
        project_id = earlier.project_id if earlier is not None else request.project_id
        run_id = earlier.id if earlier is not None else request.request_id or f"redteam-{uuid4().hex[:12]}"
        request = request.model_copy(update={"project_id": project_id, "request_id": run_id})
        with self._admission(principal, project_id, run_id) as (lease, exclusive):
            if earlier is not None and await self.store.get(project_id, run_id, principal=principal) != earlier:
                raise HTTPException(409, "The saved run changed before it could resume")
            return await self._launch(principal, request, target_id, lease, earlier, recover_interrupted=exclusive)

    async def _launch(
        self,
        principal: Principal,
        request: RunStart,
        target_id: str | None,
        lease: ExitStack,
        earlier: RedteamRun | None,
        *,
        recover_interrupted: bool,
    ) -> RedteamRun:
        run_file = self._run_file(request.run_file)
        if missing := unsupported(run_file):
            raise HTTPException(422, "; ".join(missing))
        stack = AsyncExitStack()
        try:
            if earlier is not None:
                # Refuse before opening sessions or replacing any part of the stored run.
                Ledger(run_file.budget).restore(earlier)
            service, provenance = build_service(
                run_file, stack, self.environ if self.environ is not None else os.environ
            )
            # A resumed run plants the canary its earlier session planted.
            canary = run_canary(earlier)
            planted = planted_run_file(run_file, canary)
            target = build_target(planted.target, base=self.base_dir, environ=self._environ(target_id, run_file))
            stack.push_async_callback(target.close)
            folder = Path(run_file.output_folder)
            target = await tool_boundary(
                planted, target, stack, folder=folder if folder.is_absolute() else self.base_dir / folder
            )

            # Kept beside the run, after redaction, which would hide a key's ${NAME} reference.
            launch = {"run_file": self._kept_file(request.run_file), "target_id": target_id}

            async def keep(run: RedteamRun) -> None:
                kept = for_report(run, policy=service.redaction_policy)
                kept = kept.model_copy(update={"provenance": {**kept.provenance, "launch": launch}})
                await self.store.save(kept, principal=principal)

            engine = Engine(
                run_file,
                service=service,
                target=target,
                run_id=request.request_id,
                project_id=request.project_id,
                checkpoint=keep,
                provenance=provenance,
                resume=earlier,
                canary=canary,
            )
            key = (engine.project_id, engine.run_id)
            if key in self.tasks or (
                earlier is None and await self.store.get(request.project_id, engine.run_id, principal=principal)
            ):
                raise HTTPException(409, "A red team run already has this id")
            first = engine.snapshot("running")
            if earlier is None:
                await keep(first)
            else:
                await self.store.claim_resume(earlier, principal=principal, recover_interrupted=recover_interrupted)
        except (BudgetExhausted, ResumeError) as error:
            await stack.aclose()
            raise HTTPException(409, str(error)) from None
        except (
            ServiceSelectionError,
            TargetUnavailable,
            PlanError,
            ConfigError,
            SessionHandoverError,
            ConnectionError,
            StrategyLibraryError,
        ) as error:
            await stack.aclose()
            raise HTTPException(422, str(error)) from None
        except (FileNotFoundError, ImportError, AttributeError):
            await stack.aclose()
            raise HTTPException(422, "Could not load the target: check its file and function") from None
        except BaseException:
            await stack.aclose()
            raise

        async def go() -> None:
            async with stack:
                await engine.run()

        task = asyncio.create_task(go())
        self.tasks[key] = task
        active_lease = lease.pop_all()

        def release(_: asyncio.Task[None]) -> None:
            active_lease.close()
            self.tasks.pop(key, None)

        task.add_done_callback(release)
        return first

    def _kept_file(self, mapping: dict[str, Any]) -> dict[str, Any]:
        """The run file as sent, with a relative ``file`` made absolute, so a resume reads the same."""
        target = mapping.get("target")
        if isinstance(target, dict) and isinstance(target.get("file"), str) and not Path(target["file"]).is_absolute():
            return {**mapping, "target": {**target, "file": str((self.base_dir / target["file"]).resolve())}}
        return mapping

    async def stop(self, principal: Principal, project_id: str, run_id: str) -> RedteamRun:
        key = (project_id, run_id)
        task = self.tasks.get(key)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with self._admission(principal, project_id, run_id):
            run = await self.store.get(project_id, run_id, principal=principal)
            if run is None:
                raise HTTPException(404, "Red team run not found")
            if self._orphan(run):
                run = finished(
                    run.model_copy(
                        update={
                            "state": "cancelled",
                            "budget": {**run.budget, "history_incomplete": True},
                            "limitations": [
                                *run.limitations,
                                "The process stopped before a final budget checkpoint. "
                                "Recorded spend is a lower bound.",
                            ],
                        }
                    )
                )
                await self.store.save(run, principal=principal)
            return run

    async def resume(self, principal: Principal, project_id: str, run_id: str) -> RedteamRun:
        """Go on with a stopped, cancelled or failed run with the run file and local target it was
        started with. Each finished attempt whose fingerprint is unchanged is kept, and the rest run
        again; what the earlier session spent still counts against the budget."""
        with self._admission(principal, project_id, run_id) as (lease, exclusive):
            earlier = await self.store.get(project_id, run_id, principal=principal)
            if earlier is None:
                raise HTTPException(404, "Red team run not found")
            launch = earlier.provenance.get("launch")
            if (
                earlier.provenance.get("execution") == CLIENT_EXECUTED
                or not isinstance(launch, dict)
                or not isinstance(launch.get("run_file"), dict)
            ):
                raise HTTPException(
                    409,
                    "This run was not started here, so its run file is not kept: "
                    f"resume it with martian redteam run FILE --resume {run_id}",
                )
            if earlier.state not in RESUMABLE and not self._orphan(earlier):
                raise HTTPException(
                    409, f"Only a stopped, cancelled or failed run can be resumed; this one is {earlier.state}"
                )
            request = RunStart(project_id=project_id, run_file=launch["run_file"], request_id=run_id)
            return await self._launch(
                principal, request, launch.get("target_id"), lease, earlier, recover_interrupted=exclusive
            )

    async def close(self) -> None:
        """Cancel every run still going."""
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


__all__ = ["LocalLauncher"]
