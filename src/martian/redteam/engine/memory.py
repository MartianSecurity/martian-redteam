"""The local attack memory: a strategy library learned from the user's own runs.

A run file with ``memory.strategies`` reads and updates a folder of plain files
(``<data folder>/redteam/strategies`` by default, :func:`default_folder`):

- ``strategy-<id>.json``, one per strategy: an attack that reached a confirmed success (evidence
  level 2 or above, decided by code) on a probe's objective, with the method that found it and where.
- ``stats.csv``, a table with one row per strategy, objective and target fingerprint: attempts,
  confirmed successes and when it was last used.

Deleting a strategy file forgets it (its rows are dropped when the table is next read); deleting the
folder forgets everything. Nothing in it leaves the machine.

A search method (``Refine``, ``TreeSearch``) of a later run on the same objective sends the picked
strategies as its first attacks, instead of asking the attacker for them, and refines from them when
they do not hit. :meth:`StrategyLibrary.pick` chooses them: with chance ``explore`` none (the method
writes its own), else up to three whose success on this target, ``(successes + 1) / (attempts + 2)``,
is at least one half, best first. The draw is seeded by the run's memory seed and the attempt, so a
rerun with the same seed and the same files picks the same strategies.

The table is updated when a run ends, only from attempts the checks decided by code: a strategy
counts a success when the query that sent it hit at level 2 or above and the probe's negative control
did not show the same signal. A hit only the judge found trains nothing. The attacker's text is kept
with the run's canary and receipt address replaced by placeholders and secret values scrubbed; the
target's replies are never kept.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from martian.redteam.model import Probe

STATS_FILE = "stats.csv"
STRATEGY_PREFIX = "strategy-"
COLUMNS = ("kind", "name", "objective", "target", "attempts", "successes", "last_seen")
PICKED = 3
"""The most strategies one attempt is given."""
CANARY = "{{canary}}"
RECEIPT = "{{receipt_url}}"

RowKind = Literal["strategy", "method"]


class StrategyLibraryError(ValueError):
    """The strategy library's folder holds a file that cannot be read."""


def default_folder() -> Path:
    """``<data folder>/redteam/strategies``: the data folder is ``MARTIAN_EVALS_HOME``, else
    ``~/.martian/evals``, as :func:`martian.evals.local.data_folder` reads it."""
    home = os.environ.get("MARTIAN_EVALS_HOME") or (Path.home() / ".martian" / "evals")
    return Path(home).expanduser().resolve() / "redteam" / "strategies"


def objective_key(probe: Probe) -> str:
    """What a strategy is for: the probe's vulnerability and type, and a digest of its goal."""
    key = f"{probe.vulnerability}/{probe.vulnerability_type}"
    return key + (f"#{hashlib.sha256(probe.goal.encode()).hexdigest()[:8]}" if probe.goal else "")


def target_key(block: Any) -> str:
    """The target's fingerprint: a digest of its run-file block without its headers and timeout, so
    a changed key or timeout is the same target."""
    data = block.model_dump(mode="json", exclude={"headers", "timeout_seconds"})
    if isinstance(data.get("mcp"), dict):
        data["mcp"].pop("headers", None)
    text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return "t-" + hashlib.sha256(text.encode()).hexdigest()[:16]


def score(attempts: int, successes: int) -> float:
    """The success rate with one success and one failure assumed: one half when nothing is known."""
    return (successes + 1) / (attempts + 2)


@dataclass
class Strategy:
    """An attack that once reached a confirmed success on ``objective``."""

    id: str
    objective: str
    vulnerability: str
    vulnerability_type: str
    goal: str
    method: str
    attack: str
    created_at: str
    found_in: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Row:
    attempts: int = 0
    successes: int = 0
    last_seen: str = ""


