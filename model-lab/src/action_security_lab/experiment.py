"""Offline supervised scope experiment: frozen readout, context ablation, full candidate."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import random
import resource
import time
from pathlib import Path

import numpy as np
import torch
from safetensors.torch import save_file
from torch.nn.attention import SDPBackend, sdpa_kernel
from transformers import AutoModel, AutoTokenizer

from .contracts import ActionContext, serialize
from .dataset import digest
from .metrics import rates
from .model_spec import MODEL_ID, MODEL_REVISION
from .scoring import fit_operating_point, quality


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def load_dataset(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["schema_version"] != 1 or manifest["files"] != {
        "decisions.jsonl": digest(directory / "decisions.jsonl")
    }:
        raise ValueError("Dataset integrity mismatch")
    if "oracle_source_file" in manifest and (
        manifest["oracle_source_file"] != "oracle-source.txt"
        or digest(directory / "oracle-source.txt") != manifest["oracle_sha256"]
    ):
        raise ValueError("Oracle snapshot checksum mismatch")
    rows = [json.loads(line) for line in (directory / "decisions.jsonl").read_text().splitlines()]
    if not 1 <= len(rows) <= 10000 or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Invalid dataset cardinality or duplicate identifiers")
    ownership = {field: {} for field in ("family", "world")}
    source_owners = {}
    counts = {}
    for row in rows:
        split = row["split"]
        if split not in {"train", "calibration", "validation", "evaluation", "transfer"}:
            raise ValueError("Unknown dataset split")
        counts[split] = counts.get(split, 0) + 1
        for field, owners in ownership.items():
            if owners.setdefault(row[field], split) != split:
                raise ValueError("Dataset group leaks across partitions")
        context = ActionContext.model_validate(row["context"])
        for source in context.sources:
            source_hash = hashlib.sha256(source.text.encode()).hexdigest()
            if source_owners.setdefault(source_hash, split) != split:
                raise ValueError("Source artifact leaks across partitions")
        if row["labels"] not in ({"task_scope": 0}, {"task_scope": 1}):
            raise ValueError("Invalid scope labels")
        if row["oracle"]["kind"] != "sdk_exact_grant_predicate" or row["oracle"]["business_effect"] != "not_executed":
            raise ValueError("Unsupported label provenance")
        if row["labels"]["task_scope"] == int(row["oracle"]["allowed"]):
            raise ValueError("Label contradicts oracle")
    if counts != manifest["counts"]:
        raise ValueError("Dataset counts mismatch")
    if any("pair" in row for row in rows):
        pairs = {}
        for row in rows:
            if not row.get("pair"):
                raise ValueError("Incomplete counterfactual pairing")
            pairs.setdefault(row["pair"], []).append(row)
        for pair in pairs.values():
            if len(pair) != 2 or {r["labels"]["task_scope"] for r in pair} != {0, 1}:
                raise ValueError("Counterfactual pair must contain opposite labels")
            left, right = pair
            for key in ("split", "family", "world", "workflow", "source_variant"):
                if left[key] != right[key]:
                    raise ValueError("Counterfactual provenance mismatch")
            for key in ("system", "operation", "arguments", "sources", "rules"):
                if left["context"][key] != right["context"][key]:
                    raise ValueError("Counterfactual proposed action or evidence differs")
    return manifest, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, default=Path("/opt/model"))
    parser.add_argument("--device", choices=["cpu", "cuda"], required=True)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument(
        "--full-ablation",
        action="store_true",
        help="Train a matched action-only encoder before a fresh full-context candidate",
    )
    args = parser.parse_args()
    if not 1 <= args.epochs <= 5 or not 1 <= args.threads <= 8:
        parser.error("Epochs must be 1–5 and threads 1–8")
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA unavailable; no device substitution")
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    torch.manual_seed(17)
    random.seed(17)
    np.random.seed(17)
    device = torch.device(args.device)
    manifest, rows = load_dataset(args.dataset)
    source_hashes = {p.name: digest(p) for p in Path(__file__).parent.glob("*.py")}
    protocol = {
        "dataset_manifest_sha256": digest(args.dataset / "manifest.json"),
        "source_sha256": source_hashes,
        "seed": 17,
        "precision": "fp32",
        "attention": "sdpa_math",
        "batch_size": 1,
        "epochs": args.epochs,
        "full_learning_rate": 2e-5,
        "matched_full_ablation": args.full_ablation,
        "frozen_learning_rate": 0.02,
        "frozen_steps": 400,
        "weight_decay": 0.01,
        "pooling": "attention_mask_mean",
        "calibration": "calibration-only regularized affine logit; fixed penalties and FP64",
        "threshold": (
            "validation raw logits only; FP64 score >= threshold flags violation; probabilities never select decisions"
        ),
        "selection": "Fixed settings; no checkpoint selection or final-data tuning",
        "gate": manifest["gate"],
        "enforcement_approved": False,
    }
    # Persist the protocol before running any model. Final rows are validated for
    # integrity above but never participate in fitting, calibration or selection.
    write_json(args.output / "protocol.json", protocol)
    base = json.loads((args.model_path / "martian-model-manifest.json").read_text())
    if base["model_id"] != MODEL_ID or base["revision"] != MODEL_REVISION:
        raise ValueError("Unexpected base model")
    for name, expected in base["sha256"].items():
        if Path(name).name != name or digest(args.model_path / name) != expected:
            raise ValueError("Base model checksum mismatch")
    tokenizer = AutoTokenizer.from_pretrained(args.model_path, local_files_only=True, trust_remote_code=False)
    encoder = AutoModel.from_pretrained(
        args.model_path,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        attn_implementation="sdpa",
        dtype=torch.float32,
    ).to(device)
    encoder.eval()
    encoder.requires_grad_(False)
    training = [r for r in rows if r["split"] == "train"]
    labels_train = torch.tensor([r["labels"]["task_scope"] for r in training], dtype=torch.float32, device=device)
    token_lengths, timings = [], []

    def inputs(row, ablated=False):
        ids = serialize(
            ActionContext.model_validate(row["context"]),
            tokenizer,
            max_tokens=manifest["gate"]["max_tokens"],
            ablate_task=ablated,
        )
        return {
            "input_ids": torch.tensor([ids], device=device),
            "attention_mask": torch.ones((1, len(ids)), dtype=torch.long, device=device),
        }

    def representation(encoded):
        hidden = encoder(**encoded).last_hidden_state
        mask = encoded["attention_mask"].unsqueeze(-1)
        return (hidden * mask).sum(1) / mask.sum(1)

    def features(selected, ablated):
        result = []
        with torch.no_grad(), sdpa_kernel([SDPBackend.MATH]):
            for row in selected:
                result.append(representation(inputs(row, ablated)).detach())
        return torch.cat(result)

    models = {}
    for name, ablated in (("frozen_context", False), ("frozen_action_only", True)):
        print(json.dumps({"phase": "frozen_training", "model": name, "rows": len(training)}), flush=True)
        features_train = features(training, ablated)
        torch.manual_seed(17)
        head = torch.nn.Linear(encoder.config.hidden_size, 1).to(device)
        optimizer = torch.optim.AdamW(head.parameters(), lr=0.02, weight_decay=0.01)
        for _ in range(400):
            optimizer.zero_grad(set_to_none=True)
            loss = torch.nn.functional.binary_cross_entropy_with_logits(head(features_train).squeeze(1), labels_train)
            if not torch.isfinite(loss):
                raise RuntimeError("Nonfinite frozen loss")
            loss.backward()
            optimizer.step()
        del optimizer, features_train
        head.eval()
        calibrating = [r for r in rows if r["split"] == "calibration"]
        validating = [r for r in rows if r["split"] == "validation"]
        with torch.no_grad():
            cal_logits = head(features(calibrating, ablated)).squeeze(1).cpu().numpy()
            val_logits = head(features(validating, ablated)).squeeze(1).cpu().numpy()
        point = fit_operating_point(
            cal_logits,
            [r["labels"]["task_scope"] for r in calibrating],
            val_logits,
            [r["labels"]["task_scope"] for r in validating],
            manifest["gate"]["max_validation_false_block_rate"],
        )
        save_file(
            {k: v.detach().cpu().contiguous() for k, v in head.state_dict().items()},
            str(args.output / f"{name}.safetensors"),
        )
        models[name] = {"head": head, "ablated": ablated, "point": point}

    report = {
        "schema_version": 2,
        "purpose": "synthetic scope feasibility; not customer enforcement",
        "base": base,
        "protocol": protocol,
        "dataset": manifest,
        "models": {},
        "dependencies": {
            n: importlib.metadata.version(n) for n in ("torch", "transformers", "tokenizers", "numpy", "safetensors")
        },
    }

    def evaluate(name, head, ablated, point):
        entry = {"operating_point": point.model_dump(mode="json"), "splits": {}, "errors": []}
        encoder.eval()
        head.eval()
        with torch.no_grad(), sdpa_kernel([SDPBackend.MATH]):
            for split in ("calibration", "validation", "evaluation", "transfer"):
                selected = [r for r in rows if r["split"] == split]
                logits = []
                for row in selected:
                    if device.type == "cuda":
                        torch.cuda.synchronize()
                    start = time.perf_counter()
                    encoded = inputs(row, ablated)
                    result = head(representation(encoded)).squeeze()
                    if device.type == "cuda":
                        torch.cuda.synchronize()
                    if name == "full_context" and split == "evaluation":
                        timings.append((time.perf_counter() - start) * 1000)
                        token_lengths.append(encoded["input_ids"].shape[1])
                    logits.append(float(result))
                values = np.array(logits)
                labels = np.array([r["labels"]["task_scope"] for r in selected])
                entry["splits"][split] = quality(values, labels, point)
                entry["splits"][split]["authored_families"] = len({r["family"] for r in selected})
                entry["splits"][split]["uncertainty_warning"] = "Correlated synthetic rows; no production risk bound"
                scores = point.calibration.probabilities(values)
                decisions = point.decisions(values)
                entry["splits"][split]["by_workflow"] = {}
                for workflow in sorted({r["workflow"] for r in selected}):
                    indexes = np.array([r["workflow"] == workflow for r in selected])
                    entry["splits"][split]["by_workflow"][workflow] = rates(
                        values[indexes], labels[indexes], point.threshold
                    )
                for row, label, score, decision in zip(selected, labels, scores, decisions, strict=True):
                    if bool(decision) != bool(label):
                        entry["errors"].append(
                            {
                                "id": row["id"],
                                "split": split,
                                "variant": row["variant"],
                                "source_variant": row["source_variant"],
                                "label": int(label),
                                "probability": float(score),
                            }
                        )
        return entry

    # Freeze each baseline threshold before scoring final partitions; then discard
    # their heads before changing the encoder for the full candidate.
    for name, item in models.items():
        print(json.dumps({"phase": "baseline_evaluation", "model": name}), flush=True)
        report["models"][name] = evaluate(name, **item)
    del models
    write_json(args.output / "baseline-report.json", report)

    def fit_full(name, ablated):
        torch.manual_seed(17)
        head = torch.nn.Linear(encoder.config.hidden_size, 1).to(device)
        encoder.requires_grad_(True)
        encoder.gradient_checkpointing_enable()
        encoder.train()
        head.train()
        optimizer = torch.optim.AdamW([*encoder.parameters(), *head.parameters()], lr=2e-5, weight_decay=0.01)
        if device.type == "cuda":
            torch.cuda.reset_peak_memory_stats()
        steps = 0
        for epoch in range(args.epochs):
            order = list(training)
            random.Random(17 + epoch).shuffle(order)
            total_loss = 0.0
            for row in order:
                optimizer.zero_grad(set_to_none=True)
                with sdpa_kernel([SDPBackend.MATH]):
                    value = head(representation(inputs(row, ablated))).squeeze()
                    loss = torch.nn.functional.binary_cross_entropy_with_logits(
                        value, torch.tensor(float(row["labels"]["task_scope"]), device=device)
                    )
                    if not torch.isfinite(loss):
                        raise RuntimeError("Nonfinite full-training loss")
                    loss.backward()
                if not torch.isfinite(torch.nn.utils.clip_grad_norm_([*encoder.parameters(), *head.parameters()], 1.0)):
                    raise RuntimeError("Nonfinite gradient")
                optimizer.step()
                steps += 1
                total_loss += float(loss.detach())
                if steps % 128 == 0:
                    print(
                        json.dumps(
                            {"phase": "full_training", "model": name, "epoch": epoch + 1, "completed_updates": steps}
                        ),
                        flush=True,
                    )
            print(
                json.dumps(
                    {"phase": "epoch_complete", "model": name, "epoch": epoch + 1, "loss": total_loss / len(order)}
                ),
                flush=True,
            )
        training_receipt = {
            "completed_updates": steps,
            "trainable_parameters": sum(p.numel() for p in [*encoder.parameters(), *head.parameters()]),
            "peak_allocated_bytes": torch.cuda.max_memory_allocated() if device.type == "cuda" else None,
            "peak_reserved_bytes": torch.cuda.max_memory_reserved() if device.type == "cuda" else None,
        }
        del optimizer
        encoder.eval()
        head.eval()
        calibrating = [r for r in rows if r["split"] == "calibration"]
        validating = [r for r in rows if r["split"] == "validation"]
        with torch.no_grad():
            cal_logits = head(features(calibrating, ablated)).squeeze(1).cpu().numpy()
            val_logits = head(features(validating, ablated)).squeeze(1).cpu().numpy()
        point = fit_operating_point(
            cal_logits,
            [r["labels"]["task_scope"] for r in calibrating],
            val_logits,
            [r["labels"]["task_scope"] for r in validating],
            manifest["gate"]["max_validation_false_block_rate"],
        )
        write_json(
            args.output / ("operating-point.json" if name == "full_context" else name + "-operating-point.json"),
            point.model_dump(mode="json"),
        )
        report["models"][name] = evaluate(name, head, ablated, point)
        report["models"][name]["training"] = training_receipt
        return head, training_receipt, point

    if args.full_ablation:
        head, _, _ = fit_full("full_action_only", True)
        encoder.save_pretrained(args.output / "action-only-encoder", safe_serialization=True)
        save_file(
            {k: v.detach().cpu().contiguous() for k, v in head.state_dict().items()},
            str(args.output / "full_action_only.safetensors"),
        )
        write_json(args.output / "full-ablation-report.json", report)
        # A matched comparison starts from the same base, never the ablated weights.
        del encoder, head
        if device.type == "cuda":
            torch.cuda.empty_cache()
        encoder = AutoModel.from_pretrained(
            args.model_path,
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            attn_implementation="sdpa",
            dtype=torch.float32,
        ).to(device)
    head, report["training"], point = fit_full("full_context", False)
    final = report["models"]["full_context"]["splits"]["evaluation"]
    baseline = report["models"]["frozen_action_only"]["splits"]["evaluation"]
    context_baseline = report["models"]["frozen_context"]["splits"]["evaluation"]
    report["inference"] = {
        "device": args.device,
        "threads": args.threads,
        "gpu": torch.cuda.get_device_name() if device.type == "cuda" else None,
        "scope": "context validation, serialization, tokenization, transfer, encoder and readout; no queue or network",
        "tokens_min": min(token_lengths),
        "tokens_max": max(token_lengths),
        "timing_ms": {f"p{p}": float(np.percentile(timings, p)) for p in (50, 95, 99)},
    }
    report["screening_gate"] = {
        "quality": final["false_block_rate"] <= manifest["gate"]["max_validation_false_block_rate"]
        and final["violation_recall"] >= manifest["gate"]["minimum_evaluation_violation_recall"],
        "context_gain_frozen_matched_comparison": context_baseline["violation_recall"] - baseline["violation_recall"]
        >= manifest["gate"]["min_context_recall_gain_over_ablation"],
        "latency": report["inference"]["timing_ms"]["p95"] <= manifest["gate"]["max_gpu_p95_ms"],
        "context_gain_full_matched_comparison": (
            final["violation_recall"] - report["models"]["full_action_only"]["splits"]["evaluation"]["violation_recall"]
            >= manifest["gate"]["min_context_recall_gain_over_ablation"]
        )
        if args.full_ablation
        else None,
        "production_promotion": False,
    }
    report["rss_peak_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    encoder.save_pretrained(args.output / "encoder", safe_serialization=True)
    tokenizer.save_pretrained(args.output / "encoder")
    save_file(
        {k: v.detach().cpu().contiguous() for k, v in head.state_dict().items()},
        str(args.output / "full_context.safetensors"),
    )
    write_json(args.output / "report.json", report)
    artifacts = {str(p.relative_to(args.output)): digest(p) for p in args.output.rglob("*") if p.is_file()}
    write_json(
        args.output / "release-manifest.json",
        {
            "schema_version": 2,
            "status": "experimental_not_approved",
            "base_model": base,
            "files": artifacts,
            "supported_rule": "task_scope",
            "production_enforcement": False,
        },
    )
    print(
        json.dumps(
            {
                "phase": "complete",
                "screening_gate": report["screening_gate"],
                "training": report["training"],
                "inference": report["inference"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
