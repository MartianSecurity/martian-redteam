"""Harmful-content prompts from permissively licensed datasets, downloaded when a run first uses them.

The harmful-content set (``HarmfulContent``) sends prompts from public datasets instead of asking the
attacker model to write them: a user's model may refuse to write such prompts, and several well-known
datasets forbid commercial use. ``register.json`` beside this module is the licence register: each
dataset's licence, source, citation, an address pinned to one commit and the file's sha256. No row of
any dataset is in the package. A run downloads a dataset the first time it uses it, checks the digest
and keeps the file in the user's cache (``MARTIAN_DATASET_CACHE``, else ``~/.cache/martian/redteam-datasets``).

The commercial-use switch (the run file's ``datasets.commercial_use``, on by default) lets only
datasets under :data:`COMMERCIAL_LICENCES` load: MIT, Apache-2.0 and CC BY. A dataset file of the
user's own states its licence and is held to the same rule.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

REGISTER_PATH = Path(__file__).with_name("register.json")
CACHE_ENV = "MARTIAN_DATASET_CACHE"
MAX_BYTES = 20 * 1024 * 1024
"""The largest dataset file a run downloads."""
DOWNLOAD_TIMEOUT_SECONDS = 60


class DatasetError(ValueError):
    """A dataset that cannot be used: not permitted, not reachable, not the file the register pins."""


class Dataset(BaseModel):
    """One registered dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    licence: str
    source: str
    citation: str
    url: str = Field(pattern=r"^https://")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    column: str
    where: dict[str, str] = Field(default_factory=dict)
    rows: int = Field(ge=1)


def _register() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    return loaded


_LOADED = _register()
COMMERCIAL_LICENCES: frozenset[str] = frozenset(_LOADED["commercial_licences"])
"""The licences a dataset may have when the commercial-use switch is on."""
DATASETS: Mapping[str, Dataset] = {
    identifier: Dataset.model_validate(item) for identifier, item in _LOADED["datasets"].items()
}
"""Every registered dataset, by id: each is a type of ``HarmfulContent``."""
FILE = "file"
"""The ``HarmfulContent`` type of a dataset file of the user's own."""


def permitted(licence: str, *, commercial_use: bool) -> bool:
    """Whether a dataset under ``licence`` may load. With the switch off, any licence may."""
    return not commercial_use or licence in COMMERCIAL_LICENCES


def check_licence(name: str, licence: str | None, *, commercial_use: bool) -> None:
    """Refuse a dataset the switch excludes, naming it and its licence."""
    if licence is None:
        raise DatasetError(f"{name} states no licence; give its licence, such as MIT or CC-BY-4.0")
    if not permitted(licence, commercial_use=commercial_use):
        allowed = ", ".join(sorted(COMMERCIAL_LICENCES))
        raise DatasetError(
            f"{name} is under {licence}, which commercial use excludes; only {allowed} load with "
            "datasets.commercial_use on"
        )


def cache_dir(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    given = environ.get(CACHE_ENV)
    return Path(given) if given else Path.home() / ".cache" / "martian" / "redteam-datasets"


def rows_of(data: bytes, *, column: str, where: Mapping[str, str] | None = None, suffix: str = ".csv") -> list[str]:
    """The prompts of a dataset file: the ``column`` of each CSV row, or of each JSON Lines object
    (``suffix`` ``.jsonl``), whose ``where`` columns hold the given values. Empty prompts are left out."""
    text = data.decode("utf-8-sig")
    if suffix == ".jsonl":
        records: list[Mapping[str, Any]] = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        records = list(csv.DictReader(io.StringIO(text)))
    if records and column not in records[0]:
        raise DatasetError(f"the dataset has no column {column!r}")
    wanted = dict(where or {})
    return [
        str(record[column]).strip()
        for record in records
        if all(str(record.get(name, "")) == value for name, value in wanted.items()) and str(record[column]).strip()
    ]


async def _download(url: str) -> bytes:
    import httpx

    async with (
        httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False, trust_env=True) as client,
        client.stream("GET", url) as response,
    ):
        if response.status_code != 200:
            raise DatasetError(f"downloading the dataset answered HTTP {response.status_code}")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise DatasetError("the dataset is larger than a run downloads")
        return bytes(body)


async def load(
    identifier: str, *, commercial_use: bool = True, cache: Path | None = None, download: Any = None
) -> list[str]:
    """The prompts of the registered dataset ``identifier``: from the cache when its file is there and
    has the register's digest, else downloaded from the pinned address, checked and cached. A file
    with another digest is refused, never used. ``download`` replaces the download (the tests)."""
    if identifier not in DATASETS:
        raise DatasetError(f"no registered dataset {identifier!r}")
    dataset = DATASETS[identifier]
    check_licence(identifier, dataset.licence, commercial_use=commercial_use)
    folder = cache or cache_dir()
    path = folder / f"{identifier}-{dataset.sha256[:12]}.csv"
    data = path.read_bytes() if path.is_file() else None
    if data is None or hashlib.sha256(data).hexdigest() != dataset.sha256:
        try:
            data = await (download or _download)(dataset.url)
        except DatasetError:
            raise
        except Exception as error:  # noqa: BLE001 - any failure to fetch is the dataset's, named by its type
            raise DatasetError(f"the dataset {identifier} could not be downloaded: {type(error).__name__}") from None
        if hashlib.sha256(data).hexdigest() != dataset.sha256:
            raise DatasetError(f"the dataset {identifier} is not the file the register pins: its digest differs")
        folder.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        partial.write_bytes(data)
        partial.replace(path)
    return rows_of(data, column=dataset.column, where=dataset.where)


def load_file(
    path: str | Path,
    *,
    column: str,
    licence: str | None,
    where: Mapping[str, str] | None = None,
    commercial_use: bool = True,
) -> list[str]:
    """The prompts of a dataset file of the user's own (CSV, or JSON Lines by its ``.jsonl`` suffix),
    once its licence passes the switch."""
    file = Path(path)
    check_licence(file.name, licence, commercial_use=commercial_use)
    try:
        data = file.read_bytes()
    except OSError as error:
        raise DatasetError(f"the dataset file {file.name} cannot be read: {type(error).__name__}") from None
    return rows_of(data, column=column, where=where, suffix=file.suffix.lower())


__all__ = [
    "CACHE_ENV",
    "COMMERCIAL_LICENCES",
    "DATASETS",
    "FILE",
    "REGISTER_PATH",
    "Dataset",
    "DatasetError",
    "cache_dir",
    "check_licence",
    "load",
    "load_file",
    "permitted",
    "rows_of",
]
