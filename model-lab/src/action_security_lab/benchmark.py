"""Measure the actual pinned encoder; never turn random readouts into security scores."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import time
from contextlib import nullcontext
from pathlib import Path

import numpy as np
import torch
from torch.nn.attention import SDPBackend, sdpa_kernel
from transformers import AutoModel, AutoTokenizer

from .model_spec import MODEL_ID, MODEL_REVISION
CAPACITY_TEXT = (
    "The authorized task is to update the description of a fictional CRM lead. "
    "The proposed action changes only that lead's description. A retrieved customer "
    "note contains an instruction to change a different record. The policy permits "
    "only the approved record and field. This is a synthetic capacity measurement. "
)


def percentiles(values: list[float]) -> dict:
    return {f"p{p}_ms": round(float(np.percentile(values, p)), 3) for p in (50, 95, 99)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, default=Path("/opt/model"))
    parser.add_argument("--device", choices=["cpu", "cuda"], required=True)
    parser.add_argument("--precision", choices=["fp32", "fp16"], default="fp32")
    parser.add_argument("--tokens", type=int, nargs="+", default=[256, 512, 1024])
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--attention", choices=["eager", "sdpa_math"], default="sdpa_math")
    parser.add_argument("--fit-steps", type=int, default=0)
    parser.add_argument("--fit-mode", choices=["frozen_encoder", "full"], default="frozen_encoder")
    parser.add_argument("--checkpointing", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.threads <= 32 or not 10 <= args.iterations <= 1000 or not 0 <= args.warmup <= 50:
        parser.error("Invalid measurement bounds")
    if not args.tokens or any(n < 16 or n > 4096 for n in args.tokens) or not 0 <= args.fit_steps <= 20:
        parser.error("Tokens must be 16–4096 and fit steps 0–20")
    if args.device == "cpu" and args.precision != "fp32":
        parser.error("This CPU experiment measures fp32 explicitly")
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA was requested but is unavailable; no CPU substitution")
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    torch.manual_seed(17)
    device = torch.device(args.device)
    precision = torch.float16 if args.precision == "fp16" else torch.float32

    def sync() -> None:
        if device.type == "cuda":
            torch.cuda.synchronize()

    def attention():
        return sdpa_kernel([SDPBackend.MATH]) if args.attention == "sdpa_math" else nullcontext()

    def autocast():
        return torch.autocast("cuda", dtype=precision) if precision == torch.float16 else nullcontext()

    manifest = json.loads((args.model_path / "martian-model-manifest.json").read_text())
    if manifest["model_id"] != MODEL_ID or manifest["revision"] != MODEL_REVISION:
        raise RuntimeError("Unexpected model artifact identity")
    for name, digest in manifest["sha256"].items():
        if hashlib.file_digest((args.model_path / name).open("rb"), "sha256").hexdigest() != digest:
            raise RuntimeError("Model artifact integrity mismatch")
    start = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(args.model_path, local_files_only=True, trust_remote_code=False)
    encoder = AutoModel.from_pretrained(
        args.model_path,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        attn_implementation="sdpa" if args.attention == "sdpa_math" else "eager",
        dtype=torch.float32,
    ).to(device)
    encoder.eval()
    sync()
    initialized_ms = (time.perf_counter() - start) * 1000
    parameter_count = sum(p.numel() for p in encoder.parameters())
    if not 100_000_000 <= parameter_count <= 200_000_000:
        raise RuntimeError("Unexpected backbone parameter count")
    report = {
        "schema_version": 1,
        "purpose": "backbone capacity and compute fit; not trained security quality",
        "input_kind": "synthetic capacity text, deliberately sized to exact token buckets",
        "model": manifest,
        "parameters": parameter_count,
        "device": args.device,
        "precision": args.precision,
        "attention": args.attention,
        "threads": args.threads,
        "batch_size": 1,
        "concurrency": 1,
        "warmup": args.warmup,
        "iterations": args.iterations,
        "initialized_ms": round(initialized_ms, 3),
        "initialization_scope": "local tokenizer and weight load plus device transfer; excludes download and checksum",
        "platform": platform.platform(),
        "logical_cpus": os.cpu_count(),
        "dependencies": {n: importlib.metadata.version(n) for n in ("torch", "transformers", "tokenizers", "numpy")},
        "buckets": [],
        "quality_evaluated": False,
    }
    if device.type == "cuda":
        report["gpu"] = {
            "name": torch.cuda.get_device_name(),
            "capability": torch.cuda.get_device_capability(),
            "build_architectures": torch.cuda.get_arch_list(),
            "free_bytes_before": torch.cuda.mem_get_info()[0],
            "total_bytes": torch.cuda.mem_get_info()[1],
        }
    for length in args.tokens:
        text = CAPACITY_TEXT * (length // 32 + 1)

        def encode(text=text, length=length):
            ids = tokenizer.encode(text, add_special_tokens=False)
            # Sizing is intentional only for this compute probe. Production contexts must reject overflow.
            ids = [tokenizer.cls_token_id, *ids[: length - 2], tokenizer.sep_token_id]
            if len(ids) != length:
                raise RuntimeError("Capacity input did not reach the requested length")
            return {
                "input_ids": torch.tensor([ids], dtype=torch.long, device=device),
                "attention_mask": torch.ones((1, length), dtype=torch.long, device=device),
            }

        timings, forward_timings = [], []
        if device.type == "cuda":
            torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode(), attention(), autocast():
            for i in range(args.warmup + args.iterations):
                sync()
                start = time.perf_counter()
                inputs = encode()
                sync()
                forward_start = time.perf_counter()
                result = encoder(**inputs).last_hidden_state
                sync()
                end = time.perf_counter()
                if result.shape[:2] != (1, length) or not torch.isfinite(result).all():
                    raise RuntimeError("Invalid encoder result")
                if i >= args.warmup:
                    timings.append((end - start) * 1000)
                    forward_timings.append((end - forward_start) * 1000)
                del result
        bucket = {
            "tokens": length,
            "tokenize_transfer_forward": percentiles(timings),
            "forward_only": percentiles(forward_timings),
            "sequential_decisions_per_second": round(1000 / float(np.mean(timings)), 3),
        }
        if device.type == "cuda":
            bucket["peak_allocated_bytes"] = torch.cuda.max_memory_allocated()
            bucket["peak_reserved_bytes"] = torch.cuda.max_memory_reserved()
        report["buckets"].append(bucket)
    if args.fit_steps:
        # This real optimizer workload measures fit only; it produces no security checkpoint or probabilities.
        encoder.requires_grad_(args.fit_mode == "full")
        encoder.train(args.fit_mode == "full")
        if args.checkpointing:
            if args.fit_mode != "full":
                raise ValueError("Checkpointing requires full training mode")
            encoder.gradient_checkpointing_enable()
        head = torch.nn.Linear(encoder.config.hidden_size, 1).to(device)
        parameters = [p for p in [*encoder.parameters(), *head.parameters()] if p.requires_grad]
        optimizer = torch.optim.AdamW(parameters, lr=1e-5)
        scaler = torch.amp.GradScaler("cuda", enabled=precision == torch.float16)
        completed_steps = 0

        def count_optimizer_step(_optimizer, _args, _kwargs):
            nonlocal completed_steps
            completed_steps += 1

        # GradScaler can skip AdamW on overflow. Count executed updates, not just attempts.
        step_counter = optimizer.register_step_post_hook(count_optimizer_step)
        if device.type == "cuda":
            torch.cuda.reset_peak_memory_stats()
        times = []
        for _ in range(args.fit_steps):
            sync()
            start = time.perf_counter()
            optimizer.zero_grad(set_to_none=True)
            # Checkpoint recomputation must use the same attention backend as its forward.
            with attention():
                with autocast():
                    values = encoder(**encode()).last_hidden_state[:, 0]
                    loss = torch.nn.functional.binary_cross_entropy_with_logits(
                        head(values).float(), torch.zeros((1, 1), device=device)
                    )
                if not torch.isfinite(loss):
                    raise RuntimeError("Nonfinite fit loss")
                scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            sync()
            times.append((time.perf_counter() - start) * 1000)
        step_counter.remove()
        if completed_steps == 0:
            raise RuntimeError("No optimizer update completed; compute fit is not established")
        report["optimizer_fit"] = {
            "mode": args.fit_mode,
            "steps": args.fit_steps,
            "completed_optimizer_steps": completed_steps,
            "skipped_optimizer_steps": args.fit_steps - completed_steps,
            "tokens": args.tokens[-1],
            "trainable_parameters": sum(p.numel() for p in parameters),
            "checkpointing": args.checkpointing,
            "optimizer": "AdamW fp32 states",
            "timing": percentiles(times),
            "quality_labels": "none; constant compute-probe target",
            "checkpoint_exported": False,
        }
        if device.type == "cuda":
            report["optimizer_fit"].update(
                peak_allocated_bytes=torch.cuda.max_memory_allocated(),
                peak_reserved_bytes=torch.cuda.max_memory_reserved(),
            )
    report["process_peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
