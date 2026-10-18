"""Download pinned public reference bytes; never load remote Python code."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

REVISION = "90c9989b1a342275dd0d1a95aad283c04e075671"
FILES = {
    "model.onnx": ("onnx/model.onnx", "f0ea7f239f765aedbde7c9e163a7cb38a79c5b8853d3f76db5152172047b228c"),
    "tokenizer.json": ("onnx/tokenizer.json", "752fe5f0d5678ad563e1bd2ecc1ddf7a3ba7e2024d0ac1dba1a72975e26dff2f"),
    "LICENSE": ("LICENSE", "59899c6091b540582ed617e8eeaac4919dc985ccfc35459ee9752b699be5205b"),
    "README.md": ("README.md", "37221a38df1e60ad56364c39a233c75b77fa08740ecdee248d1b4e07ccc8ef5a"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=False)
    manifest = {"model": "protectai/deberta-v3-base-prompt-injection-v2", "revision": REVISION, "files": {}}
    for name, (remote, expected) in FILES.items():
        path = args.destination / name
        hasher = hashlib.sha256()
        size = 0
        with urlopen(f"https://huggingface.co/{manifest['model']}/resolve/{REVISION}/{remote}", timeout=60) as response:
            with path.open("xb") as stream:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 800_000_000:
                        raise ValueError("Reference file exceeds its bounded download size")
                    hasher.update(chunk)
                    stream.write(chunk)
        if hasher.hexdigest() != expected:
            raise ValueError(f"Reference checksum mismatch: {name}")
        manifest["files"][name] = {"sha256": expected, "bytes": size, "source": remote}
    (args.destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
