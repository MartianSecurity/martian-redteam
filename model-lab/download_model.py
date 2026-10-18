"""Build-time download of an immutable public model; no remote Python code."""

import hashlib
import json
from pathlib import Path

from model_spec import MODEL_ID, MODEL_REVISION
from huggingface_hub import snapshot_download

target = Path("/opt/model")
snapshot_download(
    MODEL_ID,
    revision=MODEL_REVISION,
    local_dir=target,
    allow_patterns=[
        "config.json",
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
    ],
)
files = {p.name: hashlib.file_digest(p.open("rb"), "sha256").hexdigest() for p in target.iterdir() if p.is_file()}
assert "model.safetensors" in files and "tokenizer.json" in files
(target / "martian-model-manifest.json").write_text(
    json.dumps(
        {
            "model_id": MODEL_ID,
            "revision": MODEL_REVISION,
            "license": "Apache-2.0",
            "source": f"https://huggingface.co/{MODEL_ID}/tree/{MODEL_REVISION}",
            "sha256": files,
        },
        indent=2,
    )
)
