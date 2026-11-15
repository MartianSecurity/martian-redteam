"""Prompt versions: a prompt's text kept as immutable versions, and the runs that used each one.

    martian evals prompts push evals.yaml -m "Shorter greeting"

A prompt is named, and each change of its text is a new version with a commit message, its author
and its time (:class:`~martian.evals.model.PromptVersion`), kept in the evals core's store: in
MLflow's prompt registry on the in-process binding. A prompt belongs to the organisation, or to the
single local user, not to a project.

- **From a suite file.** A suite file's prompts are the text values of its matrix
  (:mod:`martian.evals.matrix`). :func:`push_suite_prompts` registers each text value of one axis,
  ``prompt`` unless another is named, as a version of the prompt named by the value's name, in the
  local Evals UI's data folder. A value whose text is the prompt's latest version adds nothing.
- **A run's prompt version.** When ``martian evals run`` records a combination of a matrix, each of
  its text values whose content digest (:func:`~martian.evals.model.prompt_digest`) is a registered
  version's is recorded in the run's ``provenance.prompts`` (:func:`matched_prompts`): the axis, the
  value's name, and the prompt's name, version and digest. A run is matched when it is recorded, so
  a run recorded before its prompt's text was pushed names no version.
- A suite file does not name a version (``name@version``): the suite stays the whole definition of
  what it runs, and runs the same on a machine with no registry, such as a CI runner.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from martian.config import ConfigError
from martian.evals.matrix import axis_values
from martian.evals.model import PROMPT_NAME, PromptVersion, prompt_digest

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal
    from martian.evals.store import EvalStore

PROMPT_AXIS = "prompt"
"""The matrix axis :func:`push_suite_prompts` reads unless it is given another."""

PROVENANCE_KEY = "prompts"
"""The key of a run's provenance that names the prompt versions it used."""

_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Pushed:
    """One prompt pushed: its version, and whether this push added it (``False`` when the prompt's
    latest version already had the text)."""

    version: PromptVersion
    added: bool


async def push_prompt(
    store: EvalStore, principal: Principal, name: str, text: str, *, message: str = "", create: bool = True
) -> Pushed:
    """Register ``text`` as the next version of the prompt ``name``, unless its latest version has
    the same text, in which case that version is returned with ``added=False``. With ``create``,
    a prompt that does not exist yet is created first; without it, it is ``not_found``."""
    if create and await store.get_prompt(principal, name) is None:
        await store.create_prompt(principal, name)
    versions = await store.prompt_versions(principal, name)
    if versions and versions[-1].digest == prompt_digest(text):
        return Pushed(versions[-1], added=False)
    return Pushed(await store.add_prompt_version(principal, name, text, message=message), added=True)


def suite_prompts(path: str | Path, *, axis: str = PROMPT_AXIS, values: Sequence[str] = ()) -> list[tuple[str, str]]:
    """The prompts of a suite file: each value of the matrix axis ``axis``, or only those named in
    ``values``, as its name and its text, in the file's order. Raises ``ConfigError`` naming what to
    change: a suite with no such axis, a value that is not text, or a name that is not a prompt's."""
    from martian.evals import runner

    suite, _ = runner.load_suite(path)
    if axis not in suite.matrix:
        named = ", ".join(suite.matrix) or "none"
        raise ConfigError(f"the suite file's matrix has no axis {axis!r} (its axes: {named}); name one with --axis")
    pairs = axis_values(axis, suite.matrix[axis])
    if unknown := [name for name in values if name not in dict(pairs)]:
        raise ConfigError(f"matrix.{axis} has no value named {unknown[0]!r}")
    found = []
    for name, value in pairs:
        if values and name not in values:
            continue
        if not isinstance(value, str) or not value:
            raise ConfigError(f"matrix.{axis}.{name} is not text, so it is not a prompt")
        if not re.fullmatch(PROMPT_NAME, name):
            raise ConfigError(
                f"matrix.{axis}: {name!r} cannot name a prompt, whose name is 1 to 64 letters, digits and . _ -, "
                "starting with a letter or digit; rename the value"
            )
        found.append((name, value))
    return found


def push_suite_prompts(
    path: str | Path,
    *,
    axis: str = PROMPT_AXIS,
    values: Sequence[str] = (),
    message: str = "",
    home: str | Path | None = None,
) -> list[Pushed]:
    """Push each prompt of the suite file (:func:`suite_prompts`) to the local Evals UI's data
    folder (``home``, else ``MARTIAN_EVALS_HOME``, else ``~/.martian/evals``), as the single local
    user, each with the commit message ``message``. Returns each prompt's version, in the file's
    order."""
    from martian.evals.local import LocalEditionError, data_folder, open_stores
    from martian.evals.server.principal import SINGLE_USER

    prompts = suite_prompts(path, axis=axis, values=values)
    try:
        store, records = open_stores(data_folder(home))
    except LocalEditionError as error:
        raise ConfigError(str(error)) from None
    records.close()

    async def push() -> list[Pushed]:
        return [await push_prompt(store, SINGLE_USER, name, text, message=message) for name, text in prompts]

    return asyncio.run(push())


async def matched_prompts(
    store: EvalStore, principal: Principal, matrix: Mapping[str, Any], names: Mapping[str, str]
) -> list[dict[str, Any]]:
    """What a run of one combination of ``matrix`` records under ``provenance.prompts``: for each
    axis, in the matrix's order, whose value in the combination (``names``, each axis's value name)
    is text whose digest is a registered prompt version's, the ``axis``, the ``value``'s name and
    the version's prompt ``name``, ``version`` and ``digest``. When several versions have the text,
    the newest of the prompt named as the value is taken, else the newest of any prompt."""
    found = []
    for axis, given in matrix.items():
        value = dict(axis_values(axis, given)).get(names.get(axis, ""))
        if not isinstance(value, str) or not value:
            continue
        versions = await store.find_prompt_versions(principal, prompt_digest(value))
        if not versions:
            continue
        name = names[axis]
        version = max(versions, key=lambda v: (v.name == name, v.created_at or _EPOCH, v.version))
        found.append(
            {"axis": axis, "value": name, "name": version.name, "version": version.version, "digest": version.digest}
        )
    return found


__all__ = [
    "PROMPT_AXIS",
    "PROVENANCE_KEY",
    "Pushed",
    "matched_prompts",
    "push_prompt",
    "push_suite_prompts",
    "suite_prompts",
]
