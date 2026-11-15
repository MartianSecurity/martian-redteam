"""``scripts/trace_store_load.py``, the trace store's load test: its arguments, and every phase on a
few traces on SQLite. The load itself is not part of the test suite."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("trace_store_load", ROOT / "scripts/trace_store_load.py")
assert _spec is not None and _spec.loader is not None
load = importlib.util.module_from_spec(_spec)
sys.modules["trace_store_load"] = load
_spec.loader.exec_module(load)

URL = ["--database-url", "sqlite:///unused.db"]


def test_the_arguments_are_parsed_and_checked() -> None:
    args = load.parse_args([*URL, "--sizes", "10,100", "--shapes", "0x200,5x1000", "--rates", "5,10"])
    assert args.sizes == [10, 100]
    assert args.shapes == [(0, 200), (5, 1000)]
    assert args.rates == [5, 10]
    assert args.phases == ["growth", "rate", "size"]
    refused = (
        ["--sizes", "100,10"],
        ["--sizes", "10,10"],
        ["--shapes", "5by1000"],
        ["--rates", "0"],
        ["--phases", "growth,soak"],
        ["--processes", "0"],
        ["--rate-seconds", "0"],
    )
    for arguments in refused:
        with pytest.raises(SystemExit):
            load.parse_args([*URL, *arguments])
    with pytest.raises(SystemExit):
        load.parse_args(["--database-url", "mysql://localhost/load"])


def test_a_small_run_measures_every_phase(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)
    output = tmp_path / "load.json"
    code = load.main(
        [
            "--database-url",
            f"sqlite:///{tmp_path / 'mlflow.db'}",
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--sizes",
            "6,12",
            "--writers",
            "2",
            "--spans",
            "2",
            "--payload",
            "100",
            "--searches",
            "1",
            "--rates",
            "20",
            "--rate-seconds",
            "0.5",
            "--shapes",
            "0x50,3x200",
            "--shape-traces",
            "3",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    results = json.loads(output.read_text(encoding="utf-8"))
    growth = results["growth"]
    assert [step["traces"] for step in growth] == [6, 12]
    assert all(step["latency_ms"]["n"] == 6 and not step["stopped_early"] for step in growth)
    searches = growth[-1]["searches"]
    assert list(searches) == list(load.SEARCHES)
    found = {name: searches[name]["items"] for name in load.SEARCHES}
    # Twelve traces: three sessions of up to five turns, every user and route on at most one.
    assert found == {
        "page": 12,
        "mlflow_page": 12,
        "session": found["session"],
        "user": 1,
        "tag": 1,
        "window": 12,
        "text": 1,
        "sessions": 3,
        "get": 1,
    }
    assert 1 <= found["session"] <= load.TURNS
    rate = results["rate"][0]
    assert rate["offered"] == 10 and rate["written"] + rate["shed"] + rate["failed"] == 10
    assert [step["written"] for step in results["size"]] == [3, 3]
    assert results["size"][-1]["traces"] == 12 + rate["written"] + 6
    assert results["errors"] == {}
    assert "database_url" not in results["parameters"]
    assert "| Traces | Write p50 |" in capsys.readouterr().out
