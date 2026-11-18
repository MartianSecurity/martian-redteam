"""Pinned dataset pull. Rows carry the Backend item id as a trace tag for the upload mapping."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from martian.config import ConfigError, MartianConfig
from martian.evals.client import Backend, BackendError
from martian.evals.config import CONFIG, KitConfig

ITEM_TAG = "martian_item_id"


@dataclass(frozen=True)
class Pin:
    dataset_id: str
    name: str
    items_digest: str
    truncated: bool
    cap: int


class EvalDataset(list[dict[str, Any]]):
    """MLflow `data` rows plus the pin Backend will check the upload against."""

    pin: Pin

    @property
    def item_ids(self) -> list[str]:
        return [row["tags"][ITEM_TAG] for row in self]


def _from_payload(payload: dict[str, Any]) -> EvalDataset:
    data = EvalDataset(
        {
            "inputs": {"message": item["prompt"]},
            "expectations": {"expected_response": item["expected"]},
            "tags": {ITEM_TAG: item["id"]},
        }
        for item in payload["items"]
    )
    data.pin = Pin(
        dataset_id=payload["dataset_id"],
        name=payload["name"],
        items_digest=payload["items_digest"],
        truncated=bool(payload.get("truncated", False)),
        cap=int(payload.get("cap", 0)),
    )
    if not data:
        raise ConfigError(f"dataset {payload['name']!r} has no usable items")
    return data


def load_dataset(
    name: str, *, config: MartianConfig | KitConfig | None = None, backend: Backend | None = None
) -> EvalDataset:
    """Pull the pinned dataset from Backend, or read a local fixture in offline mode."""
    config = config or CONFIG.get()
    if config is None:
        import os

        config = KitConfig.from_env() if os.environ.get("MARTIAN_EVALS_OFFLINE") == "1" else MartianConfig.from_env()
    if isinstance(config, KitConfig) and config.offline:
        if not config.dataset_file:
            raise ConfigError("offline mode needs MARTIAN_EVALS_DATASET_FILE")
        with open(config.dataset_file, encoding="utf-8") as handle:
            return _from_payload(json.load(handle))
    owned = backend is None
    connection = config.connection if isinstance(config, KitConfig) else config
    backend = backend or Backend(
        connection.api_url,
        connection.token.get_secret_value(),
        timeout=connection.request_timeout_seconds,
        trust_env=connection.trust_env,
    )
    try:
        return _from_payload(backend.pull_dataset(name))
    except BackendError:
        raise
    finally:
        if owned:
            backend.close()
