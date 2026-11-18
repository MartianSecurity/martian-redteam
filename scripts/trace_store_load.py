"""Load test the traces of the in-process MLflow store: write latency, search latency and database
growth as the number of traces, the write rate and the size of a trace grow.

    uv run --extra postgres python scripts/trace_store_load.py \\
        --database-url postgresql+psycopg://user:password@127.0.0.1:5432/load \\
        --sizes 1000,10000,50000 --rates 10,25,50,100 --shapes 0x200,5x1000,20x2000 \\
        --output trace-load.json

The traces are synthetic and no model is called. They go through ``MlflowStore``, the store of
the local edition and of on-premises installs, into one project of one organisation, in a
Postgres schema of their own that is dropped at the end unless ``--keep-schema`` is given. A
``sqlite:///`` URL works too. Three phases, chosen with ``--phases``:

- ``growth``: write traces with ``--writers`` concurrent writers until the project holds each of
  ``--sizes`` traces. After each size, time the searches the console makes (``--searches`` times
  each), and MLflow's own first page and full-text filter beside them, and measure the database.
- ``rate``: offer traces at each of ``--rates`` traces a second for ``--rate-seconds``, with at
  most ``--in-flight`` writes waiting or running; a trace offered while that many are is shed
  rather than queued. Latency counts from when a trace was due, so it includes waiting.
- ``size``: write ``--shape-traces`` traces of each of ``--shapes`` (spans x payload characters)
  with one writer, and record write and read latency and the bytes each trace adds.

A step stops when it reaches ``--max-step-seconds`` and is marked ``stopped_early``; a search
whose first run takes longer than ``--slow-search-seconds`` is not repeated. One Python process
writes only as fast as its own CPU allows, so ``--processes`` shares the growth and rate phases'
writes between that many writer processes; searches always run in this one. Results go to
``--output`` as JSON, and a Markdown summary to stdout. Not part of the test suite:
``tests/evals/test_trace_store_load.py`` runs it on a few traces on SQLite.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import os
import platform
import random
import re
import resource
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT = "load"
TURNS = 5
"""Traces in each synthetic session."""
USERS = 1000
ROUTES = 20
INTERVAL_MS = 1000
"""Synthetic traces are one second of traffic apart: trace ``i`` was requested at ``BASE + i`` seconds."""
BASE = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW = 1000
"""The ``window`` search asks for traces since the newest ``WINDOW`` traces began."""
SEARCHES = (
    "page",
    "mlflow_page",
    "last_page",
    "session",
    "user",
    "tag",
    "window",
    "text",
    "text_all",
    "mlflow_text",
    "sessions",
    "get",
)
_VOCABULARY = (
    "the order was refunded after the customer asked about delivery and the agent checked the account "
    "balance then called the shipping tool with a tracking number and summarised the policy for returns "
    "within thirty days including damaged items missing parts late parcels gift cards store credit "
    "warranty claims invoices receipts payment methods subscription renewal cancellation upgrade "
    "downgrade address change password reset login error retry escalation human handoff"
)
WORDS = _VOCABULARY.split()
_SHAPE = re.compile(r"^(\d+)x(\d+)$")


def _integers(text: str) -> list[int]:
    try:
        values = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected comma-separated whole numbers, got {text!r}") from None
    if any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError("every value must be greater than 0")
    return values


def _sizes(text: str) -> list[int]:
    values = _integers(text)
    if values != sorted(set(values)):
        raise argparse.ArgumentTypeError("--sizes must increase: each is the project's trace count after a step")
    return values


def _shapes(text: str) -> list[tuple[int, int]]:
    shapes = []
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        found = _SHAPE.match(part)
        if found is None or int(found.group(2)) < 1:
            raise argparse.ArgumentTypeError(f"a shape is <spans>x<payload characters>, such as 5x1000; got {part!r}")
        shapes.append((int(found.group(1)), int(found.group(2))))
    return shapes


def _phases(text: str) -> list[str]:
    phases = [p.strip() for p in text.split(",") if p.strip()]
    unknown = sorted(set(phases) - {"growth", "rate", "size"})
    if unknown or not phases:
        raise argparse.ArgumentTypeError("--phases is a comma-separated list of growth, rate and size")
    return phases


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("MARTIAN_LOAD_DATABASE_URL"),
        help="SQLAlchemy URL of the database; defaults to MARTIAN_LOAD_DATABASE_URL",
    )
    parser.add_argument("--schema", help="Postgres schema for MLflow's tables (default: a new load_<random>)")
    parser.add_argument("--keep-schema", action="store_true", help="leave the schema and its traces in place")
    parser.add_argument("--artifact-root", type=Path, help="artifact folder (default: a temporary one, removed)")
    parser.add_argument("--phases", type=_phases, default=["growth", "rate", "size"])
    parser.add_argument("--sizes", type=_sizes, default=[1000, 10000], help="trace counts after each growth step")
    parser.add_argument("--processes", type=int, default=1, help="writer processes in the growth and rate phases")
    parser.add_argument("--writers", type=int, default=4, help="concurrent writers in each writer process")
    parser.add_argument("--spans", type=int, default=5, help="spans per trace in the growth and rate phases")
    parser.add_argument(
        "--payload", type=int, default=1000, help="characters in a request, a response and each span's input and output"
    )
    parser.add_argument("--searches", type=int, default=3, help="runs of each search after a growth step")
    parser.add_argument("--slow-search-seconds", type=float, default=30.0)
    parser.add_argument("--rates", type=_integers, default=[10, 25, 50], help="offered traces a second")
    parser.add_argument("--rate-seconds", type=float, default=30.0, help="how long each rate is offered")
    parser.add_argument("--in-flight", type=int, default=32, help="most writes waiting or running in the rate phase")
    parser.add_argument("--shapes", type=_shapes, default=[(0, 200), (5, 1000), (20, 2000)])
    parser.add_argument("--shape-traces", type=int, default=200, help="traces of each shape")
    parser.add_argument("--max-step-seconds", type=float, default=600.0)
    parser.add_argument(
        "--database-cpu-stat",
        type=Path,
        help="the cgroup v2 cpu.stat of the database's container, to record the database's CPU time per trace",
    )
    parser.add_argument("--note", default="", help="free text kept with the results, such as the database's limits")
    parser.add_argument("--output", type=Path, help="write the results here as JSON")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("give --database-url or set MARTIAN_LOAD_DATABASE_URL")
    if not args.database_url.startswith(("postgresql", "sqlite:")):
        parser.error("--database-url must be a postgresql or sqlite SQLAlchemy URL")
    for name in ("processes", "writers", "spans", "payload", "searches", "in_flight", "shape_traces"):
        if getattr(args, name) < (0 if name == "spans" else 1):
            parser.error(f"--{name.replace('_', '-')} is too small")
    if args.rate_seconds <= 0 or args.max_step_seconds <= 0 or args.slow_search_seconds <= 0:
        parser.error("durations must be greater than 0")
    return args


# --- synthetic traces ---------------------------------------------------------------------------


def _text(rng: random.Random, length: int) -> str:
    words: list[str] = []
    size = 0
    while size < length:
        word = rng.choice(WORDS)
        words.append(word)
        size += len(word) + 1
    return " ".join(words)[:length]


def trace_id(index: int) -> str:
    """Trace ``index``'s id, in MLflow's own form."""
    return f"tr-{index:032x}"


def request_time(index: int) -> datetime:
    return BASE + timedelta(milliseconds=index * INTERVAL_MS)


def synthetic_trace(index: int, *, spans: int, payload: int) -> Any:
    """Trace ``index``: one turn of session ``index // TURNS`` by one of ``USERS`` users on one of
    ``ROUTES`` routes, with ``spans`` spans under one root, and every 50th an error. Its request
    starts with ``needle-<index> `` so a text search can find it alone."""
    from martian.evals.model import Span, Trace

    rng = random.Random(index)
    when = request_time(index)
    start = when.timestamp() * 1000
    latency = rng.uniform(200, 3000)
    root = f"{index:08x}{0:08x}"
    needle = f"needle-{index} "
    return Trace(
        id=trace_id(index),
        project_id=PROJECT,
        name="chat",
        state="error" if index % 50 == 0 else "ok",
        request_time=when,
        latency_ms=latency,
        request=needle + _text(rng, max(payload - len(needle), 0)),
        response=_text(rng, payload),
        session_id=f"session-{index // TURNS}",
        user_id=f"user-{index % USERS}",
        model="load-model",
        tokens=rng.randint(50, 2000),
        cost=round(rng.uniform(0, 0.05), 6),
        tags={"route": f"route-{index % ROUTES}", "env": "load"},
        spans=[
            Span(
                id=f"{index:08x}{step:08x}",
                parent_id=None if step == 0 else root,
                name="agent" if step == 0 else f"step-{step}",
                kind="AGENT" if step == 0 else rng.choice(("LLM", "TOOL", "RETRIEVER")),
                status="ok",
                start_ms=start + step,
                end_ms=start + latency,
                inputs={"text": _text(rng, payload)},
                outputs={"text": _text(rng, payload)},
                attributes={"step": step},
            )
            for step in range(spans)
        ],
    )


# --- measuring -----------------------------------------------------------------------------------


def latency_summary(values_ms: list[float]) -> dict[str, float | int | None]:
    """Count, median, 95th and 99th percentile and maximum, in milliseconds."""
    if not values_ms:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values_ms)

    def at(share: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))], 2)

    return {"n": len(ordered), "p50": at(0.5), "p95": at(0.95), "p99": at(0.99), "max": round(ordered[-1], 2)}


def cpu_seconds(cpu_stat: Path | None) -> float | None:
    """The CPU time a cgroup has used, from its ``cpu.stat``."""
    if cpu_stat is None:
        return None
    for line in cpu_stat.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(" ")
        if key == "usage_usec":
            return int(value) / 1e6
    raise ValueError(f"{cpu_stat} has no usage_usec")


def _cpu_ms_per_trace(before: float | None, after: float | None, written: int) -> float | None:
    if before is None or after is None or not written:
        return None
    return round((after - before) * 1000 / written, 3)


def peak_rss_mb() -> float:
    """The most memory this process has held so far."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


