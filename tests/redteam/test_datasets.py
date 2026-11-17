"""The harmful-content set: download-on-use loaders for permissively licensed datasets, a commercial-use
switch, and no dataset rows in the package (martian-notes#269). Every download here is a local
fixture; nothing reaches the network."""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

from martian.redteam.catalog import ENTRIES, datasets
from martian.redteam.engine import Engine, PlanError, plan
from tests.redteam.engine_helpers import StubService, run_file
from tests.redteam.vulnerable_agents import Agent

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = b'goal,target\nSAMPLE REQUEST ONE,x\nSAMPLE REQUEST TWO,y\n"",z\nSAMPLE REQUEST THREE,w\n'
"""A stand-in for a downloaded dataset: placeholder requests, nothing harmful."""


def check_release():
    spec = importlib.util.spec_from_file_location("check_release", ROOT / "scripts" / "check_release.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_release"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pinned(monkeypatch):
    """``advbench`` pinned to the stand-in file, and a download that counts its calls."""
    sample = datasets.DATASETS["advbench"].model_copy(update={"sha256": hashlib.sha256(SAMPLE).hexdigest()})
    monkeypatch.setattr(datasets, "DATASETS", {**datasets.DATASETS, "advbench": sample})
    calls: list[str] = []

    async def download(url: str) -> bytes:
        calls.append(url)
        return SAMPLE

    return download, calls


def test_every_registered_dataset_is_permissive_and_pinned():
    assert {"MIT", "Apache-2.0", "CC-BY-3.0", "CC-BY-4.0"} == datasets.COMMERCIAL_LICENCES
    for identifier, item in datasets.DATASETS.items():
        assert item.licence in datasets.COMMERCIAL_LICENCES, identifier
        assert item.source.startswith("https://") and item.citation
        assert item.url.startswith("https://raw.githubusercontent.com/") and "/main/" not in item.url
    assert set(ENTRIES["HarmfulContent"].types) == {*datasets.DATASETS, datasets.FILE}


def test_the_package_holds_the_register_and_no_rows():
    folder = ROOT / "src" / "martian" / "redteam" / "catalog" / "datasets"
    assert sorted(path.name for path in folder.iterdir() if path.name != "__pycache__") == [
        "__init__.py",
        "register.json",
    ]
    text = (folder / "register.json").read_text()
    assert "SAMPLE" not in text and "harmful_behaviors.csv" in text  # an address, not its rows
    packaged = [str(path.relative_to(ROOT / "src")) for path in (ROOT / "src").rglob("*") if path.is_file()]
    assert check_release().dataset_rows(packaged) == []


def test_the_release_check_refuses_a_wheel_with_dataset_rows():
    names = [
        "martian/redteam/catalog/datasets/__init__.py",
        "martian/redteam/catalog/datasets/register.json",
        "martian/redteam/catalog/datasets/advbench.txt",
        "martian/data/prompts.jsonl",
        "martian/redteam/verify/vectors.json",
    ]
    assert check_release().dataset_rows(names) == [
        "martian/data/prompts.jsonl",
        "martian/redteam/catalog/datasets/advbench.txt",
    ]


async def test_a_dataset_is_downloaded_on_first_use_then_read_from_the_cache(tmp_path, pinned):
    download, calls = pinned
    rows = await datasets.load("advbench", cache=tmp_path, download=download)
    assert rows == ["SAMPLE REQUEST ONE", "SAMPLE REQUEST TWO", "SAMPLE REQUEST THREE"]
    assert await datasets.load("advbench", cache=tmp_path, download=download) == rows
    assert len(calls) == 1 and calls[0] == datasets.DATASETS["advbench"].url
    # A cached file that changed is downloaded again, never used.
    [cached] = tmp_path.iterdir()
    cached.write_bytes(b"goal\nsomething else\n")
    assert await datasets.load("advbench", cache=tmp_path, download=download) == rows
    assert len(calls) == 2


async def test_a_download_that_is_not_the_pinned_file_is_refused(tmp_path, pinned):
    async def other(url: str) -> bytes:
        return b"goal\nnot the pinned file\n"

    with pytest.raises(datasets.DatasetError, match="not the file the register pins"):
        await datasets.load("advbench", cache=tmp_path, download=other)
    assert list(tmp_path.iterdir()) == []


def harmful(tmp_path: Path, licence: str, **settings):
    file = tmp_path / "mine.csv"
    file.write_text("prompt,kind\nSAMPLE A,x\nSAMPLE B,y\n")
    return run_file(
        vulnerabilities=[
            {"name": "HarmfulContent", "types": ["file"], "dataset": {"file": str(file), "licence": licence}}
        ],
        **settings,
    )


def test_with_the_switch_on_only_permitted_sets_load(tmp_path):
    with pytest.raises(PlanError, match="under CC-BY-NC-4.0, which commercial use excludes"):
        plan(harmful(tmp_path, "CC-BY-NC-4.0"))
    [planned] = plan(harmful(tmp_path, "CC-BY-NC-4.0", datasets={"commercial_use": False}))
    assert planned.probe.metadata["dataset"]["licence"] == "CC-BY-NC-4.0"
    assert plan(harmful(tmp_path, "CC-BY-4.0"))
    with pytest.raises(datasets.DatasetError, match="which commercial use excludes"):
        datasets.check_licence("beavertails", "CC-BY-NC-4.0", commercial_use=True)


async def test_dataset_prompts_are_the_attacks_and_the_attacker_writes_nothing(tmp_path, pinned):
    download, _ = pinned
    service = StubService()
    file = run_file(
        vulnerabilities=[{"name": "HarmfulContent", "types": ["advbench"]}],
        budget={"attempts_per_probe": 2},
        datasets={"cache": str(tmp_path)},
    )
    engine = Engine(file, service=service, target=Agent(lambda message, session: f"LEAKED {message}"))
    engine.download = download
    found = await engine.run()
    assert [attempt.input for attempt in found.attempts] == ["SAMPLE REQUEST ONE", "SAMPLE REQUEST TWO"]
    assert service.simulated == [] and len(service.evaluated) == 2
    assert {finding.counted_as for finding in found.findings} == {"suspected"}
    [used] = found.provenance["datasets"]
    assert (used["id"], used["licence"], used["rows"]) == ("advbench", "MIT", 3)


async def test_a_dataset_that_does_not_load_is_an_error_never_a_pass(tmp_path, monkeypatch):
    async def unreachable(url: str) -> bytes:
        raise OSError("no network")

    file = run_file(
        vulnerabilities=[{"name": "HarmfulContent", "types": ["xstest"]}], datasets={"cache": str(tmp_path)}
    )
    engine = Engine(file, service=StubService(), target=Agent("No."))
    engine.download = unreachable
    found = await engine.run()
    [attempt] = found.attempts
    assert (attempt.outcome, attempt.error_kind, attempt.error_code) == (
        "error",
        "infrastructure",
        "dataset_unavailable",
    )
    assert "could not be downloaded: OSError" in attempt.error
