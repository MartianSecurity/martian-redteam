"""Public input-classification research splits; no execution/authorization labels."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    upstream = json.loads((args.inputs / "manifest.json").read_text())
    for name, expected in upstream["files"].items():
        if sha(args.inputs / name) != expected["sha256"]:
            raise ValueError("Input checksum mismatch")
    rows = []
    for source_split in ["train", "test"]:
        paths = list((args.inputs / "public/data").glob(source_split + "-*.parquet"))
        assert len(paths) == 1
        for i, r in enumerate(pq.read_table(paths[0]).to_pylist()):
            assert isinstance(r["text"], str) and r["label"] in [0, 1]
            rows.append(
                {
                    "id": f"deepset-{source_split}-{i}",
                    "text": r["text"],
                    "label": r["label"],
                    "source": "deepset",
                    "original_split": source_split,
                    "group": "",
                    "split": "unassigned" if source_split == "train" else "evaluation",
                }
            )
    for file in sorted((args.inputs / "transfer/data").glob("NotInject_*.parquet")):
        for i, r in enumerate(pq.read_table(file).to_pylist()):
            rows.append(
                {
                    "id": file.stem + f"-{i}",
                    "text": r["prompt"],
                    "label": 0,
                    "source": "notinject",
                    "original_split": file.stem,
                    "category": r["category"],
                    "group": "",
                    "split": "transfer",
                }
            )
    # Connected lexical groups include final data only to exclude overlapping
    # development rows. Final text/labels never select training settings or threshold.
    normalized = [" ".join(re.findall(r"\w+", r["text"].casefold())) for r in rows]
    grams = []
    for text in normalized:
        words = text.split()
        grams.append(set(zip(words, words[1:], words[2:], strict=False)) if len(words) >= 3 else {text})
    parents = list(range(len(rows)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i in range(len(rows)):
        for j in range(i):
            overlap = len(grams[i] & grams[j])
            union = len(grams[i] | grams[j])
            if normalized[i] == normalized[j] or (union and overlap / union >= 0.5):
                parents[root(i)] = root(j)
    groups = {}
    for i, r in enumerate(rows):
        groups.setdefault(root(i), []).append(r)
    excluded = Counter()
    for group in groups.values():
        gid = hashlib.sha256("\n".join(sorted(r["id"] for r in group)).encode()).hexdigest()
        public_final = any(r["split"] in ["evaluation", "transfer"] for r in group)
        ambiguous = len({r["label"] for r in group if r["split"] == "unassigned"}) > 1
        fraction = int(gid[:8], 16) / 2**32
        partition = "train" if fraction < 0.7 else ("calibration" if fraction < 0.85 else "validation")
        seen = set()
        for r in group:
            r["group"] = gid
            if r["split"] != "unassigned":
                continue
            reason = "overlaps_final_lexically" if public_final else ("conflicting_group_labels" if ambiguous else None)
            canonical = " ".join(re.findall(r"\w+", r["text"].casefold()))
            if canonical in seen:
                reason = "duplicate_development_text"
            seen.add(canonical)
            if reason:
                r["split"] = "excluded"
                r["exclusion_reason"] = reason
                excluded[reason] += 1
            else:
                r["split"] = partition
    path = args.output / "rows.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows if r["split"] != "excluded"))
    counts = {
        s: dict(Counter(str(r["label"]) for r in rows if r["split"] == s))
        for s in ["train", "calibration", "validation", "evaluation", "transfer"]
    }
    for s in ["train", "calibration", "validation", "evaluation"]:
        assert int(counts[s].get("0", 0)) >= 10 and int(counts[s].get("1", 0)) >= 10, ("Insufficient labels", s)
    manifest = {
        "schema_version": 1,
        "target": "public input injection label; not action permission or source influence",
        "sources": upstream["sources"],
        "upstream_manifest_sha256": sha(args.inputs / "manifest.json"),
        "files": {"rows.jsonl": sha(path)},
        "counts": counts,
        "excluded": dict(excluded),
        "preparer_sha256": sha(Path(__file__)),
        "pyarrow_version": __import__("pyarrow").__version__,
        "splitting": (
            "Connected word-trigram Jaccard >=0.5 or normalized exact match; "
            "final-overlap development groups excluded; SHA256 group assignment 70/15/15"
        ),
        "group_limitation": (
            "Lexical deduplication does not establish independent attack-family "
            "or workflow holdout. Public pretraining contamination unknown."
        ),
        "licenses": {
            "base": "Apache-2.0 upstream model declaration",
            "deepset": (
                "Card declares Apache-2.0 at top level and CC-BY-4.0 in "
                "dataset_info; preserve both declarations and attribution. No model redistribution approved."
            ),
            "notinject": "MIT; Hao Li and Xiaogeng Liu, NotInject / PIGuard",
        },
        "protocol": {
            "seed": 31,
            "max_tokens": 256,
            "epochs": 6,
            "batch_size": 8,
            "learning_rate": 2e-5,
            "weight_decay": 0.01,
            "frozen_steps": 300,
            "frozen_learning_rate": 0.01,
            "validation_false_block_budget": 0.01,
            "evaluation_recall_minimum": 0.90,
            "evaluation_false_block_maximum": 0.01,
            "transfer_false_block_maximum": 0.01,
            "coverage_minimum": 0.95,
            "selection": (
                "Fixed settings before first model run; calibration-only temperature and validation-only "
                "threshold. Neither final set is used for fitting or selection."
            ),
            "status": "experimental_not_approved",
            "enforcement_approved": False,
        },
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"counts": counts, "excluded": dict(excluded), "rows_sha256": sha(path)}))


if __name__ == "__main__":
    main()