class StrategyLibrary:
    """The strategy library in ``folder``: read when the run starts, written when it ends."""

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder).expanduser()
        self.strategies: dict[str, Strategy] = {}
        self.rows: dict[tuple[str, str, str, str], _Row] = {}
        self._added: dict[str, Strategy] = {}
        self._counted: dict[tuple[str, str, str, str], _Row] = {}
        self._forgotten: set[tuple[str, str, str, str]] = set()
        self.load()

    # --- reading ----------------------------------------------------------------------------------

    def load(self) -> None:
        self.strategies = {item.id: item for item in self._read_strategies()}
        rows = self._read_rows()
        self._forgotten = {key for key in rows if key[0] == "strategy" and key[1] not in self.strategies}
        """Rows of strategies whose file the user deleted: dropped from the table when it is written."""
        self.rows = {key: row for key, row in rows.items() if key not in self._forgotten}

    def _read_strategies(self) -> list[Strategy]:
        if not self.folder.is_dir():
            return []
        found = []
        for path in sorted(self.folder.glob(f"{STRATEGY_PREFIX}*.json")):
            try:
                found.append(Strategy(**json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, TypeError) as error:
                raise StrategyLibraryError(
                    f"{path.name} in the strategy library is not a strategy ({type(error).__name__}); "
                    "fix it or delete it"
                ) from None
        return found

    def _read_rows(self) -> dict[tuple[str, str, str, str], _Row]:
        path = self.folder / STATS_FILE
        if not path.is_file():
            return {}
        rows: dict[tuple[str, str, str, str], _Row] = {}
        reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise StrategyLibraryError(
                f"{STATS_FILE} in the strategy library must have the columns {', '.join(COLUMNS)}"
            )
        for number, item in enumerate(reader, start=2):
            try:
                key = (item["kind"], item["name"], item["objective"], item["target"])
                rows[key] = _Row(int(item["attempts"]), int(item["successes"]), item["last_seen"])
            except (TypeError, ValueError):
                raise StrategyLibraryError(f"{STATS_FILE} line {number} cannot be read; fix it or delete it") from None
        return rows

    def row(self, kind: RowKind, name: str, objective: str, target: str) -> _Row:
        """What the table, with this run's updates, says about one strategy or method."""
        key = (kind, name, objective, target)
        stored, counted = self.rows.get(key, _Row()), self._counted.get(key, _Row())
        return _Row(stored.attempts + counted.attempts, stored.successes + counted.successes, stored.last_seen)

    # --- picking ----------------------------------------------------------------------------------

    def pick(self, objective: str, target: str, *, rng: random.Random, explore: float) -> list[Strategy]:
        """The strategies one attempt sends first: none with chance ``explore``, else up to
        :data:`PICKED` for ``objective`` whose score on ``target`` is at least one half, best first."""
        if rng.random() < explore:
            return []
        ranked = []
        for strategy in self.strategies.values():
            if strategy.objective != objective:
                continue
            row = self.row("strategy", strategy.id, objective, target)
            value = score(row.attempts, row.successes)
            if value >= 0.5:
                ranked.append((-value, -row.successes, strategy.created_at, strategy.id, strategy))
        return [item[-1] for item in sorted(ranked)[:PICKED]]

    # --- learning ---------------------------------------------------------------------------------

    def count(self, kind: RowKind, name: str, objective: str, target: str, *, success: bool) -> None:
        """One attempt of a strategy or a method, and whether it was a confirmed success."""
        row = self._counted.setdefault((kind, name, objective, target), _Row())
        row.attempts += 1
        row.successes += int(success)
        row.last_seen = _now()

    def learn(self, probe: Probe, *, target: str, method: str, attack: str, found_in: dict[str, Any]) -> Strategy:
        """Keep ``attack``, a confirmed success on ``probe``'s objective, as a strategy (the same text
        for the same objective is the same strategy) and count its success on ``target``."""
        objective = objective_key(probe)
        identifier = hashlib.sha256(f"{objective}\0{attack}".encode()).hexdigest()[:16]
        strategy = self.strategies.get(identifier) or self._added.get(identifier)
        if strategy is None:
            strategy = Strategy(
                id=identifier,
                objective=objective,
                vulnerability=probe.vulnerability,
                vulnerability_type=probe.vulnerability_type,
                goal=probe.goal,
                method=method,
                attack=attack,
                created_at=_now(),
                found_in=found_in,
            )
            self._added[identifier] = strategy
        self.count("strategy", identifier, objective, target, success=True)
        return strategy

    @property
    def learned(self) -> int:
        return len(self._added)

    def save(self) -> None:
        """Write this run's strategies and counts, added to what the files hold now: another run
        may have written them since this one started."""
        if not self._added and not self._counted:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        for strategy in self._added.values():
            path = self.folder / f"{STRATEGY_PREFIX}{strategy.id}.json"
            if not path.exists():
                _write(path, json.dumps(asdict(strategy), indent=2, ensure_ascii=False) + "\n")
        rows = {key: row for key, row in self._read_rows().items() if key not in self._forgotten}
        for key, counted in self._counted.items():
            row = rows.setdefault(key, _Row())
            row.attempts += counted.attempts
            row.successes += counted.successes
            row.last_seen = max(row.last_seen, counted.last_seen)
        _write(self.folder / STATS_FILE, _table(rows))
        self.strategies.update(self._added)
        self._added, self._counted = {}, {}
        self.rows = rows


def _table(rows: dict[tuple[str, str, str, str], _Row]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for key in sorted(rows):
        row = rows[key]
        writer.writerow([*key, row.attempts, row.successes, row.last_seen])
    return out.getvalue()


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` whole: a reader sees the old file or the new one."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def placeholders(text: str, replacements: Iterable[tuple[str, str]]) -> str:
    """``text`` with each value replaced by its placeholder (or back), longest value first."""
    for value, placeholder in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if value:
            text = text.replace(value, placeholder)
    return text


__all__ = [
    "CANARY",
    "COLUMNS",
    "PICKED",
    "RECEIPT",
    "STATS_FILE",
    "Strategy",
    "StrategyLibrary",
    "StrategyLibraryError",
    "default_folder",
    "objective_key",
    "placeholders",
    "score",
    "target_key",
]