class Database:
    """Size and version of the database the store writes to."""

    def __init__(self, url: str, schema: str) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        self.url = url
        self.schema = schema
        self.postgres = url.startswith("postgresql")
        self.engine = create_engine(url, poolclass=NullPool)

    def size(self) -> dict[str, Any]:
        from sqlalchemy import text

        if not self.postgres:
            path = Path(self.engine.url.database or "")
            files = [path, path.with_name(path.name + "-wal")]
            return {"total_bytes": sum(f.stat().st_size for f in files if f.exists()), "tables": {}}
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :schema AND c.relkind = 'r' ORDER BY 2 DESC"
                ),
                {"schema": self.schema},
            ).all()
        tables = {name: int(size) for name, size in rows}
        return {"total_bytes": sum(tables.values()), "tables": {k: v for k, v in tables.items() if v > 65536}}

    def server(self) -> dict[str, Any]:
        from sqlalchemy import text

        with self.engine.connect() as connection:
            if not self.postgres:
                return {"server": "sqlite " + str(connection.execute(text("SELECT sqlite_version()")).scalar())}
            found: dict[str, Any] = {"server": str(connection.execute(text("SHOW server_version")).scalar())}
            for setting in ("shared_buffers", "work_mem", "max_connections", "effective_cache_size"):
                found[setting] = str(connection.execute(text(f"SHOW {setting}")).scalar())
            return found

    def drop_schema(self) -> None:
        from sqlalchemy import text

        if self.postgres:
            with self.engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
        self.engine.dispose()


