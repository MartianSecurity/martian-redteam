"""A :class:`~martian.redteam.server.Launcher` that runs the red team engine in the server's own process.

An edition binds it as ``app.state.martian_redteam_launcher`` to serve ``POST /v1/redteam/runs`` and
``POST /v1/redteam/runs/{run_id}/stop``. ``start`` checks the run file, saves the run as ``running``
and answers at once; the engine runs as a task on the server's event loop and keeps the run in the
record store after each attempt. ``stop`` cancels the task: the run is kept as ``cancelled`` with the
attempts it finished.

A run file's relative ``file`` is read from ``base_dir``. The service is chosen as for the command
line (:func:`~martian.redteam.engine.service.select_service`) from ``environ``, the server's
environment by default.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from martian.config import ConfigError
from martian.evals.server.principal import Principal
from martian.redteam.engine.plan import PlanError
from martian.redteam.engine.report import for_report
from martian.redteam.engine.run import Engine
from martian.redteam.engine.runs import (
    SessionHandoverError,
    TargetUnavailable,
    build_service,
    build_target,
    tool_boundary,
    unsupported,
)
from martian.redteam.engine.service import ServiceSelectionError
from martian.redteam.model import RedteamRun
from martian.redteam.runfile import RunFile
from martian.redteam.server import RunStart
from martian.redteam.store import RedteamStore
from martian.redteam.verify.canary import new_canary, planted_run_file


class LocalLauncher:
    """Starts and stops red team runs in this process, kept in ``records``."""

    def __init__(
        self, records: Any, *, base_dir: str | Path | None = None, environ: Mapping[str, str] | None = None
    ) -> None:
        self.store = RedteamStore(records)
        self.base_dir = Path(base_dir) if base_dir is not None else Path.cwd()
        self.environ = environ
        self.tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    def _run_file(self, mapping: dict[str, Any]) -> RunFile:
        run_file = RunFile.model_validate(mapping)
        if run_file.target.file is not None and not Path(run_file.target.file).is_absolute():
            target = run_file.target.model_copy(update={"file": str((self.base_dir / run_file.target.file).resolve())})
            run_file = run_file.model_copy(update={"target": target})
        return run_file

    async def start(self, principal: Principal, request: RunStart) -> RedteamRun:
        run_file = self._run_file(request.run_file)
        if missing := unsupported(run_file):
            raise HTTPException(422, "; ".join(missing))
        stack = AsyncExitStack()
        try:
            service, provenance = build_service(
                run_file, stack, self.environ if self.environ is not None else os.environ
            )
            canary = new_canary()
            planted = planted_run_file(run_file, canary)
            target = build_target(planted.target, base=self.base_dir)
            stack.push_async_callback(target.close)
            folder = Path(run_file.output_folder)
            target = await tool_boundary(
                planted, target, stack, folder=folder if folder.is_absolute() else self.base_dir / folder
            )

            async def keep(run: RedteamRun) -> None:
                await self.store.save(for_report(run, policy=service.redaction_policy), principal=principal)

            engine = Engine(
                run_file,
                service=service,
                target=target,
                run_id=request.request_id,
                project_id=request.project_id,
                checkpoint=keep,
                provenance=provenance,
                canary=canary,
            )
        except (
            ServiceSelectionError,
            TargetUnavailable,
            PlanError,
            ConfigError,
            SessionHandoverError,
            ConnectionError,
        ) as error:
            await stack.aclose()
            raise HTTPException(422, str(error)) from None
        except (FileNotFoundError, ImportError, AttributeError):
            await stack.aclose()
            raise HTTPException(422, "Could not load the target: check its file and function") from None
        if (request.project_id, engine.run_id) in self.tasks or await self.store.get(
            request.project_id, engine.run_id, principal=principal
        ):
            await stack.aclose()
            raise HTTPException(409, "A red team run already has this id")
        first = engine.snapshot("running")
        await keep(first)

        async def go() -> None:
            async with stack:
                await engine.run()

        task = asyncio.create_task(go())
        key = (request.project_id, engine.run_id)
        self.tasks[key] = task
        task.add_done_callback(lambda _: self.tasks.pop(key, None))
        return first

    async def stop(self, principal: Principal, project_id: str, run_id: str) -> RedteamRun:
        task = self.tasks.get((project_id, run_id))
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        run = await self.store.get(project_id, run_id, principal=principal)
        if run is None:
            raise HTTPException(404, "Red team run not found")
        return run

    async def close(self) -> None:
        """Cancel every run still going."""
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


__all__ = ["LocalLauncher"]
