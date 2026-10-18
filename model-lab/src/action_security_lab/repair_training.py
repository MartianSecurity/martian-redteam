"""Fixed-protocol paired continuation of the original 149M checkpoint.

Train only on fresh training worlds. Calibration/validation run after the final
update; the held-out evaluation partition is never scored by this entry point.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import torch
from safetensors.torch import save_file
from torch.nn.attention import SDPBackend, sdpa_kernel

from .contracts import ActionContext, serialize
from .dataset import digest
from .readout import ScopeReadout
from .repair import load_data, measure, summarize, write
from .scoring import fit_grouped_operating_point


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-data", type=Path, required=True)
    parser.add_argument("--development-data", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--release-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise ValueError("Training requires the requested CUDA device")
    train_manifest = json.loads((args.training_data / "manifest.json").read_text())
    if train_manifest["dataset"] != "owned-synthetic-scope-repair-training-v1":
        raise ValueError("Unexpected training corpus")
    if set(train_manifest["files"]) != {"decisions.jsonl", "oracle-source.txt"}:
        raise ValueError("Unexpected training files")
    for name, expected in train_manifest["files"].items():
        if digest(args.training_data / name) != expected:
            raise ValueError("Training corpus integrity mismatch")
    training = [json.loads(line) for line in (args.training_data / "decisions.jsonl").read_text().splitlines()]
    if len(training) != train_manifest["rows"] or len({r["id"] for r in training}) != len(training):
        raise ValueError("Invalid training cardinality")
    manifest, development = load_data(args.development_data)
    pairs = {}
    for row in training:
        if row["split"] != "train" or row["label"] not in {0, 1}:
            raise ValueError("Non-training row or invalid label")
        pairs.setdefault(row["pair"], []).append(row)
    for pair in pairs.values():
        pair.sort(key=lambda r: r["label"])
        if len(pair) != 2 or [r["label"] for r in pair] != [0, 1]:
            raise ValueError("Invalid training pair")
        for field in ("task", "system", "operation", "arguments", "sources", "rules"):
            if pair[0]["context"][field] != pair[1]["context"][field]:
                raise ValueError("Only authorized scope may change in a pair")
    for field in ("id", "world", "family", "pair"):
        if {r[field] for r in training} & {r[field] for r in development}:
            raise ValueError("Training group overlaps development/evaluation")
    train_sources = {s["text"] for r in training for s in r["context"]["sources"]}
    if train_sources & {s["text"] for r in development for s in r["context"]["sources"]}:
        raise ValueError("Training source overlaps held-out source")
    args.output.mkdir(parents=True, exist_ok=False)
    protocol = dict(
        schema_version=2,
        parent_release_sha256=args.release_sha256,
        training_manifest_sha256=digest(args.training_data / "manifest.json"),
        dataset_manifest_sha256=digest(args.development_data / "manifest.json"),
        source_sha256={p.name: digest(p) for p in Path(__file__).parent.glob("*.py")},
        seed=1702,
        epochs=2,
        learning_rate=2e-5,
        weight_decay=0.01,
        gradient_norm=1.0,
        loss="mean BCE with targets 0.02/0.98 + 0.25 * softplus(2 - (violating_logit - allowed_logit))",
        pair_batch_size=1,
        precision="fp32",
        attention="sdpa_math",
        gradient_checkpointing=True,
        gate=manifest["gate"],
        threshold_selection="Validation 1% budget pooled and per declared slice; raw-logit gap midpoint",
        final_checkpoint_selection="fixed two epochs, no evaluation access",
        training=True,
        evaluation_scored=False,
        production_enforcement=False,
    )
    write(args.output / "protocol.json", protocol)
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    torch.manual_seed(protocol["seed"])
    model = ScopeReadout(args.release, args.release_sha256, device="cuda")
    tokens = {
        r["id"]: serialize(
            ActionContext.model_validate(r["context"]), model.tokenizer, max_tokens=manifest["gate"]["max_tokens"]
        )
        for r in training
    }
    model.encoder.train().requires_grad_(True)
    model.encoder.gradient_checkpointing_enable()
    model.head.train().requires_grad_(True)
    parameters = [*model.encoder.parameters(), *model.head.parameters()]
    optimizer = torch.optim.AdamW(parameters, lr=protocol["learning_rate"], weight_decay=protocol["weight_decay"])
    torch.cuda.reset_peak_memory_stats()
    start = time.perf_counter()
    steps, losses = 0, []
    targets = torch.tensor([0.02, 0.98], device=model.device)
    for epoch in range(protocol["epochs"]):
        order = list(pairs)
        random.Random(protocol["seed"] + epoch).shuffle(order)
        total = 0.0
        for key in order:
            optimizer.zero_grad(set_to_none=True)
            with sdpa_kernel([SDPBackend.MATH]):
                values = []
                for row in pairs[key]:
                    ids = tokens[row["id"]]
                    inputs = dict(
                        input_ids=torch.tensor([ids], device=model.device),
                        attention_mask=torch.ones((1, len(ids)), dtype=torch.long, device=model.device),
                    )
                    hidden = model.encoder(**inputs).last_hidden_state
                    mask = inputs["attention_mask"].unsqueeze(-1)
                    values.append(model.head((hidden * mask).sum(1) / mask.sum(1)).squeeze())
                logits = torch.stack(values)
                loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, targets)
                loss = loss + 0.25 * torch.nn.functional.softplus(2 - (logits[1] - logits[0]))
                if not torch.isfinite(loss):
                    raise ValueError("Nonfinite training loss")
                loss.backward()
            if not torch.isfinite(torch.nn.utils.clip_grad_norm_(parameters, 1.0)):
                raise ValueError("Nonfinite gradient")
            optimizer.step()
            steps += 1
            total += float(loss.detach())
            if steps % 128 == 0:
                print(
                    json.dumps(
                        dict(
                            phase="training",
                            epoch=epoch + 1,
                            updates=steps,
                            elapsed_seconds=round(time.perf_counter() - start, 1),
                        )
                    ),
                    flush=True,
                )
        losses.append(total / len(order))
        print(json.dumps(dict(phase="epoch_complete", epoch=epoch + 1, loss=losses[-1])), flush=True)
    receipt = dict(
        completed_updates=steps,
        examples_seen=steps * 2,
        epoch_loss=losses,
        elapsed_seconds=time.perf_counter() - start,
        peak_allocated_bytes=torch.cuda.max_memory_allocated(),
        peak_reserved_bytes=torch.cuda.max_memory_reserved(),
        parameters=sum(p.numel() for p in parameters),
    )
    write(args.output / "training-receipt.json", receipt)
    del optimizer
    model.encoder.eval().requires_grad_(False)
    model.head.eval().requires_grad_(False)
    torch.cuda.empty_cache()
    scores, timings, selected = {}, {}, {}
    for split in ("calibration", "validation"):
        selected[split] = [r for r in development if r["split"] == split]
        scores[split], timings[split] = measure(model, selected[split], manifest["gate"]["max_tokens"])
    point = fit_grouped_operating_point(
        scores["calibration"],
        [r["label"] for r in selected["calibration"]],
        scores["validation"],
        [r["label"] for r in selected["validation"]],
        manifest["gate"]["max_false_block_rate"],
        {
            field: [r[field] for r in selected["validation"]]
            for field in ("workflow", "dimension", "closeness", "family", "carrier")
        },
    )
    reports = {
        split: summarize(selected[split], scores[split], point, manifest["gate"]) | timings[split] for split in selected
    }
    write(args.output / "operating-point.json", point.model_dump(mode="json"))
    write(
        args.output / "report.json", dict(protocol=protocol, training=receipt, splits=reports, evaluation_scored=False)
    )
    model.encoder.save_pretrained(args.output / "encoder", safe_serialization=True)
    model.tokenizer.save_pretrained(args.output / "encoder")
    save_file(
        {k: v.detach().cpu().contiguous() for k, v in model.head.state_dict().items()},
        str(args.output / "full_context.safetensors"),
    )
    write(
        args.output / "release-manifest.json",
        dict(
            schema_version=2,
            status="experimental_not_approved",
            base_model=model.manifest["base_model"],
            parent_release_sha256=args.release_sha256,
            files={str(p.relative_to(args.output)): digest(p) for p in args.output.rglob("*") if p.is_file()},
            supported_rule="task_scope",
            production_enforcement=False,
        ),
    )
    print(
        json.dumps(
            dict(
                phase="complete",
                training=receipt,
                validation={
                    k: v
                    for k, v in reports["validation"].items()
                    if k not in {"decisions", "slices", "reliability_bins"}
                },
                release_sha256=digest(args.output / "release-manifest.json"),
            )
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