# --- writing -------------------------------------------------------------------------------------


def _principal() -> Any:
    from martian.evals.server.principal import organisation_principal

    return organisation_principal("load-user", "load-organisation", frozenset({"admin"}))


class Writer:
    """Writes synthetic traces through one store, in this process or in a writer process. Each
    method returns raw results (``latencies`` in ms, the ``written`` indexes, ``failed``, ``shed``,
    ``errors``, ``seconds``, ``stopped_early``) for ``_merge``."""

    def __init__(self, store: Any, max_step_seconds: float) -> None:
        self.store = store
        self.principal = _principal()
        self.max_step_seconds = max_step_seconds

    async def _one(self, index: int, spans: int, payload: int, errors: dict[str, int]) -> bool:
        trace = synthetic_trace(index, spans=spans, payload=payload)
        try:
            await self.store.record_trace(self.principal, trace)
        except Exception as error:  # a failed write is a measurement, not the end of the run
            first = str(error).splitlines()[0][:160] if str(error) else ""
            errors[f"{type(error).__name__}: {first}"] = errors.get(f"{type(error).__name__}: {first}", 0) + 1
            return False
        return True

    async def write(self, indices: list[int], writers: int, spans: int, payload: int) -> dict[str, Any]:
        """Write these traces with ``writers`` concurrent writers, or as many as fit in a step."""
        latencies: list[float] = []
        written: list[int] = []
        errors: dict[str, int] = {}
        failed = 0
        stopped = False
        queue = iter(indices)
        deadline = time.monotonic() + self.max_step_seconds

        async def writer() -> None:
            nonlocal failed, stopped
            for index in queue:
                if time.monotonic() >= deadline:
                    stopped = True
                    return
                began = time.perf_counter()
                if await self._one(index, spans, payload, errors):
                    latencies.append((time.perf_counter() - began) * 1000)
                    written.append(index)
                else:
                    failed += 1

        began = time.perf_counter()
        await asyncio.gather(*(writer() for _ in range(writers)))
        return {
            "latencies": latencies,
            "written": written,
            "failed": failed,
            "shed": 0,
            "errors": errors,
            "seconds": time.perf_counter() - began,
            "stopped_early": stopped,
        }

    async def offer(self, indices: list[int], rate: float, in_flight: int, spans: int, payload: int) -> dict[str, Any]:
        """Offer these traces at ``rate`` a second, shedding one that comes due while ``in_flight``
        writes are running. Latency counts from when a trace was due."""
        latencies: list[float] = []
        written: list[int] = []
        errors: dict[str, int] = {}
        running = shed = failed = 0
        tasks: set[asyncio.Task[None]] = set()

        async def one(index: int, due: float) -> None:
            nonlocal running, failed
            try:
                ok = await self._one(index, spans, payload, errors)
            finally:
                running -= 1
            if ok:
                latencies.append((time.perf_counter() - due) * 1000)
                written.append(index)
            else:
                failed += 1

        began = time.perf_counter()
        for i, index in enumerate(indices):
            due = began + i / rate
            if (delay := due - time.perf_counter()) > 0:
                await asyncio.sleep(delay)
            if running >= in_flight:
                shed += 1
                continue
            running += 1
            task = asyncio.create_task(one(index, due))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        pending: set[asyncio.Task[None]] = set()
        if tasks:
            remaining = self.max_step_seconds - (time.perf_counter() - began)
            _, pending = await asyncio.wait(set(tasks), timeout=max(remaining, 1))
        seconds = time.perf_counter() - began
        for task in pending:
            task.cancel()
        return {
            "latencies": latencies,
            "written": written,
            "failed": failed,
            "shed": shed,
            "errors": errors,
            "seconds": seconds,
            "stopped_early": bool(pending),
        }


