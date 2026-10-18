"""Verify a fixed experimental NX checkpoint on public rows without training."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import torch
from safetensors.torch import load_file
from torch.nn.attention import SDPBackend, sdpa_kernel
from transformers import AutoModel, AutoTokenizer

from .nx_experiment import metrics, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--release-sha256", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    args = parser.parse_args()
    if sha(args.release / "release.json") != args.release_sha256:
        raise ValueError("Release manifest digest mismatch")
    release = json.loads((args.release / "release.json").read_text())
    for name, expected in release["files"].items():
        path = args.release / name
        if not path.resolve().is_relative_to(args.release.resolve()) or path.is_symlink() or sha(path) != expected:
            raise ValueError("Release file integrity mismatch")
    protocol = json.loads((args.release / "protocol.json").read_text())
    manifest = json.loads((args.dataset / "manifest.json").read_text())
    if sha(args.dataset / "manifest.json") != protocol["dataset_manifest_sha256"]:
        raise ValueError("Dataset manifest mismatch")
    if sha(args.dataset / "rows.jsonl") != manifest["files"]["rows.jsonl"]:
        raise ValueError("Dataset rows mismatch")
    point = json.loads((args.release / "operating-point.json").read_text())
    assert 0 < point["temperature"] <= 20 and 0 <= point["threshold"] <= np.nextafter(1.0, 2.0)
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    tokenizer = AutoTokenizer.from_pretrained(args.release / "encoder", local_files_only=True, trust_remote_code=False)
    encoder = AutoModel.from_pretrained(
        args.release / "encoder",
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        attn_implementation="sdpa",
        dtype=torch.float32,
    ).eval()
    head = torch.nn.Linear(encoder.config.hidden_size, 1).eval()
    head.load_state_dict(load_file(str(args.release / "head.safetensors")))
    reserved = set(tokenizer.all_special_ids)

    def evaluate(text):
        if len(text.encode()) > 32768:
            raise ValueError("input_bytes_exceeded")
        tokens = tokenizer.encode(text, add_special_tokens=False)
        if reserved.intersection(tokens):
            raise ValueError("reserved_tokens")
        ids = [tokenizer.cls_token_id, *tokens, tokenizer.sep_token_id]
        if len(ids) > protocol["max_tokens"]:
            raise ValueError("token_budget_exceeded")
        with torch.no_grad(), sdpa_kernel([SDPBackend.MATH]):
            data = torch.tensor([ids])
            mask = torch.ones_like(data)
            hidden = encoder(input_ids=data, attention_mask=mask).last_hidden_state
            mean = hidden.mean(1)
            score = head(torch.nn.functional.normalize(mean, p=2, dim=1)).item()
        assert math.isfinite(score)
        return score, len(ids)

    rows = [json.loads(line) for line in (args.dataset / "rows.jsonl").read_text().splitlines()]
    report = {
        "schema_version": 1,
        "purpose": "CPU fixed-checkpoint reproduction; no training or enforcement",
        "release_sha256": args.release_sha256,
        "operating_point": point,
        "splits": {},
        "rejected": [],
        "latency": {},
    }
    for split in ["evaluation", "transfer"]:
        scores = []
        labels = []
        for row in rows:
            if row["split"] != split:
                continue
            try:
                value, _ = evaluate(row["text"])
            except ValueError as exc:
                report["rejected"].append({"id": row["id"], "split": split, "reason": str(exc)})
            else:
                scores.append(value)
                labels.append(row["label"])
        report["splits"][split] = metrics(np.array(scores), np.array(labels), point["temperature"], point["threshold"])
    for count in [64, 128, 256]:
        samples = []
        for i in range(35):
            start = time.perf_counter()
            _, length = evaluate(" ".join(["world"] * (count - 2)))
            assert length == count
            if i >= 5:
                samples.append((time.perf_counter() - start) * 1000)
        report["latency"][str(count)] = {
            k: float(np.percentile(samples, p)) for k, p in [("p50_ms", 50), ("p95_ms", 95), ("p99_ms", 99)]
        }
    print(json.dumps(report, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
