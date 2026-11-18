"""Score the grounding and retrieval judges against their hand-labelled set.

    OPENAI_API_KEY=... OPENAI_API_BASE=http://localhost:4000/v1 \\
        uv run --extra evals python scripts/benchmark_grounding_judges.py --model openai:/glm-5 --out results.jsonl

Each case of ``tests/evals/benchmarks/grounding_judges.jsonl`` is graded once by its judge, with
temperature 0. A finished case is skipped when the same ``--out`` is used again, so a rerun spends
no calls on it. ``--cap`` stops the run after that many HTTP requests to the model, counted across
reruns of the same ``--out``. The summary gives, per judge, the false pass rate (cases labelled
fail that the judge passed, over the cases labelled fail) and the false fail rate (labelled pass,
judged fail, over the cases labelled pass). A case the judge gave no verdict is an error: it is
counted on its own, and a release gate would count it as a failure. The key comes from the
environment and is never printed.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from martian.evals import _judge_transport, grounding

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "evals" / "benchmarks" / "grounding_judges.jsonl"
JUDGES = {
    "groundedness": grounding.groundedness,
    "context_relevance": grounding.context_relevance,
    "context_sufficiency": grounding.context_sufficiency,
}
_CODE = re.compile(r"\[(judge_[a-z_]+)\]")
# Requests sent by the case each worker thread is grading: the transport is synchronous.
_mine = threading.local()


def grade(case: dict[str, Any], judge: Any) -> dict[str, Any]:
    inputs = {"message": case["request"], "context": case["documents"]}
    expectations = {"expected_response": case["expected_response"]} if "expected_response" in case else {}
    _mine.calls, _mine.reply, _mine.metadata = 0, None, {}
    started = time.monotonic()
    row: dict[str, Any] = {"id": case["id"], "judge": case["judge"], "label": case["label"]}
    try:
        feedback = judge(inputs=inputs, outputs=case.get("response"), expectations=expectations)
        metadata = feedback.metadata or {}
        row |= {
            "verdict": "pass" if feedback.value else "fail",
            "rationale": feedback.rationale,
            "evidence": json.loads(metadata["martian.judge.evidence"]),
            "model_version": metadata.get("martian.judge.model_version"),
            "response_format": metadata.get("martian.judge.response_format"),
            "input_tokens": int(metadata.get("mlflow.assessment.judgeInputTokens", 0)),
            "output_tokens": int(metadata.get("mlflow.assessment.judgeOutputTokens", 0)),
        }
    except ValueError as error:
        code = _CODE.search(str(error))
        row |= {"verdict": "error", "error": str(error), "code": code[1] if code else "other"}
        # The model's own reply, to see why its quote was refused. The texts here are invented.
        row["reply"] = _mine.reply
        row["input_tokens"] = int(_mine.metadata.get("mlflow.assessment.judgeInputTokens", 0))
        row["output_tokens"] = int(_mine.metadata.get("mlflow.assessment.judgeOutputTokens", 0))
    row["seconds"] = round(time.monotonic() - started, 2)
    row["calls"] = _mine.calls
    return row


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name in JUDGES:
        mine = [row for row in rows if row["judge"] == name]
        if not mine:
            continue
        passing = [row for row in mine if row["label"] == "pass"]
        failing = [row for row in mine if row["label"] == "fail"]
        false_pass = [row["id"] for row in failing if row["verdict"] == "pass"]
        false_fail = [row["id"] for row in passing if row["verdict"] == "fail"]
        errors = [row["id"] for row in mine if row["verdict"] == "error"]
        summary[name] = {
            "cases": len(mine),
            "labelled_pass": len(passing),
            "labelled_fail": len(failing),
            "false_pass": false_pass,
            "false_pass_rate": round(len(false_pass) / len(failing), 3) if failing else None,
            "false_fail": false_fail,
            "false_fail_rate": round(len(false_fail) / len(passing), 3) if passing else None,
            "errors": errors,
            "error_codes": sorted({row["code"] for row in mine if row["verdict"] == "error"}),
            "input_tokens": sum(row.get("input_tokens", 0) for row in mine),
            "output_tokens": sum(row.get("output_tokens", 0) for row in mine),
            "mean_seconds": round(sum(row["seconds"] for row in mine) / len(mine), 2),
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="the judge's MLflow model URI, such as openai:/glm-5")
    parser.add_argument("--out", type=Path, required=True, help="results JSONL; reused to skip finished cases")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--judge", choices=sorted(JUDGES), action="append", help="only these judges")
    parser.add_argument("--id", action="append", help="only these case ids")
    parser.add_argument("--limit", type=int, help="grade at most this many cases in this invocation")
    parser.add_argument("--cap", type=int, default=80, help="stop after this many model requests in all")
    parser.add_argument("--concurrency", type=int, default=2)
    options = parser.parse_args()

    cases = [json.loads(line) for line in options.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = [json.loads(line) for line in options.out.read_text().splitlines()] if options.out.exists() else []
    finished = {row["id"] for row in done}
    calls = [sum(row["calls"] for row in done)]
    lock = threading.Lock()
    real_post = _judge_transport._post_chat

    def counted_post(endpoint: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        with lock:
            if calls[0] >= options.cap:
                raise RuntimeError("the benchmark's request cap is reached")
            calls[0] += 1
        _mine.calls += 1
        return real_post(endpoint, headers, payload)

    _judge_transport._post_chat = counted_post  # type: ignore[assignment]
    real_invoke = grounding.invoke_response_judge

    def kept_invoke(**kwargs: Any) -> Any:
        feedback = real_invoke(**kwargs)
        _mine.reply, _mine.metadata = feedback.value, dict(feedback.metadata or {})
        return feedback

    grounding.invoke_response_judge = kept_invoke  # type: ignore[assignment]
    judges = {
        name: factory(model=options.model, inference_params={"temperature": 0}, max_retries=2)
        for name, factory in JUDGES.items()
    }
    todo = [
        case
        for case in cases
        if case["id"] not in finished
        and (not options.judge or case["judge"] in options.judge)
        and (not options.id or case["id"] in options.id)
    ]
    todo = todo[: options.limit]

    def one(case: dict[str, Any]) -> dict[str, Any] | None:
        with lock:
            if calls[0] >= options.cap:
                return None  # not graded; a later run with a higher cap grades it
        return grade(case, judges[case["judge"]])

    with ThreadPoolExecutor(max_workers=options.concurrency) as pool, options.out.open("a") as out:
        for row in pool.map(one, todo):
            if row is None:
                continue
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            print(row["judge"], row["id"], row["label"], "->", row["verdict"], row.get("code", ""), flush=True)
    rows = [json.loads(line) for line in options.out.read_text().splitlines()]
    summary = summarise(rows) | {"model_requests": calls[0], "model": options.model}
    options.out.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