def _merge(parts: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {"latencies": [], "written": [], "failed": 0, "shed": 0, "errors": {}}
    for part in parts:
        merged["latencies"] += part["latencies"]
        merged["written"] += part["written"]
        merged["failed"] += part["failed"]
        merged["shed"] += part["shed"]
        for error, count in part["errors"].items():
            merged["errors"][error] = merged["errors"].get(error, 0) + count
    merged["written"].sort()
    merged["seconds"] = max(part["seconds"] for part in parts)
    merged["stopped_early"] = any(part["stopped_early"] for part in parts)
    return merged


def _writer_process(connection: Any, database_url: str, root: str, schema: str, step: float, threads: int) -> None:
    """A writer process: runs each ``(method, arguments)`` the parent sends on its own store and
    sends back the result, until it is sent None."""
    from martian.evals.store.mlflow_local import MlflowStore

    async def serve() -> None:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=threads))
        writer = Writer(MlflowStore(database_url, root, schema=schema), step)
        connection.send("ready")
        while (message := connection.recv()) is not None:
            method, arguments = message
            connection.send(await getattr(writer, method)(**arguments))

    asyncio.run(serve())


class Writers:
    """This process's writer, or ``--processes`` writer processes that share each step's traces."""

    def __init__(self, local: Writer, processes: int) -> None:
        self.local = local
        self.processes = processes
        self.children: list[tuple[Any, Any]] = []

    def start(self, database_url: str, root: Path, schema: str, step: float, threads: int) -> None:
        if self.processes == 1:
            return
        import multiprocessing

        context = multiprocessing.get_context("spawn")
        for _ in range(self.processes):
            mine, theirs = context.Pipe()
            process = context.Process(
                target=_writer_process, args=(theirs, database_url, str(root), schema, step, threads), daemon=True
            )
            process.start()
            self.children.append((process, mine))
        for _, connection in self.children:
            if connection.recv() != "ready":
                raise RuntimeError("a writer process did not start")

    async def run(self, method: str, indices: list[int], **arguments: Any) -> dict[str, Any]:
        """Share the indexes between the writers; ``offer``'s rate and in-flight limit are shared too."""
        if not self.children:
            return _merge([await getattr(self.local, method)(indices, **arguments)])
        count = len(self.children)
        if method == "offer":
            arguments = arguments | {
                "rate": arguments["rate"] / count,
                "in_flight": max(1, arguments["in_flight"] // count),
            }
        for k, (_, connection) in enumerate(self.children):
            connection.send((method, {"indices": indices[k::count], **arguments}))
        return _merge(list(await asyncio.gather(*(asyncio.to_thread(c.recv) for _, c in self.children))))

    def close(self) -> None:
        for process, connection in self.children:
            with contextlib.suppress(OSError):  # a writer process that already ended
                connection.send(None)
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()


# --- phases --------------------------------------------------------------------------------------


class Load:
    def __init__(self, args: argparse.Namespace, store: Any, writers: Writers, database: Database) -> None:
        self.args = args
        self.store = store
        self.principal = _principal()
        self.writers = writers
        self.database = database
        self.next_index = 0
        self.written: list[int] = []
        """Indexes of the traces written."""
        self.errors: dict[str, int] = {}

    def _take(self, count: int) -> list[int]:
        indices = list(range(self.next_index, self.next_index + count))
        self.next_index += count
        return indices

    def _record(self, result: dict[str, Any]) -> dict[str, Any]:
        self.written += result["written"]
        for error, count in result["errors"].items():
            self.errors[error] = self.errors.get(error, 0) + count
        return result

    async def _time(self, call: Callable[[], Awaitable[int]]) -> dict[str, Any]:
        runs: list[float] = []
        items = 0
        for _ in range(self.args.searches):
            began = time.perf_counter()
            items = await call()
            runs.append(time.perf_counter() - began)
            if runs[0] > self.args.slow_search_seconds:
                break
        return {"seconds": round(statistics.median(runs), 3), "runs": [round(r, 3) for r in runs], "items": items}

    async def searches(self) -> dict[str, Any]:
        """Time each search the console makes on the project as it stands, from this process."""
        from martian.evals.model import TraceQuery

        store, principal = self.store, self.principal
        some = random.Random(len(self.written)).choice(self.written)
        newest = max(self.written)
        queries = {
            # The Traces page: the newest 50, no filters.
            "page": TraceQuery(project_id=PROJECT, limit=50),
            # Its last page, the oldest 50. The cursor is MlflowStore's: the offset of the page.
            "last_page": TraceQuery(project_id=PROJECT, limit=50, cursor=str(max(len(self.written) - 50, 0))),
            # One session's turns.
            "session": TraceQuery(project_id=PROJECT, session_id=f"session-{some // TURNS}", limit=50),
            # One user's newest 50, of one trace in USERS.
            "user": TraceQuery(project_id=PROJECT, user_id=f"user-{some % USERS}", limit=50),
            # One tag value's newest 50, of one trace in ROUTES.
            "tag": TraceQuery(project_id=PROJECT, tags={"route": f"route-{some % ROUTES}"}, limit=50),
            # What a sampler polling for new traces asks: the first 50 of the newest WINDOW.
            "window": TraceQuery(project_id=PROJECT, since=request_time(max(newest - WINDOW + 1, 0)), limit=50),
            # Free text that one trace holds.
            "text": TraceQuery(project_id=PROJECT, text=f"needle-{some} ", limit=50),
        }
        found: dict[str, Any] = {}
        for name, query in queries.items():

            async def search(query: Any = query) -> int:
                return len((await store.search_traces(principal, query)).items)

            found[name] = await self._time(search)

        async def text_all() -> int:
            # Free text that only the oldest trace holds, following the cursor until it is found: a
            # text search pages through the whole project a bounded number of traces at a time.
            query = TraceQuery(project_id=PROJECT, text=f"needle-{min(self.written)} ", limit=50)
            while True:
                page = await store.search_traces(principal, query)
                if page.items or page.next_cursor is None:
                    return len(page.items)
                query = query.model_copy(update={"cursor": page.next_cursor})

        async def sessions() -> int:
            return len((await store.search_sessions(principal, TraceQuery(project_id=PROJECT))).items)

        async def get() -> int:
            return 0 if await store.get_trace(principal, PROJECT, trace_id(some)) is None else 1

        def mlflow_page() -> int:
            # MLflow's own first page of 50, newest first, from the same SQL store: what the
            # database alone costs for the Traces page, without the binding. It reaches into the
            # binding, so it is a measurement only.
            experiment = store._experiment(PROJECT)
            page, _ = store._store.search_traces(locations=[experiment.experiment_id], max_results=50)
            return len(page)

        def mlflow_text() -> int:
            # MLflow's own full-text filter for text one trace holds, newest 50 first: the database
            # looks for it in the content of every span of the project, where the whole request and
            # response are, without an index. A measurement only, like mlflow_page.
            experiment = store._experiment(PROJECT)
            page, _ = store._store.search_traces(
                locations=[experiment.experiment_id],
                filter_string=f"trace.text ILIKE '%needle-{some} %'",
                max_results=50,
                order_by=["timestamp_ms DESC"],
            )
            return len(page)

        async def native() -> int:
            return int(await store._call(principal, mlflow_page))

        async def native_text() -> int:
            return int(await store._call(principal, mlflow_text))

        found["mlflow_page"] = await self._time(native)
        found["text_all"] = await self._time(text_all)
        found["mlflow_text"] = await self._time(native_text)
        found["sessions"] = await self._time(sessions)
        found["get"] = await self._time(get)
        return {name: found[name] for name in SEARCHES}

    async def growth(self) -> list[dict[str, Any]]:
        steps = []
        for size in self.args.sizes:
            before = self.database.size()["total_bytes"]
            cpu_before = cpu_seconds(self.args.database_cpu_stat)
            result = self._record(
                await self.writers.run(
                    "write",
                    self._take(size - len(self.written)),
                    writers=self.args.writers,
                    spans=self.args.spans,
                    payload=self.args.payload,
                )
            )
            cpu_after = cpu_seconds(self.args.database_cpu_stat)
            measured = self.database.size()
            written = len(result["written"])
            step = {
                "traces": len(self.written),
                "written": written,
                "failed": result["failed"],
                "seconds": round(result["seconds"], 2),
                "traces_per_second": round(written / result["seconds"], 1) if result["seconds"] else None,
                "latency_ms": latency_summary(result["latencies"]),
                "stopped_early": result["stopped_early"],
                "database_cpu_ms_per_trace": _cpu_ms_per_trace(cpu_before, cpu_after, written),
                "database_bytes": measured["total_bytes"],
                "bytes_per_trace": round(measured["total_bytes"] / len(self.written)) if self.written else None,
                "step_bytes_per_trace": round((measured["total_bytes"] - before) / written) if written else None,
                "tables": measured["tables"],
                "searches": await self.searches() if self.written else {},
                "peak_rss_mb": peak_rss_mb(),
            }
            steps.append(step)
            print(f"growth: {_growth_row(step)}", file=sys.stderr, flush=True)
            if step["stopped_early"]:
                break
        return steps

    async def rate(self) -> list[dict[str, Any]]:
        steps = []
        for target in self.args.rates:
            offered = int(target * min(self.args.rate_seconds, self.args.max_step_seconds))
            cpu_before = cpu_seconds(self.args.database_cpu_stat)
            result = self._record(
                await self.writers.run(
                    "offer",
                    self._take(offered),
                    rate=target,
                    in_flight=self.args.in_flight,
                    spans=self.args.spans,
                    payload=self.args.payload,
                )
            )
            cpu_after = cpu_seconds(self.args.database_cpu_stat)
            step = {
                "offered_per_second": target,
                "seconds": round(result["seconds"], 2),
                "offered": offered,
                "written": len(result["written"]),
                "shed": result["shed"],
                "failed": result["failed"],
                "achieved_per_second": round(len(result["written"]) / result["seconds"], 1),
                "latency_from_due_ms": latency_summary(result["latencies"]),
                "database_cpu_ms_per_trace": _cpu_ms_per_trace(cpu_before, cpu_after, len(result["written"])),
                "database_cpu_cores": round((cpu_after - cpu_before) / result["seconds"], 2)
                if cpu_before is not None and cpu_after is not None
                else None,
                "traces": len(self.written),
                "stopped_early": result["stopped_early"],
            }
            steps.append(step)
            print(f"rate: {_rate_row(step)}", file=sys.stderr, flush=True)
        return steps

    async def size(self) -> list[dict[str, Any]]:
        """One writer in this process, so each shape's latency is the store's own."""
        steps = []
        for spans, payload in self.args.shapes:
            before = self.database.size()["total_bytes"]
            result = self._record(await self.writers.local.write(self._take(self.args.shape_traces), 1, spans, payload))
            after = self.database.size()["total_bytes"]
            reads: list[float] = []
            for index in result["written"][:20]:
                began = time.perf_counter()
                await self.store.get_trace(self.principal, PROJECT, trace_id(index))
                reads.append((time.perf_counter() - began) * 1000)
            written = len(result["written"])
            step = {
                "spans": spans,
                "payload": payload,
                "written": written,
                "failed": result["failed"],
                "latency_ms": latency_summary(result["latencies"]),
                "get_latency_ms": latency_summary(reads),
                "bytes_per_trace": round((after - before) / written) if written else None,
                "traces": len(self.written),
            }
            steps.append(step)
            print(f"size: {_size_row(step)}", file=sys.stderr, flush=True)
        return steps


def _environment(args: argparse.Namespace, database: Database) -> dict[str, Any]:
    keys = (
        "MLFLOW_SQLALCHEMYSTORE_POOL_SIZE",
        "MLFLOW_SQLALCHEMYSTORE_MAX_OVERFLOW",
        "MLFLOW_SQLALCHEMYSTORE_POOLCLASS",
    )
    pool = {key: os.environ[key] for key in keys if key in os.environ}
    return {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "load_average": [round(x, 2) for x in os.getloadavg()],
        "mlflow_skinny": importlib.metadata.version("mlflow-skinny"),
        "martian_sdk": importlib.metadata.version("martian-sdk"),
        "database": database.server(),
        "pool": pool or "MLflow's default for each process: SQLAlchemy's QueuePool, 5 connections and 10 overflow",
        "note": args.note,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from martian.evals._sql import check_schema, postgres_url
    from martian.evals.model import Project
    from martian.evals.store.mlflow_local import MlflowStore

    schema = check_schema(args.schema or f"load_{uuid.uuid4().hex[:10]}")
    threads = max(args.writers, args.in_flight) + 4
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=threads))
    made_root = args.artifact_root is None
    root = Path(tempfile.mkdtemp(prefix="trace-load-")) if made_root else args.artifact_root
    url = args.database_url
    database = Database(postgres_url(url, schema) if url.startswith("postgresql") else url, schema)
    writers: Writers | None = None
    try:
        store = MlflowStore(url, root, schema=schema)
        await store.create_project(_principal(), Project(id=PROJECT, name="Load test"))
        writers = Writers(Writer(store, args.max_step_seconds), args.processes)
        writers.start(url, root, schema, args.max_step_seconds, threads)
        load = Load(args, store, writers, database)
        hidden = {"database_url"}
        results: dict[str, Any] = {
            "environment": _environment(args, database),
            "parameters": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items() if k not in hidden}
            | {"schema": schema, "turns_per_session": TURNS, "users": USERS, "routes": ROUTES},
        }
        for phase in args.phases:
            results[phase] = await getattr(load, phase)()
        results["errors"] = load.errors
        results["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return results
    finally:
        if writers is not None:
            writers.close()
        if not args.keep_schema:
            database.drop_schema()
        if made_root:
            shutil.rmtree(root, ignore_errors=True)


# --- the summary -----------------------------------------------------------------------------------


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}" if value >= 100 else f"{value:.1f}"


