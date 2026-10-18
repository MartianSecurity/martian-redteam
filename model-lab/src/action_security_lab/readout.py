"""Offline experimental readout of a pinned release. It never authorizes execution."""

from __future__ import annotations

import json
import math
from contextlib import nullcontext
from pathlib import Path

import torch
from safetensors.torch import load_file
from torch.nn.attention import SDPBackend, sdpa_kernel
from transformers import AutoModel, AutoTokenizer

from .contracts import ActionContext, serialize
from .dataset import digest
from .scoring import OperatingPoint


class ScopeReadout:
    def __init__(self, directory: Path, expected_manifest_sha256: str, *, device: str, attention: str = "sdpa_math"):
        if device not in {"cpu", "cuda"} or (device == "cuda" and not torch.cuda.is_available()):
            raise ValueError("Requested device is unavailable")
        if attention not in {"sdpa_math", "sdpa_efficient", "eager"}:
            raise ValueError("Unknown attention implementation")
        if attention == "sdpa_efficient" and device != "cuda":
            raise ValueError("Efficient attention requires CUDA")
        self.attention = attention
        if digest(directory / "release-manifest.json") != expected_manifest_sha256:
            raise ValueError("Release manifest does not match the operator-pinned digest")
        self.manifest = json.loads((directory / "release-manifest.json").read_text())
        if (
            self.manifest["schema_version"] not in {1, 2}
            or self.manifest["status"] != "experimental_not_approved"
            or self.manifest["production_enforcement"] is not False
            or self.manifest["supported_rule"] != "task_scope"
        ):
            raise ValueError("Unsupported experimental release")
        required = {
            "operating-point.json",
            "full_context.safetensors",
            "encoder/config.json",
            "encoder/model.safetensors",
            "encoder/tokenizer.json",
            "encoder/tokenizer_config.json",
        }
        if not required.issubset(self.manifest["files"]):
            raise ValueError("Release omits required artifact checksums")
        for name, expected in self.manifest["files"].items():
            path = directory / name
            if (
                Path(name).is_absolute()
                or ".." in Path(name).parts
                or any(p.is_symlink() for p in (path, *path.parents))
            ):
                raise ValueError("Invalid artifact path")
            if digest(path) != expected:
                raise ValueError("Artifact checksum mismatch")
        self.operating_point = json.loads((directory / "operating-point.json").read_text())
        if self.operating_point["enforcement_approved"] is not False:
            raise ValueError("Enforcing readouts are not supported by this lab")
        if self.manifest["schema_version"] == 2:
            self.operating_point = OperatingPoint.model_validate(self.operating_point).model_dump(mode="json")
        elif set(self.operating_point) != {"temperature", "threshold", "enforcement_approved"}:
            raise ValueError("Invalid historical operating point")
        self.device = torch.device(device)
        self.tokenizer = AutoTokenizer.from_pretrained(
            directory / "encoder", local_files_only=True, trust_remote_code=False
        )
        self.encoder = AutoModel.from_pretrained(
            directory / "encoder",
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            attn_implementation="eager" if attention == "eager" else "sdpa",
            dtype=torch.float32,
        ).to(self.device)
        self.head = torch.nn.Linear(self.encoder.config.hidden_size, 1).to(self.device)
        self.head.load_state_dict(load_file(str(directory / "full_context.safetensors"), device=device))
        self.encoder.eval().requires_grad_(False)
        self.head.eval().requires_grad_(False)

    def attention_context(self):
        if self.attention == "eager":
            return nullcontext()
        backend = SDPBackend.MATH if self.attention == "sdpa_math" else SDPBackend.EFFICIENT_ATTENTION
        return sdpa_kernel([backend])

    def score(self, context: ActionContext, *, max_tokens: int = 512) -> tuple[float, int]:
        ids = serialize(context, self.tokenizer, max_tokens=max_tokens)
        length = len(ids)
        # Efficient SDPA's additive-mask row stride must be aligned. Padding is
        # explicitly masked and never removes source/task/action tokens.
        padding = (-length) % 8 if self.attention == "sdpa_efficient" else 0
        encoded = {
            "input_ids": torch.tensor([ids + [self.tokenizer.pad_token_id] * padding], device=self.device),
            "attention_mask": torch.tensor([[1] * length + [0] * padding], dtype=torch.long, device=self.device),
        }
        with torch.inference_mode(), self.attention_context():
            hidden = self.encoder(**encoded).last_hidden_state
            mask = encoded["attention_mask"].unsqueeze(-1)
            logit = self.head((hidden * mask).sum(1) / mask.sum(1)).squeeze()
        value = float(logit)
        if not math.isfinite(value):
            raise ValueError("Nonfinite model output")
        return value, length
