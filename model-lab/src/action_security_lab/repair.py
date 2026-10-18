"""Recalibrate a pinned scope checkpoint; evaluate only through an explicit second phase."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np
import torch

from .contracts import ActionContext
from .dataset import digest
from .metrics import rates
from .readout import ScopeReadout
from .scoring import OperatingPoint, fit_grouped_operating_point, quality


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def load_data(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["dataset"] != "owned-synthetic-scope-readout-repair-v1":
        raise ValueError("Wrong repair dataset")
    if set(manifest["files"]) != {"decisions.jsonl", "oracle-source.txt"}:
        raise ValueError("Unexpected repair dataset files")
    for name, expected in manifest["files"].items():
        if digest(directory / name) != expected:
            raise ValueError("Dataset checksum mismatch")
    rows = [json.loads(line) for line in (directory / "decisions.jsonl").read_text().splitlines()]
    if len(rows) != manifest["rows"] or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Dataset cardinality mismatch")
    owners, pairs, counts = {}, {}, {}
    for row in rows:
        split = row["split"]
        if split not in {"calibration", "validation", "evaluation"} or row["label"] not in {0, 1}:
            raise ValueError("Invalid split or label")
        counts[split] = counts.get(split, 0) + 1
        for key in ("family", "world"):
            if owners.setdefault((key, row[key]), split) != split:
                raise ValueError("Group leaks across splits")
        pairs.setdefault(row["pair"], []).append(row)
        ActionContext.model_validate(row["context"])
    if counts != manifest["counts"]:
        raise ValueError("Split counts mismatch")
    for pair in pairs.values():
        if len(pair) != 2 or {r["label"] for r in pair} != {0, 1}:
            raise ValueError("Invalid counterfactual pair")
        left, right = pair
        for field in ("split", "world", "family", "workflow", "dimension", "carrier"):
            if left[field] != right[field]:
                raise ValueError("Pair provenance mismatch")
        for field in ("task", "sources", "operation", "arguments", "system", "rules"):
            if left["context"][field] != right["context"][field]:
                raise ValueError("Only trusted scope may differ in a counterfactual pair")
    return manifest, rows


def measure(model, selected, max_tokens):
    values, lengths, timings = [], [], []
    for index, row in enumerate(selected):
        if model.device.type == "cuda":
            torch.cuda.synchronize()
        start = time.perf_counter()
        value, length = model.score(ActionContext.model_validate(row["context"]), max_tokens=max_tokens)
        if model.device.type == "cuda":
            torch.cuda.synchronize()
        timings.append(1000 * (time.perf_counter() - start))
        values.append(value)
        lengths.append(length)
        if (index + 1) % 256 == 0:
            print(json.dumps(dict(phase="inference", split=selected[0]["split"], completed=index + 1)), flush=True)
    return np.asarray(values, dtype=np.float64), dict(
        tokens_min=min(lengths),
        tokens_max=max(lengths),
        latency_ms={f"p{p}": float(np.percentile(timings, p)) for p in (50, 95, 99)},
    )


def summarize(rows, logits, point, gate):
    labels = np.array([r["label"] for r in rows])
    report = quality(logits, labels, point)
    families = np.array([r["family"] for r in rows])
    groups = sorted(set(families))
    rng = np.random.default_rng(1701)
    bootstrap = []
    for _ in range(1000):
        chosen = rng.choice(groups, len(groups), replace=True)
        indexes = np.concatenate([np.flatnonzero(families == group) for group in chosen])
        bootstrap.append(rates(logits[indexes], labels[indexes], point.threshold))
    report["family_cluster_bootstrap_95_percentile"] = {
        key: np.percentile([item[key] for item in bootstrap], [2.5, 97.5]).tolist()
        for key in ("false_block_rate", "violation_recall")
    }
    report["authored_families"] = len(groups)
    report["uncertainty_warning"] = (
        "Few authored synthetic families; intervals do not establish a production risk bound"
    )
    report["slices"] = {}
    for field in ("workflow", "dimension", "closeness", "family", "carrier"):
        report["slices"][field] = {}
        for name in sorted({r[field] for r in rows}):
            mask = np.array([r[field] == name for r in rows])
            report["slices"][field][name] = quality(logits[mask], labels[mask], point)
    checks = [report] + [value for field in report["slices"].values() for value in field.values()]
    report["quality_screen"] = all(
        r["false_block_rate"] <= gate["max_false_block_rate"]
        and r["violation_recall"] >= gate["minimum_violation_recall"]
        for r in checks
    )
    report["calibration_screen"] = (
        report["ece_10_equal_width_bins"] <= gate["max_ece"] and report["brier"] <= gate["max_brier"]
    )
    predicted = point.decisions(logits)
    pairs = {}
    for row, decision in zip(rows, predicted, strict=True):
        pairs.setdefault(row["pair"], []).append(bool(decision) == bool(row["label"]))
    report["paired_scope_discrimination"] = dict(
        pairs=len(pairs), both_correct=sum(all(pair) for pair in pairs.values())
    )
    report["decisions"] = [
        dict(id=row["id"], label=row["label"], logit=float(value), probability=float(prob), flagged=bool(decision))
        for row, value, prob, decision in zip(
            rows, logits, point.calibration.probabilities(logits), predicted, strict=True
        )
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["calibrate", "evaluate"], required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--release-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=["cpu", "cuda"], required=True)
    parser.add_argument("--attention", choices=["sdpa_math", "sdpa_efficient", "eager"], default="sdpa_math")
    args = parser.parse_args()
    manifest, rows = load_data(args.dataset)
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    model = ScopeReadout(args.release, args.release_sha256, device=args.device, attention=args.attention)
    protocol = dict(
        schema_version=2,
        phase=args.phase,
        parent_release_sha256=args.release_sha256,
        dataset_manifest_sha256=digest(args.dataset / "manifest.json"),
        gate=manifest["gate"],
        source_sha256={p.name: digest(p) for p in Path(__file__).parent.glob("*.py")},
        device=args.device,
        precision="fp32",
        threads=2,
        attention=args.attention,
        padding="masked multiple of eight" if args.attention == "sdpa_efficient" else "none",
        batch_size=1,
        threshold_selection=(
            "Validation raw logits only; >= threshold; fixed 1% empirical false-block budget "
            "pooled and per workflow/dimension/closeness/family/carrier; threshold inside observed gap"
        ),
        calibration="Calibration-only regularized affine logit; fixed penalty and FP64 computation",
        training=False,
        production_enforcement=False,
    )
    write(args.output / "protocol.json", protocol)
    split_names = ("calibration", "validation") if args.phase == "calibrate" else ("evaluation",)
    selected = {split: [r for r in rows if r["split"] == split] for split in split_names}
    for _ in range(5):
        model.score(
            ActionContext.model_validate(selected[split_names[0]][0]["context"]),
            max_tokens=manifest["gate"]["max_tokens"],
        )
    if args.phase == "calibrate":
        scores, timings = {}, {}
        for split in split_names:
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
        write(args.output / "operating-point.json", point.model_dump(mode="json"))
        reports = {
            split: summarize(selected[split], scores[split], point, manifest["gate"]) | timings[split]
            for split in split_names
        }
        write(args.output / "report.json", dict(protocol=protocol, splits=reports, evaluation_scored=False))
        shutil.copytree(args.release / "encoder", args.output / "encoder")
        shutil.copyfile(args.release / "full_context.safetensors", args.output / "full_context.safetensors")
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
    else:
        if model.manifest["schema_version"] != 2:
            raise ValueError("Evaluation requires the explicit repaired release schema")
        parent_protocol = json.loads((args.release / "protocol.json").read_text())
        if parent_protocol["dataset_manifest_sha256"] != protocol["dataset_manifest_sha256"]:
            raise ValueError("Evaluation corpus differs from the reserved protocol")
        development_report = json.loads((args.release / "report.json").read_text())["splits"]["validation"]
        if not development_report["quality_screen"] or not development_report["calibration_screen"]:
            raise ValueError("Development gates failed; reserved evaluation remains unscored")
        point = OperatingPoint.model_validate(model.operating_point)
        logits, timings = measure(model, selected["evaluation"], manifest["gate"]["max_tokens"])
        report = summarize(selected["evaluation"], logits, point, manifest["gate"]) | timings
        write(
            args.output / "report.json",
            dict(protocol=protocol, operating_point=point.model_dump(mode="json"), report=report),
        )
        # This entry point accepts only the owned synthetic research corpus.
        # Retain the fixed-release evidence through Kubernetes Job logs as well.
        print(
            json.dumps(
                dict(phase="report", protocol=protocol, operating_point=point.model_dump(mode="json"), report=report),
                allow_nan=False,
            ),
            flush=True,
        )
        print(
            json.dumps(
                dict(
                    phase="complete",
                    evaluation={
                        k: v for k, v in report.items() if k not in {"decisions", "slices", "reliability_bins"}
                    },
                )
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
