"""Retrieve only pinned public model/data bytes for the isolated NX experiment."""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

SOURCES = {
    "base": (
        "models",
        "sentence-transformers/all-MiniLM-L6-v2",
        "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
        [
            "model.safetensors",
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "vocab.txt",
            "README.md",
        ],
    ),
    "public": (
        "datasets",
        "deepset/prompt-injections",
        "4f61ecb038e9c3fb77e21034b22511b523772cdd",
        [
            "README.md",
            "data/train-00000-of-00001-9564e8b05b4757ab.parquet",
            "data/test-00000-of-00001-701d16158af87368.parquet",
        ],
    ),
    "transfer": (
        "datasets",
        "leolee99/NotInject",
        "847ae76cf8fea5ed325429e569ae8cfef022d2e0",
        ["README.md", *["data/NotInject_" + s + "-00000-of-00001.parquet" for s in ["one", "two", "three"]]],
    ),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "purpose": "private local research; no redistribution approval",
        "sources": {},
        "files": {},
    }
    for key, (kind, repo, revision, files) in SOURCES.items():
        with urlopen(
            f"https://huggingface.co/api/{kind}/{repo}/tree/{revision}?recursive=true", timeout=30
        ) as response:
            entries = {r["path"]: r for r in json.load(response)}
        manifest["sources"][key] = {"repository": repo, "revision": revision}
        for name in files:
            entry = entries[name]
            if not 0 < entry["size"] < 100_000_000:
                raise ValueError("Unexpected pinned file size")
            prefix = "datasets/" if kind == "datasets" else ""
            with urlopen(
                f"https://huggingface.co/{prefix}{repo}/resolve/{revision}/{name}?download=true", timeout=60
            ) as response:
                data = response.read(entry["size"] + 1)
            if len(data) != entry["size"]:
                raise ValueError("Pinned file size mismatch")
            sha = hashlib.sha256(data).hexdigest()
            if "lfs" in entry:
                valid = sha == entry["lfs"]["oid"]
            else:
                valid = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == entry["oid"]
            if not valid:
                raise ValueError("Pinned file digest mismatch")
            relative = key + "/" + name
            path = args.destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            manifest["files"][relative] = {"sha256": sha, "bytes": len(data)}
    (args.destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"files": len(manifest["files"]), "bytes": sum(x["bytes"] for x in manifest["files"].values())}))


if __name__ == "__main__":
    main()