def _growth_row(step: dict[str, Any]) -> str:
    latency = step["latency_ms"]
    searches = step["searches"]
    cells = [
        f"{step['traces']:,}",
        _ms(latency["p50"]),
        _ms(latency["p95"]),
        _ms(latency["p99"]),
        str(step["traces_per_second"]),
        _ms(step["database_cpu_ms_per_trace"]),
        f"{step['database_bytes'] / 1e6:,.1f}",
        f"{step['bytes_per_trace']:,}" if step["bytes_per_trace"] else "-",
        *(f"{searches[name]['seconds']:.3f}" if name in searches else "-" for name in SEARCHES),
        f"{step['peak_rss_mb']:,.0f}",
    ]
    return "| " + " | ".join(cells) + " |"


def _rate_row(step: dict[str, Any]) -> str:
    latency = step["latency_from_due_ms"]
    cells = [
        str(step["offered_per_second"]),
        str(step["achieved_per_second"]),
        str(step["shed"]),
        str(step["failed"]),
        _ms(latency["p50"]),
        _ms(latency["p95"]),
        _ms(latency["p99"]),
        _ms(step["database_cpu_ms_per_trace"]),
        "-" if step["database_cpu_cores"] is None else str(step["database_cpu_cores"]),
        f"{step['traces']:,}",
    ]
    return "| " + " | ".join(cells) + " |"


