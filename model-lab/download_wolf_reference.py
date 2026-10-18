"""Retrieve pinned public classifier data, without executing repository code."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

REPOSITORY = "patronus-studio/wolf-defender-prompt-injection-small"
REVISION = "bcab2eff97bcabd7227849639e2d0d7a61b46c92"
FILES = {
    "fp32.onnx": "onnx/onnx_fp32/model.onnx",
    "int8-int4.onnx": "onnx/int8_int4_embeddings/model.onnx",
    "tokenizer.json": "tokenizer.json",
    "config.json": "config.json",
    "tokenizer_config.json": "tokenizer_config.json",
    "special_tokens_map.json": "special_tokens_map.json",
    "quantization_manifest.json": "onnx/quantization_manifest.json",
    "LICENSE": "LICENSE",
    "README.md": "README.md",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=False)
    with urlopen(f"https://huggingface.co/api/models/{REPOSITORY}/tree/{REVISION}?recursive=true", timeout=30) as r:
        entries = {e["path"]: e for e in json.load(r)}
    manifest = {"repository": REPOSITORY, "revision": REVISION, "files": {}}
    for name, remote in FILES.items():
        entry = entries[remote]
        if not 0 < entry["size"] <= 600_000_000:
            raise ValueError("Pinned file is outside the download size limit")
        path = args.destination / name
        sha = hashlib.sha256()
        git = hashlib.sha1(b"blob " + str(entry["size"]).encode() + b"\0")
        size = 0
        with (urlopen(f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{remote}", timeout=60) as response,
              path.open("xb") as stream):
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > entry["size"]:
                    raise ValueError("Pinned size exceeded")
                sha.update(chunk)
                git.update(chunk)
                stream.write(chunk)
        expected = entry.get("lfs", {}).get("oid", entry["oid"])
        actual = sha.hexdigest() if "lfs" in entry else git.hexdigest()
        if size != entry["size"] or actual != expected:
            raise ValueError("Pinned download does not match its upstream object")
        manifest["files"][name] = {"sha256": sha.hexdigest(), "bytes": size, "source": remote}
        print(json.dumps({"file": name, "bytes": size, "verified": True}), flush=True)
    (args.destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
