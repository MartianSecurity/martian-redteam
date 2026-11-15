"""The grader is out of the target's reach (MartianSecurity/martian-notes#215).

A function target runs in a process of its own and is given the case's inputs only; the
expectations, the checks, the scorers' code, the results and the stores stay where it cannot read
or change them. The proof is a target that looks for the answers everywhere it can: its arguments,
the suite file, the data file, the scorer's file, the results folder, MLflow's store, the local
Evals UI's data folder, its environment, its own memory and the memory of the process that started
it. Each answer is a random ``MARTIAN-SECRET-...`` value, so a find cannot be a coincidence, and two
controls, one in a file beside the target and one in its environment, show that it finds what it
can reach. It also tries to change the results and the stores, and to plant files beside them.

URL targets are called with a body filled from the inputs only, and a placeholder that names an
expectation is refused when the suite loads. Every target the worker calls is given the case's id
and inputs only.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from martian.config import ConfigError
from martian.evals.local import open_stores
from martian.evals.model import Case, Run, Target
from martian.evals.runner import parse_suite, run_suite
from martian.evals.server import COMMUNITY, DEFAULT_PROJECT, SINGLE_USER
from martian.evals.server.launch import admit_run
from martian.evals.server.principal import Principal
from martian.evals.store import EvalStore, MemoryStore
from martian.evals.worker import InProcessWorker, Worker
from martian.evals.worker.targets import sent_case
from tests.evals.write_fixture import AGENT, publish

# The target that looks for its answers. It reads where to look from places.json beside it, so it
# holds no answer itself, and reports every MARTIAN-SECRET value it found, and where.
SNOOPER = r"""
import gc
import json
import os
import re
import sys
from pathlib import Path

PATTERN = re.compile("MARTIAN" + "-SECRET-" + "[0-9a-f]{32}")
HERE = Path(__file__).resolve().parent
PLACES = json.loads((HERE / "places.json").read_text(encoding="utf-8"))


def _hits(text):
    return sorted(set(PATTERN.findall(text)))


def _files(place):
    path = Path(place)
    if path.is_dir():
        for folder, _, names in os.walk(path):
            for name in names:
                yield Path(folder) / name
    else:
        yield path


def _own_memory():
    found = set()
    for item in gc.get_objects():
        values = item.values() if isinstance(item, dict) else item if isinstance(item, (list, tuple)) else ()
        for value in values:
            if isinstance(value, str) and "SECRET" in value:
                found.update(PATTERN.findall(value))
    for frame in sys._current_frames().values():
        while frame is not None:
            found.update(_hits(repr(frame.f_locals)))
            frame = frame.f_back
    return sorted(found)


def _parent_memory():
    parent, found, denied = os.getppid(), set(), []
    for name in ("environ", "cmdline"):
        try:
            found.update(_hits(Path(f"/proc/{parent}/{name}").read_bytes().decode("latin-1")))
        except OSError as error:
            denied.append(f"{name}: {type(error).__name__}")
    try:
        regions = Path(f"/proc/{parent}/maps").read_text().splitlines()
        with open(f"/proc/{parent}/mem", "rb", buffering=0) as memory:
            for region in regions:
                span, permissions = region.split()[:2]
                start, end = (int(edge, 16) for edge in span.split("-"))
                if "r" not in permissions or end - start > 1 << 28:
                    continue
                try:
                    memory.seek(start)
                    found.update(_hits(memory.read(end - start).decode("latin-1")))
                except OSError:
                    continue
    except OSError as error:
        denied.append(f"mem: {type(error).__name__}")
    return sorted(found), denied


def _tamper():
    done = []
    for path in PLACES["change"]:
        try:
            with open(path, "a", encoding="utf-8") as file:
                file.write("tampered\n")
            done.append(path)
        except OSError:
            pass
    for folder in PLACES["plant"]:
        try:
            (Path(folder) / "planted.jsonl").write_text("planted\n", encoding="utf-8")
            done.append(folder)
        except OSError:
            pass
    return done