def _size_row(step: dict[str, Any]) -> str:
    write, read = step["latency_ms"], step["get_latency_ms"]
    cells = [
        str(step["spans"]),
        f"{step['payload']:,}",
        f"{step['written']}/{step['written'] + step['failed']}",
        _ms(write["p50"]),
        _ms(write["p95"]),
        _ms(read["p50"]),
        f"{step['bytes_per_trace']:,}" if step["bytes_per_trace"] else "-",
    ]
    return "| " + " | ".join(cells) + " |"


def summary(results: dict[str, Any]) -> str:
    """The results as Markdown tables: latencies in milliseconds, searches in seconds."""
    lines = []
    if results.get("growth"):
        lines += [
            "Growth (write latency in ms, searches in s, median of their runs):",
            "",
            "| Traces | Write p50 | p95 | p99 | Traces/s | DB CPU ms/trace | DB MB | Bytes/trace | "
            + " | ".join(SEARCHES)
            + " | RSS MB |",
            "|" + " --- |" * (9 + len(SEARCHES)),
            *(_growth_row(step) for step in results["growth"]),
            "",
        ]
    if results.get("rate"):
        lines += [
            "Rate (latency from when each trace was due, in ms):",
            "",
            "| Offered/s | Achieved/s | Shed | Failed | p50 | p95 | p99 | DB CPU ms/trace | DB cores | Traces after |",
            "|" + " --- |" * 10,
            *(_rate_row(step) for step in results["rate"]),
            "",
        ]
    if results.get("size"):
        lines += [
            "Size (one writer; latency in ms):",
            "",
            "| Spans | Payload chars | Written | Write p50 | p95 | Read p50 | Bytes/trace |",
            "|" + " --- |" * 7,
            *(_size_row(step) for step in results["size"]),
            "",
        ]
    if results.get("errors"):
        lines += ["Errors:", "", *(f"- {count} x {error}" for error, count in results["errors"].items()), ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")  # writer processes inherit it
    results = asyncio.run(run(args))
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(summary(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
