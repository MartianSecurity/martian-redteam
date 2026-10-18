"""Small public-input injection classifier experiment; never an execution permit."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import random
import resource
import time
from collections import Counter
from pathlib import Path

import numpy as np
import torch
from safetensors.torch import save_file
from torch.nn.attention import SDPBackend, sdpa_kernel
from transformers import AutoModel, AutoTokenizer

from .metrics import calibrate, probability, threshold_at_budget


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def metrics(logits, labels, temperature, threshold):
    scores = probability(logits, temperature)
    prediction = scores >= threshold
    benign, injection = labels == 0, labels == 1
    n = int(benign.sum())
    fp = int((prediction & benign).sum())
    if n:
        p = fp / n
        z = 1.959963984540054
        radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        center = p + z * z / (2 * n)
        den = 1 + z * z / n
        interval = [(center - radius) / den, (center + radius) / den]
    else:
        interval = None
    bins = []
    ece = 0
    for i in range(10):
        selected = (scores >= i / 10) & ((scores < (i + 1) / 10) if i < 9 else (scores <= 1))
        count = int(selected.sum())
        confidence = float(scores[selected].mean()) if count else None
        prevalence = float(labels[selected].mean()) if count else None
        if count:
            ece += count / len(labels) * abs(confidence - prevalence)
        bins.append(
            {
                "lower": i / 10,
                "upper": (i + 1) / 10,
                "count": count,
                "mean_probability": confidence,
                "injection_fraction": prevalence,
            }
        )
    return {
        "count": len(labels),
        "benign": n,
        "injections": int(injection.sum()),
        "false_flags": fp,
        "missed_injections": int((~prediction & injection).sum()),
        "false_flag_rate": fp / n if n else None,
        "injection_recall": float(prediction[injection].mean()) if injection.any() else None,
        "false_flag_wilson_95": interval,
        "interval_caveat": "Row-wise interval; correlated public examples are not independent customer observations",
        "nll": float(np.mean(np.logaddexp(0, logits / temperature) - labels * logits / temperature)),
        "brier": float(np.mean((scores - labels) ** 2)),
        "ece_10_equal_width_bins": ece,
        "reliability_bins": bins,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=["cpu", "cuda"], required=True)
    args = parser.parse_args()
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA unavailable; no device substitution")
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    manifest = json.loads((args.dataset / "manifest.json").read_text())
    upstream = json.loads((args.inputs / "manifest.json").read_text())
    assert manifest["upstream_manifest_sha256"] == sha(args.inputs / "manifest.json")
    assert upstream["sources"]["base"]["revision"] == "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    for name, info in upstream["files"].items():
        assert (
            not Path(name).is_absolute() and ".." not in Path(name).parts and sha(args.inputs / name) == info["sha256"]
        )
    for name, digest in manifest["files"].items():
        assert sha(args.dataset / name) == digest
    protocol = {
        **manifest["protocol"],
        "dataset_manifest_sha256": sha(args.dataset / "manifest.json"),
        "source_sha256": sha(Path(__file__)),
        "metrics_source_sha256": sha(Path(__file__).with_name("metrics.py")),
        "precision": "fp32",
        "attention": "sdpa_math",
        "pooling": "normalized masked mean",
        "full_head_initialization": "frozen trained head",
        "full_head_learning_rate": 0.001,
        "calibration": "241-point 0.05–20 temperature grid; calibration NLL only",
        "threshold": "lowest validation threshold satisfying fixed false-flag budget; reject-none permitted",
        "runtime_integration": False,
    }
    write(args.output / "protocol.json", protocol)
    torch.manual_seed(protocol["seed"])
    random.seed(protocol["seed"])
    np.random.seed(protocol["seed"])
    device = torch.device(args.device)
    tokenizer = AutoTokenizer.from_pretrained(args.inputs / "base", local_files_only=True, trust_remote_code=False)
    encoder = AutoModel.from_pretrained(
        args.inputs / "base",
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        attn_implementation="sdpa",
        dtype=torch.float32,
    ).to(device)
    head = torch.nn.Linear(encoder.config.hidden_size, 1).to(device)
    params = sum(x.numel() for x in encoder.parameters()) + sum(x.numel() for x in head.parameters())
    rows = [json.loads(line) for line in (args.dataset / "rows.jsonl").read_text().splitlines()]
    reserved = set(tokenizer.all_special_ids)

    def encode(text):
        if len(text.encode()) > 32768:
            raise ValueError("input_bytes_exceeded")
        tokens = tokenizer.encode(text, add_special_tokens=False)
        if reserved.intersection(tokens):
            raise ValueError("reserved_tokens")
        ids = [tokenizer.cls_token_id, *tokens, tokenizer.sep_token_id]
        if len(ids) > protocol["max_tokens"]:
            raise ValueError("token_budget_exceeded")
        return ids

    rejected = []
    valid = []
    for row in rows:
        try:
            row["ids"] = encode(row["text"])
        except ValueError as e:
            rejected.append({"id": row["id"], "split": row["split"], "label": row["label"], "reason": str(e)})
        else:
            valid.append(row)
    selected = {s: [r for r in valid if r["split"] == s] for s in manifest["counts"]}
    for split in ["train", "calibration", "validation", "evaluation"]:
        assert {r["label"] for r in selected[split]} == {0, 1}

    def tensor(batch):
        length = max(len(r["ids"]) for r in batch)
        ids = torch.full((len(batch), length), tokenizer.pad_token_id, dtype=torch.long, device=device)
        mask = torch.zeros_like(ids)
        for i, row in enumerate(batch):
            ids[i, : len(row["ids"])] = torch.tensor(row["ids"], device=device)
            mask[i, : len(row["ids"])] = 1
        return {"input_ids": ids, "attention_mask": mask}

    def representation(data):
        hidden = encoder(**data).last_hidden_state
        mask = data["attention_mask"].unsqueeze(-1)
        mean = (hidden * mask).sum(1) / mask.sum(1)
        return torch.nn.functional.normalize(mean, p=2, dim=1)

    def features(batch):
        with torch.no_grad(), sdpa_kernel([SDPBackend.MATH]):
            return torch.cat([representation(tensor(batch[i : i + 8])).detach() for i in range(0, len(batch), 8)])

    def logits(batch):
        encoder.eval()
        head.eval()
        with torch.no_grad(), sdpa_kernel([SDPBackend.MATH]):
            return head(features(batch)).squeeze(1).cpu().numpy()

    def operating_point():
        cal = selected["calibration"]
        val = selected["validation"]
        temperature = calibrate(logits(cal), np.array([r["label"] for r in cal]))
        threshold = threshold_at_budget(
            probability(logits(val), temperature),
            np.array([r["label"] for r in val]),
            protocol["validation_false_block_budget"],
        )
        return temperature, threshold

    report = {
        "schema_version": 1,
        "purpose": "public input classification research; not action authorization",
        "protocol": protocol,
        "dataset": manifest,
        "parameters": params,
        "input_coverage": {},
        "rejected": rejected,
        "models": {},
        "device": args.device,
        "device_name": torch.cuda.get_device_name() if args.device == "cuda" else "CPU",
        "dependencies": {
            n: importlib.metadata.version(n) for n in ["torch", "transformers", "tokenizers", "numpy", "safetensors"]
        },
    }
    for split, batch in selected.items():
        report["input_coverage"][split] = {
            "accepted": len(batch),
            "total": sum(r["split"] == split for r in rows),
            "labels": dict(Counter(str(r["label"]) for r in batch)),
        }

    def evaluate(name, temperature, threshold):
        result = {"temperature": temperature, "threshold": threshold, "splits": {}, "scores": []}
        for split in ["calibration", "validation", "evaluation", "transfer"]:
            batch = selected[split]
            values = logits(batch)
            labels = np.array([r["label"] for r in batch])
            scores = probability(values, temperature)
            result["splits"][split] = metrics(values, labels, temperature, threshold)
            for r, p in zip(batch, scores, strict=True):
                result["scores"].append(
                    {
                        "id": r["id"],
                        "split": split,
                        "label": r["label"],
                        "probability": float(p),
                        "flagged": bool(p >= threshold),
                    }
                )
            if split == "transfer":
                result["transfer_categories"] = {}
                for category in sorted({r["category"] for r in batch}):
                    indexes = [i for i, r in enumerate(batch) if r["category"] == category]
                    result["transfer_categories"][category] = metrics(
                        values[indexes], labels[indexes], temperature, threshold
                    )
        ev = result["splits"]["evaluation"]
        tr = result["splits"]["transfer"]
        result["quality_screen_passed"] = (
            ev["injection_recall"] >= protocol["evaluation_recall_minimum"]
            and ev["false_flag_rate"] <= protocol["evaluation_false_block_maximum"]
            and tr["false_flag_rate"] <= protocol["transfer_false_block_maximum"]
            and all(
                x["accepted"] / x["total"] >= protocol["coverage_minimum"] for x in report["input_coverage"].values()
            )
        )
        report["models"][name] = result

    encoder.eval()
    encoder.requires_grad_(False)
    x = features(selected["train"])
    y = torch.tensor([r["label"] for r in selected["train"]], dtype=torch.float32, device=device)
    opt = torch.optim.AdamW(
        head.parameters(), lr=protocol["frozen_learning_rate"], weight_decay=protocol["weight_decay"]
    )
    for _ in range(protocol["frozen_steps"]):
        opt.zero_grad(set_to_none=True)
        loss = torch.nn.functional.binary_cross_entropy_with_logits(head(x).squeeze(1), y)
        assert torch.isfinite(loss)
        loss.backward()
        opt.step()
    del opt, x, y
    temperature, threshold = operating_point()
    save_file(
        {k: v.detach().cpu().contiguous() for k, v in head.state_dict().items()},
        str(args.output / "frozen-head.safetensors"),
    )
    evaluate("frozen", temperature, threshold)
    encoder.requires_grad_(True)
    opt = torch.optim.AdamW(
        [
            {"params": encoder.parameters(), "lr": protocol["learning_rate"]},
            {"params": head.parameters(), "lr": protocol["full_head_learning_rate"]},
        ],
        weight_decay=protocol["weight_decay"],
    )
    start = time.perf_counter()
    steps = 0
    if args.device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    training = list(selected["train"])
    rng = random.Random(protocol["seed"])
    for epoch in range(protocol["epochs"]):
        rng.shuffle(training)
        encoder.train()
        head.train()
        losses = []
        for i in range(0, len(training), protocol["batch_size"]):
            batch = training[i : i + protocol["batch_size"]]
            labels = torch.tensor([r["label"] for r in batch], dtype=torch.float32, device=device)
            opt.zero_grad(set_to_none=True)
            with sdpa_kernel([SDPBackend.MATH]):
                loss = torch.nn.functional.binary_cross_entropy_with_logits(
                    head(representation(tensor(batch))).squeeze(1), labels
                )
                assert torch.isfinite(loss)
                loss.backward()
            torch.nn.utils.clip_grad_norm_([*encoder.parameters(), *head.parameters()], 1.0, error_if_nonfinite=True)
            opt.step()
            steps += 1
            losses.append(float(loss.detach()))
        print(
            json.dumps({"epoch": epoch + 1, "steps": steps, "mean_training_loss": float(np.mean(losses))}), flush=True
        )
    report["training"] = {"optimizer_steps": steps, "seconds": time.perf_counter() - start}
    if args.device == "cuda":
        report["training"].update(
            peak_allocated_bytes=torch.cuda.max_memory_allocated(), peak_reserved_bytes=torch.cuda.max_memory_reserved()
        )
    del opt
    encoder.eval()
    head.eval()
    temperature, threshold = operating_point()
    evaluate("full", temperature, threshold)
    encoder.save_pretrained(args.output / "encoder", safe_serialization=True)
    tokenizer.save_pretrained(args.output / "encoder")
    save_file(
        {k: v.detach().cpu().contiguous() for k, v in head.state_dict().items()}, str(args.output / "head.safetensors")
    )
    write(
        args.output / "operating-point.json",
        {"temperature": temperature, "threshold": threshold, "enforcement_approved": False},
    )
    # Capacity text is synthetic, separate from semantic accuracy. Include tokenization,
    # host-to-device transfer and one encoder/head forward, with no decoding loop.
    latency = {}
    for benchmark_device in [args.device, "cpu"] if args.device == "cuda" else ["cpu"]:
        device = torch.device(benchmark_device)
        encoder.to(device)
        head.to(device)
        latency[benchmark_device] = {}
        for n in [64, 128, 256]:
            text = " ".join(["world"] * (n - 2))
            samples = []
            for iteration in range(35):
                if benchmark_device == "cuda":
                    torch.cuda.synchronize()
                started = time.perf_counter()
                with torch.no_grad(), sdpa_kernel([SDPBackend.MATH]):
                    ids = encode(text)
                    assert len(ids) == n
                    score = head(representation(tensor([{"ids": ids}]))).item()
                assert math.isfinite(score)
                if benchmark_device == "cuda":
                    torch.cuda.synchronize()
                if iteration >= 5:
                    samples.append((time.perf_counter() - started) * 1000)
            latency[benchmark_device][str(n)] = {
                k: float(np.percentile(samples, p)) for k, p in [("p50_ms", 50), ("p95_ms", 95), ("p99_ms", 99)]
            }
    report["latency"] = latency
    report["peak_process_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    write(args.output / "report.json", report)
    release = {
        "status": "experimental_not_approved",
        "parameters": params,
        "files": {str(p.relative_to(args.output)): sha(p) for p in sorted(args.output.rglob("*")) if p.is_file()},
    }
    write(args.output / "release.json", release)
    print(
        json.dumps(
            {
                "complete": True,
                "parameters": params,
                "models": {
                    name: {
                        "quality_screen_passed": r["quality_screen_passed"],
                        "evaluation": r["splits"]["evaluation"],
                        "transfer": r["splits"]["transfer"],
                    }
                    for name, r in report["models"].items()
                },
                "latency": latency,
                "training": report["training"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