def target(message, **others):
    read, files = [], []
    for place in [*PLACES["read"], str(HERE)]:
        for path in _files(place):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            read.append(str(path))
            files += [[str(path), hit] for hit in _hits(data.decode("latin-1"))]
    parent, denied = _parent_memory()
    (Path(PLACES["free"]) / "wrote").write_text("the target can write where it may", encoding="utf-8")
    report = {
        "arguments": _hits(json.dumps({"message": message, **others})),
        "files": files,
        "read": read,
        "environment": _hits(json.dumps(dict(os.environ))),
        "locations": {name: value for name, value in os.environ.items() if name.startswith(("MLFLOW_", "MARTIAN_"))},
        "memory": _own_memory(),
        "parent": parent,
        "denied": denied,
        "changed": _tamper(),
    }
    return json.dumps(report)
"""


def secret() -> str:
    return f"MARTIAN-SECRET-{uuid.uuid4().hex}"


def write_snooper(folder: Path, places: dict[str, Any]) -> tuple[Path, str]:
    """The snooping target in ``folder``, where to look, and a control answer beside it that it may
    read. Returns the target's file and the control."""
    control = secret()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "agent.py").write_text(SNOOPER, encoding="utf-8")
    (folder / "notes.txt").write_text(f"a note the target may read: {control}\n", encoding="utf-8")
    (folder / "log").mkdir(exist_ok=True)
    (folder / "places.json").write_text(json.dumps({**places, "free": str(folder / "log")}), encoding="utf-8")
    return folder / "agent.py", control


def found_anything(report: dict[str, Any]) -> set[str]:
    """Every answer the target found, wherever it found it."""
    found = {hit for _, hit in report["files"]}
    for place in ("arguments", "environment", "memory", "parent"):
        found |= set(report[place])
    return found


def stored_text(folder: Path) -> str:
    """Every file under ``folder`` as text, as the test process reads it."""
    return "".join(path.read_bytes().decode("latin-1") for path in folder.rglob("*") if path.is_file())


def check_report(report: dict[str, Any], *, grader: set[str], control: str, env_control: str, private: list[Path]):
    found = found_anything(report)
    # It looked, and found what it may reach: the note beside it, and its environment.
    assert control in found and env_control in report["environment"], report
    assert any(path.endswith("notes.txt") for path in report["read"])
    # It found none of the answers it is graded against, anywhere.
    assert not found & grader, sorted(found & grader)
    assert not [path for path in report["read"] if any(path.startswith(str(hidden)) for hidden in private)]
    # It could neither change the grader's files nor plant files beside them.
    assert report["changed"] == []
    # Its parent's memory and environment are closed to it.
    assert report["parent"] == [] and any(entry.startswith("mem:") for entry in report["denied"])


# --- the suite runner ---------------------------------------------------------------------------


SUITE = """
data: cases.jsonl
target:
  file: agent/agent.py
checks:
  - type: not_contains
    expectation: answer
  - type: contains
    name: knows_the_code
    values: ["{check}"]
  - type: python
    file: scorers.py
    function: graded
batch: 1
concurrency: 1
"""

SCORER = """
ANSWER = "{answer}"


def graded(outputs):
    return ANSWER not in str(outputs), "graded"
"""


