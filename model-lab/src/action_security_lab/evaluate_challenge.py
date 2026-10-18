"""Measure a fixed experimental release without training or threshold selection."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch

from .contracts import ActionContext
from .dataset import digest
from .metrics import probability, rates
from .readout import ScopeReadout
from .scoring import OperatingPoint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--release-sha256", required=True)
    parser.add_argument("--device", choices=["cuda", "cpu"], required=True)
    parser.add_argument("--attention", choices=["sdpa_math", "sdpa_efficient", "eager"], default="sdpa_math")
    args = parser.parse_args()
    manifest = json.loads((args.dataset / "manifest.json").read_text())
    if manifest["dataset"] not in {
        "owned-synthetic-symmetric-scope-challenge-v1",
        "owned-synthetic-close-value-scope-challenge-v2",
    }:
        raise ValueError("Unexpected challenge")
    for name, expected in manifest["files"].items():
        if Path(name).name != name or digest(args.dataset / name) != expected:
            raise ValueError("Challenge integrity mismatch")
    rows = [json.loads(line) for line in (args.dataset / "decisions.jsonl").read_text().splitlines()]
    if len(rows) != manifest["rows"] or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Challenge cardinality mismatch")
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    model = ScopeReadout(args.release, args.release_sha256, device=args.device, attention=args.attention)
    protocol = {
        "release_manifest_sha256": args.release_sha256,
        "dataset_manifest_sha256": digest(args.dataset / "manifest.json"),
        "operating_point": model.operating_point,
        "training_or_recalibration": False,
        "max_tokens": manifest["gate"]["max_tokens"],
        "device": args.device,
        "attention": args.attention,
        "threads": 2,
        "source_sha256": {p.name: digest(p) for p in Path(__file__).parent.glob("*.py")},
    }
    print(json.dumps({"phase": "protocol", **protocol}), flush=True)
    first = ActionContext.model_validate(rows[0]["context"])
    for _ in range(5):
        model.score(first, max_tokens=protocol["max_tokens"])
    logits, lengths, milliseconds = [], [], []
    for row in rows:
        if args.device == "cuda":
            torch.cuda.synchronize()
        start = time.perf_counter()
        value, length = model.score(ActionContext.model_validate(row["context"]), max_tokens=protocol["max_tokens"])
        if args.device == "cuda":
            torch.cuda.synchronize()
        milliseconds.append((time.perf_counter() - start) * 1000)
        logits.append(value)
        lengths.append(length)
    logits, labels = np.array(logits), np.array([r["label"] for r in rows])
    threshold = model.operating_point["threshold"]
    if model.manifest["schema_version"] == 2:
        point = OperatingPoint.model_validate(model.operating_point)
        calibrated_logits = point.calibration.logits(logits)
        scores = point.calibration.probabilities(logits)
        decision_scores = logits
    else:
        # Explicit historical schema reproduction, never used by new releases.
        temperature = model.operating_point["temperature"]
        calibrated_logits = logits / temperature
        scores = probability(logits, temperature)
        decision_scores = scores
    report = {
        "schema_version": model.manifest["schema_version"],
        "protocol": protocol,
        "dataset": manifest,
        "metrics": rates(decision_scores, labels, threshold),
        "by_dimension": {},
        "by_workflow": {},
        "nll": float(np.mean(np.logaddexp(0, calibrated_logits) - labels * calibrated_logits)),
        "brier": float(np.mean((scores - labels) ** 2)),
        "saturated_probability_one": int((scores == 1).sum()),
        "latency_ms": {f"p{p}": float(np.percentile(milliseconds, p)) for p in (50, 95, 99)},
        "tokens": {"min": min(lengths), "max": max(lengths)},
        "production_enforcement": False,
    }
    for field in ("dimension", "workflow"):
        for key in sorted({r[field] for r in rows}):
            indexes = np.array([r[field] == key for r in rows])
            report[f"by_{field}"][key] = rates(decision_scores[indexes], labels[indexes], threshold)
    report["screening_quality"] = (
        report["metrics"]["false_block_rate"] <= manifest["gate"]["max_false_block_rate"]
        and report["metrics"]["violation_recall"] >= manifest["gate"]["minimum_violation_recall"]
    )
    pairs = {}
    for row, score in zip(rows, decision_scores, strict=True):
        pairs.setdefault(row["pair"], []).append(bool(score >= threshold) == bool(row["label"]))
    if any(len(pair) != 2 for pair in pairs.values()):
        raise ValueError("Invalid symmetric comparison pairs")
    report["paired_scope_discrimination"] = {
        "pairs": len(pairs),
        "both_allowed_and_violating_correct": sum(all(pair) for pair in pairs.values()),
    }
    report["decisions"] = [
        {"id": row["id"], "label": row["label"], "logit": float(value), "probability": float(score)}
        for row, value, score in zip(rows, logits, scores, strict=True)
    ]
    print(json.dumps({"phase": "report", "report": report}, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
