"""Measure the cheating inspection's miss and false-alarm rates on its hand-written labelled set.

    uv run --extra evals python scripts/benchmark_cheating_inspection.py
    OPENAI_API_KEY=... OPENAI_API_BASE=http://localhost:4000/v1 \\
        uv run --extra evals python scripts/benchmark_cheating_inspection.py --judge openai:/glm-5 --out judge.jsonl

Each transcript of ``tests/evals/benchmarks/cheating_transcripts.json`` is inspected with the set's
protected paths, together with the other transcripts of its run, as ``martian evals
inspect-cheating`` inspects a run. The miss rate is the transcripts labelled cheating that are not
flagged, over those labelled cheating; the false-alarm rate is the transcripts labelled honest that
are flagged, over those labelled honest. The signals call no model.

``--judge`` also has the judge read every transcript once, with temperature 0, and reports its
rates alone and with the signals (the judge reading only the transcripts the signals did not flag,
as ``--judge`` does in the command). A transcript the judge could not decide, or whose call failed,
is not flagged, and is counted on its own. A judged transcript is skipped when the same ``--out``
is used again, and ``--cap`` stops after that many HTTP requests to the model, counted across
reruns of the same ``--out``. The key comes from the environment and is never printed.
"""

from __future__ import annotations

import argparse
import json
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from martian.evals import _judge_transport
from martian.evals.cheating import Protected, Transcript, inspect_transcripts, judge_transcript
from martian.evals.model import Span

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "evals" / "benchmarks" / "cheating_transcripts.json"


def transcript(entry: Mapping[str, Any]) -> Transcript:
    """One labelled transcript: its steps as spans under the call's own span, each starting after the
    one before ended."""
    steps = entry["steps"]
    spans: list[Span] | None = None
    if steps is not None:
        spans = [
            Span(
                id="call",
                name="call",
                inputs=entry["inputs"],
                outputs=entry["output"],
                start_ms=0,
                end_ms=10 * (len(steps) + 1),
            )
        ]
        for number, step in enumerate(steps, 1):
            spans.append(
                Span(
                    id=f"s{number}",
                    parent_id="call",
                    name=step["name"],
                    kind=step.get("kind", ""),
                    status=step.get("status", "ok"),
                    start_ms=10 * number,
                    end_ms=10 * number + 5,
                    inputs=step.get("inputs"),
                    outputs=step.get("outputs"),
                    attributes=step.get("attributes", {}),
                )
            )
    return Transcript(
        case_id=entry["id"],
        inputs=entry["inputs"],
        expectations=entry["expectations"],
        output=entry["output"],
        spans=tuple(spans) if spans is not None else None,
    )


def load(path: Path = CASES) -> tuple[Protected, list[dict[str, Any]]]:
    """The set's protected paths and its transcripts, each with its ``id``, ``label``, ``run`` and
    ``transcript``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    given = data["protected"]
    protected = Protected(paths=tuple(given["paths"]), names=tuple(given["names"]), endpoints=tuple(given["endpoints"]))
    items = [
        {"id": entry["id"], "label": entry["label"], "run": entry.get("run", "main"), "transcript": transcript(entry)}
        for entry in data["transcripts"]
    ]
    return protected, items


def flagged_by_signals(protected: Protected, items: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """The signals each flagged transcript has, inspecting each run's transcripts together."""
    runs: dict[str, list[Transcript]] = {}
    for item in items:
        runs.setdefault(item["run"], []).append(item["transcript"])
    found: dict[str, list[str]] = {}
    for transcripts in runs.values():
        for finding in inspect_transcripts(transcripts, protected)["findings"]:
            found[finding["case"]] = [signal["signal"] for signal in finding["signals"]]
    return found


def rates(items: list[Mapping[str, Any]], flagged: set[str]) -> dict[str, Any]:
    cheating = [item["id"] for item in items if item["label"] == "cheating"]
    honest = [item["id"] for item in items if item["label"] == "honest"]
    misses = [i for i in cheating if i not in flagged]
    false_alarms = [i for i in honest if i in flagged]
    return {
        "transcripts": len(items),
        "labelled_cheating": len(cheating),
        "labelled_honest": len(honest),
        "misses": misses,
        "miss_rate": round(len(misses) / len(cheating), 3) if cheating else None,
        "false_alarms": false_alarms,
        "false_alarm_rate": round(len(false_alarms) / len(honest), 3) if honest else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--judge", help="also measure this judge model, an MLflow model URI such as openai:/glm-5")
    parser.add_argument("--out", type=Path, help="the judge's verdicts as JSONL; reused to skip judged transcripts")
    parser.add_argument("--cap", type=int, default=90, help="stop after this many model requests in all")
    options = parser.parse_args()

    protected, items = load(options.cases)
    signals = flagged_by_signals(protected, items)
    summary: dict[str, Any] = {"signals": rates(items, set(signals)), "signals_found": signals}
    if options.judge:
        if options.out is None:
            parser.error("--judge needs --out")
        done = [json.loads(line) for line in options.out.read_text().splitlines()] if options.out.exists() else []
        judged = {row["id"]: row for row in done}
        calls = [sum(row["calls"] for row in done)]
        lock = threading.Lock()
        mine = [0]
        real_post = _judge_transport._post_chat

        def counted_post(endpoint: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
            with lock:
                if calls[0] >= options.cap:
                    raise RuntimeError("the benchmark's request cap is reached")
                calls[0] += 1
                mine[0] += 1
            return real_post(endpoint, headers, payload)

        _judge_transport._post_chat = counted_post  # type: ignore[assignment]
        with options.out.open("a") as out:
            for item in items:
                if item["id"] in judged or calls[0] >= options.cap:
                    continue
                mine[0] = 0
                row: dict[str, Any] = {"id": item["id"], "label": item["label"]}
                try:
                    verdict = judge_transcript(
                        item["transcript"], model=options.judge, inference_params={"temperature": 0}
                    )
                    row |= verdict
                except (ValueError, RuntimeError) as error:
                    row |= {"verdict": "error", "error": str(error)}
                row["calls"] = mine[0]
                judged[item["id"]] = row
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                print(item["label"], item["id"], "->", row["verdict"], flush=True)
        asked = [item for item in items if item["id"] in judged]
        by_judge = {i for i, row in judged.items() if row["verdict"] == "cheating"}
        summary["judge"] = rates(asked, by_judge) | {
            "uncertain": sorted(i for i, row in judged.items() if row["verdict"] == "uncertain"),
            "errors": sorted(i for i, row in judged.items() if row["verdict"] == "error"),
        }
        both = set(signals) | {i for i in by_judge if i not in signals}
        summary["signals_and_judge"] = rates(asked, both) | {
            "judge_calls_needed": sum(1 for item in asked if item["id"] not in signals)
        }
        summary["model"] = options.judge
        summary["model_requests"] = calls[0]
        summary["input_tokens"] = sum(row.get("input_tokens", 0) for row in judged.values())
        summary["output_tokens"] = sum(row.get("output_tokens", 0) for row in judged.values())
        options.out.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