@pytest.mark.skipif(sys.platform != "linux", reason="the target's files are hidden with Landlock, on Linux")
def test_a_function_target_that_looks_for_its_answers_finds_none_in_the_suite_runner(tmp_path, monkeypatch):
    answers, check, scorer, env_control = [secret(), secret()], secret(), secret(), secret()
    home = tmp_path / "evals-home"
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(home))
    monkeypatch.setenv("SNOOP_CONTROL", env_control)
    rows = [{"id": f"c{n}", "input": "What is the code?", "expectations": {"answer": a}} for n, a in enumerate(answers)]
    (tmp_path / "cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (tmp_path / "evals.yaml").write_text(SUITE.format(check=check), encoding="utf-8")
    (tmp_path / "scorers.py").write_text(SCORER.format(answer=scorer), encoding="utf-8")
    results = tmp_path / "results"
    private = [
        tmp_path / "evals.yaml",
        tmp_path / "cases.jsonl",
        tmp_path / "scorers.py",
        tmp_path / "__pycache__",
        results,
        home,
    ]
    places = {
        "read": [str(path) for path in private],
        "change": [
            str(path)
            for path in (
                tmp_path / "evals.yaml",
                tmp_path / "cases.jsonl",
                tmp_path / "scorers.py",
                results / "results.jsonl",
                results / "mlflow.db",
                home / "martian.db",
                home / "mlflow.db",
            )
        ],
        "plant": [str(results), str(home)],
    }
    _, control = write_snooper(tmp_path / "agent", places)

    first = run_suite(tmp_path / "evals.yaml")
    # Run again from the start: now every store already holds both cases' answers.
    again = run_suite(tmp_path / "evals.yaml", resume=False)

    grader = {*answers, check, scorer}
    # The answers are where the grader keeps them, so finding none means none could be reached.
    assert all(answer in (tmp_path / "cases.jsonl").read_text(encoding="utf-8") for answer in answers)
    assert check in (tmp_path / "evals.yaml").read_text(encoding="utf-8")
    assert scorer in (tmp_path / "scorers.py").read_text(encoding="utf-8")
    assert all(answer in stored_text(results) and answer in stored_text(home) for answer in answers)
    for summary in (first, again):
        assert summary["target_isolation"] == {"files_hidden": True, "reason": None}
        assert (summary["cases"], summary["errors"]) == (2, 0)
    records = [json.loads(line) for line in (results / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2  # nothing planted or appended
    for record in records:
        report = json.loads(record["output"])
        check_report(report, grader=grader, control=control, env_control=env_control, private=private)
        # Its arguments are the case's inputs, and its environment names none of the run's locations.
        assert record["inputs"] == {"message": "What is the code?"}
        assert not [value for value in report["locations"].values() if str(tmp_path) in value or "evals-home" in value]
        assert "MARTIAN_EVALS_HOME" not in report["locations"]


def test_a_function_target_inside_a_private_folder_is_refused_before_any_case(tmp_path, monkeypatch):
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "agent.py").write_text("def target(message):\n    return 'x'\n", encoding="utf-8")
    (tmp_path / "cases.jsonl").write_text(json.dumps({"input": "hi"}) + "\n", encoding="utf-8")
    suite = tmp_path / "evals.yaml"
    suite.write_text(
        "data: cases.jsonl\noutput: out\ntarget:\n  file: out/agent.py\nchecks:\n  - type: is_json\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="target.file is inside the output folder, which the target may not read"):
        run_suite(suite)


@pytest.mark.parametrize(
    "placeholder",
    ["{{expectations.answer}}", "{{expected_response}}", "{{ Expectation }}", "{{outputs}}", "{{output.text}}"],
)
def test_a_url_target_body_that_names_an_expectation_is_refused_when_the_suite_loads(placeholder):
    text = (
        "data: cases.jsonl\n"
        "target:\n"
        "  url: http://127.0.0.1:9/chat\n"
        f"  body: {{message: '{{{{input}}}}', hint: 'x {placeholder}'}}\n"
        "checks:\n"
        "  - type: is_json\n"
    )
    with pytest.raises(
        ConfigError, match="name the case's expectations or outputs, which are never sent to the target"
    ):
        parse_suite(text)


# --- the worker ---------------------------------------------------------------------------------


async def admitted(store: EvalStore, records: Any, worker: Worker, agent_id: str) -> Run:
    """A run of ``agent_id`` over the published support-faq dataset, submitted to ``worker``."""
    target = Target(kind="agent", agent_id=agent_id)
    dataset = await store.find_dataset(SINGLE_USER, DEFAULT_PROJECT, "support-faq")
    assert dataset is not None
    return await admit_run(
        store,
        records,
        worker,
        SINGLE_USER,
        entitlements=COMMUNITY,
        run_id=str(uuid.uuid4()),
        project=DEFAULT_PROJECT,
        target=target,
        pin=await worker.check_target(SINGLE_USER, target),
        dataset=dataset,
        max_cases=100,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="the target's files are hidden with Landlock, on Linux")
async def test_a_function_target_that_looks_for_its_answers_finds_none_in_the_worker(tmp_path, monkeypatch):
    answers, env_control = [secret(), secret()], secret()
    monkeypatch.setenv("SNOOP_CONTROL", env_control)
    home = tmp_path / "data"
    store, records = await asyncio.to_thread(open_stores, home)  # it runs an event loop of its own
    try:
        cases = [
            Case(id=f"faq-{n}", inputs={"message": f"Question {n}?"}, expectations={"expected_response": answer})
            for n, answer in enumerate(answers)
        ]
        await store.create_dataset(SINGLE_USER, DEFAULT_PROJECT, "support-faq", cases)
        places = {
            "read": [str(home)],
            "change": [str(home / "martian.db"), str(home / "mlflow.db")],
            "plant": [str(home)],
        }
        file, control = write_snooper(tmp_path / "agent", places)
        function = Target(kind="function", file=str(file), function="target")
        worker = InProcessWorker(
            store, records, targets={AGENT: function}, private_paths=[home], target_timeout_seconds=120
        )
        run = await admitted(store, records, worker, AGENT)
        await worker.drain()
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        results = await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        records.close()
    assert finished is not None and finished.state == "completed", finished
    assert all(answer in stored_text(home) for answer in answers)
    assert [(result.case_id, result.status) for result in results.items] == [("faq-0", "failed"), ("faq-1", "failed")]
    for result in results.items:
        report = json.loads(result.output)
        check_report(report, grader=set(answers), control=control, env_control=env_control, private=[home])
        assert report["arguments"] == [] and "MARTIAN_TEST_POSTGRES_URL" not in report["locations"]


class RecordingAgents:
    """A registered agent's executor that keeps every case it is given."""

    def __init__(self) -> None:
        self.cases: list[Case] = []

    async def check(self, principal: Principal, agent_id: str) -> dict[str, Any]:
        return {"kind": "dynamic", "target_id": agent_id}

    async def call(self, principal: Principal, run: Run, case: Case, *, timeout_seconds: float) -> Any:
        self.cases.append(case)
        return "I am not sure."


async def test_every_target_the_worker_calls_is_given_the_cases_id_and_inputs_only(tmp_path):
    from martian.evals.records.sql import SqlRecordStore

    given: list[Case] = []

    async def function(case: Case) -> str:
        given.append(case)
        return "I am not sure."

    agents = RecordingAgents()
    registered = str(uuid.uuid4())
    store = MemoryStore()
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    try:
        await publish(store)
        worker = InProcessWorker(store, records, targets={AGENT: function}, executor=agents)
        for agent_id in (AGENT, registered):
            await admitted(store, records, worker, agent_id)
            await worker.drain()
    finally:
        records.close()
    assert len(given) == len(agents.cases) == 2  # the dataset's third case has no expected text
    for case in [*given, *agents.cases]:
        assert case == Case(id=case.id, inputs=case.inputs)
        assert (case.expectations, case.outputs, case.tags, case.metadata) == ({}, None, {}, {})
    full = Case(id="c", inputs={"message": "hi"}, expectations={"answer": "x"}, outputs="y", tags={"t": "1"})
    assert sent_case(full) == Case(id="c", inputs={"message": "hi"})


async def test_a_worker_refuses_an_http_target_body_that_names_an_expectation(tmp_path):
    from martian.evals.records.sql import SqlRecordStore

    leaking = Target(
        kind="http", url="http://127.0.0.1:9/chat", body={"message": "{{input}}", "hint": "{{expectations.answer}}"}
    )
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    try:
        worker = InProcessWorker(MemoryStore(), records, targets={AGENT: leaking})
        problem = rf"targets\['{AGENT}'\]\.body placeholder\(s\) expectations\.answer name the case's expectations"
        with pytest.raises(ValueError, match=problem):
            await worker.check_target(SINGLE_USER, Target(kind="agent", agent_id=AGENT))
    finally:
        records.close()


def test_a_function_target_is_pinned_by_its_file_so_a_run_never_calls_changed_code(tmp_path):
    from martian.evals.worker.targets import function_pin

    file = tmp_path / "agent.py"
    file.write_text("def target(message):\n    return 'one'\n", encoding="utf-8")
    target = Target(kind="function", file=str(file), function="target")
    before = function_pin(AGENT, target)
    file.write_text("def target(message):\n    return 'two'\n", encoding="utf-8")
    assert before["kind"] == "function" and before["agent_id"] == AGENT
    assert function_pin(AGENT, target)["digest"] != before["digest"]


def test_the_worker_keeps_its_isolated_function_target_running_until_the_run_ends(tmp_path):
    """A function target's process starts before the run's first case and ends with the run."""
    (tmp_path / "agent.py").write_text("import os\n\ndef target(message):\n    return os.getpid()\n", encoding="utf-8")
    from martian.evals.worker.targets import FunctionProcessCall

    call = FunctionProcessCall(
        Target(kind="function", file=str(tmp_path / "agent.py"), function="target"), private=[], timeout_seconds=30
    )

    async def twice() -> list[Any]:
        await call.ready()
        try:
            return [await call(Case(id=str(n), inputs={"message": "hi"})) for n in range(2)]
        finally:
            call.close()

    first, second = asyncio.run(twice())
    assert first == second and first != __import__("os").getpid()
