"""Compare explicit kernels on a fixed scope release; never select a new threshold."""

from __future__ import annotations

import argparse
import gc
import json
import time
from pathlib import Path

import numpy as np
import torch

from .contracts import ActionContext, serialize
from .dataset import digest
from .readout import ScopeReadout
from .repair import load_data, write
from .scoring import OperatingPoint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--release-sha256", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attention", choices=["sdpa_math", "sdpa_efficient", "eager"], required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output already exists")
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    manifest, rows = load_data(args.dataset)
    # Use development cases only. Final evaluation is not a tuning workload.
    selected = [r for r in rows if r["split"] == "validation"]
    reference = ScopeReadout(args.release, args.release_sha256, device="cuda")
    point = OperatingPoint.model_validate(reference.operating_point)
    actual_reference = [
        reference.score(ActionContext.model_validate(r["context"]), max_tokens=1024)[0] for r in selected
    ]
    template = ActionContext.model_validate(selected[0]["context"])
    del reference
    gc.collect()
    torch.cuda.empty_cache()
    start = time.perf_counter()
    model = ScopeReadout(args.release, args.release_sha256, device="cuda", attention=args.attention)
    initialized_ms = 1000 * (time.perf_counter() - start)
    actual = [model.score(ActionContext.model_validate(r["context"]), max_tokens=1024)[0] for r in selected]
    delta = np.abs(np.asarray(actual) - actual_reference)
    agreement = point.decisions(actual) == point.decisions(actual_reference)
    report = dict(
        schema_version=2,
        release_manifest_sha256=args.release_sha256,
        dataset_manifest_sha256=digest(args.dataset / "manifest.json"),
        attention=args.attention,
        precision="fp32",
        device=torch.cuda.get_device_name(),
        threads=2,
        batch_size=1,
        concurrency=1,
        initialized_ms=initialized_ms,
        initialization_scope=("Artifact checksums and model/tokenizer load after imports, "
                              "with an already initialized CUDA context"),
        validation_comparison=dict(
            rows=len(selected),
            decisions_agree=int(agreement.sum()),
            max_absolute_logit_difference=float(delta.max()),
            max_absolute_probability_difference=float(
                np.max(
                    np.abs(point.calibration.probabilities(actual) - point.calibration.probabilities(actual_reference))
                )
            ),
        ),
        source_sha256={p.name: digest(p) for p in Path(__file__).parent.glob("*.py")},
        capacity_input="Owned synthetic filler, exact total token lengths; not long-context security-quality evidence",
        timing_scope=(
            "Pydantic context validation, serialization, tokenization, transfer, encoder and readout; "
            "no queue or network"
        ),
        production_enforcement=False,
        buckets=[],
    )
    for length in (256, 512, 1024):
        raw = template.model_dump(mode="json")
        raw["sources"] = [raw["sources"][0]]
        raw["sources"][0]["text"] = "Fictional background. x"
        context = ActionContext.model_validate(raw)
        base = len(serialize(context, model.tokenizer, max_tokens=1024))
        raw["sources"][0]["text"] += " x" * (length - base)
        context = ActionContext.model_validate(raw)
        if len(serialize(context, model.tokenizer, max_tokens=1024)) != length:
            raise ValueError("Capacity filler failed to reach exact token length")
        for _ in range(5):
            model.score(context, max_tokens=1024)
        times = []
        torch.cuda.reset_peak_memory_stats()
        for _ in range(100):
            torch.cuda.synchronize()
            start = time.perf_counter()
            model.score(ActionContext.model_validate(raw), max_tokens=1024)
            torch.cuda.synchronize()
            times.append(1000 * (time.perf_counter() - start))
        report["buckets"].append(
            dict(
                tokens=length,
                iterations=100,
                warmup=5,
                timing_ms={f"p{p}": float(np.percentile(times, p)) for p in (50, 95, 99)},
                sequential_decisions_per_second=1000 / float(np.mean(times)),
                peak_allocated_bytes=torch.cuda.max_memory_allocated(),
                peak_reserved_bytes=torch.cuda.max_memory_reserved(),
            )
        )
    write(args.output, report)
    print(json.dumps(report, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
