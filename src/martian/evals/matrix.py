"""A suite's matrix: several prompts, models or other values, and a run of every combination.

    matrix:
      model: [small-model, large-model]     # a list: each value is its own name
      prompt:                               # a mapping: a short name for each value
        terse: "Answer in one sentence."
        friendly: "Greet the customer, then answer."

Each key of ``matrix`` is an **axis**, and its values are given as a list, each value naming
itself, or as a mapping of short names to values, for values too long or too varied to name a
combination, such as prompts. A **combination** takes one value of every axis; the suite runs once
per combination (:func:`combinations`, the first axis outermost), so two models and two prompts
are four runs.

- **Identity.** A combination's ``id`` is ``axis=name`` for each axis, in the axes' alphabetical
  order, joined by commas: ``model=large-model,prompt=terse``. It depends on the axes' and the
  values' names only, so reordering the axes, or changing a named value's text, keeps it; a later
  run's results of the same combination compare with this one's.
- **Names.** An axis name is a letter or ``_`` then letters, digits and ``_``, at most 40, so it
  can be a placeholder and a keyword argument. A value's name is 1 to 64 letters, digits and
  ``. _ : / @ + -``, so an id is plain text in a file name, a run name and a comment; a list value
  that is not such a name, such as a prompt, belongs in a mapping. A list value names itself as
  written: text as it is, a number as JSON writes it, ``true`` or ``false``.
- **Size.** At most :data:`MAX_COMBINATIONS` combinations, and an id of at most
  :data:`MAX_ID_LENGTH` characters.

Where the values go is the runner's (:mod:`martian.evals.runner`): a URL target's body takes
``{{matrix.NAME}}``, and a function target receives each axis as a keyword argument.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MatrixValue = str | int | float | bool
"""A value of an axis given as a list: it names itself, so it is a scalar."""

MatrixAxis = list[MatrixValue] | dict[str, Any]
"""One axis as a suite file writes it: a list of values, or a mapping of names to values."""

MAX_COMBINATIONS = 100
"""The most combinations a matrix has."""

MAX_ID_LENGTH = 200
"""The longest a combination's id is, so it fits a run name."""

AXIS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
VALUE_NAME = re.compile(r"[A-Za-z0-9._:/@+-]{1,64}")
# Characters of a value name that cannot be in a folder name on every system.
_UNSAFE_IN_FOLDER = re.compile(r"[/:]")


@dataclass(frozen=True)
class Combination:
    """One value of every axis. ``names`` and ``values`` map each axis to its value's name and to
    the value itself, in the matrix's axis order."""

    id: str
    names: dict[str, str]
    values: dict[str, Any]

    @property
    def folder(self) -> str:
        """The folder of this combination's results under a run's output folder: its id, with
        ``/`` and ``:`` written as ``_``."""
        return _UNSAFE_IN_FOLDER.sub("_", self.id)


def value_name(value: MatrixValue) -> str:
    """The name a list value gives itself."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value)


def combination_id(names: Mapping[str, str]) -> str:
    """``axis=name`` for each axis, in the axes' alphabetical order, joined by commas."""
    return ",".join(f"{axis}={names[axis]}" for axis in sorted(names))


def _shown(text: str) -> str:
    return repr(text if len(text) <= 40 else text[:39] + "…")


def axis_values(axis: str, given: MatrixAxis) -> list[tuple[str, Any]]:
    """Each value of one axis with its name, in order. Raises ``ValueError`` naming the axis."""
    if not AXIS_NAME.fullmatch(axis):
        raise ValueError(
            f"matrix: {_shown(axis)} is not an axis name; use letters, digits and _, starting with a letter "
            "or _, at most 40"
        )
    where = f"matrix.{axis}"
    named = isinstance(given, Mapping)
    if isinstance(given, Mapping):
        pairs = [(str(name), value) for name, value in given.items()]
    else:
        pairs = [(value_name(value), value) for value in given]
    if not pairs:
        raise ValueError(f"{where} has no values")
    seen: set[str] = set()
    for name, value in pairs:
        if not VALUE_NAME.fullmatch(name):
            rule = "1 to 64 letters, digits and . _ : / @ + -"
            if named:
                raise ValueError(f"{where}: {_shown(name)} is not a value name; a name is {rule}")
            raise ValueError(
                f"{where}: {_shown(name)} cannot name itself in a combination's id, whose names are {rule}; "
                f"give {where} as a mapping of short names to values"
            )
        if named and value is None:
            raise ValueError(f"{where}.{name} has no value")
        if name in seen:
            raise ValueError(f"{where} names {_shown(name)} twice")
        seen.add(name)
    return pairs


def combinations(matrix: Mapping[str, MatrixAxis]) -> list[Combination]:
    """Every combination of the matrix's values, the first axis outermost; none for an empty
    matrix. Raises ``ValueError`` for a matrix that cannot run, naming what to change."""
    axes = [(axis, axis_values(axis, given)) for axis, given in matrix.items()]
    if not axes:
        return []
    count = math.prod(len(values) for _, values in axes)
    if count > MAX_COMBINATIONS:
        raise ValueError(f"the matrix has {count} combinations; at most {MAX_COMBINATIONS}")
    found: list[Combination] = []
    folders: dict[str, str] = {}
    for picked in itertools.product(*(values for _, values in axes)):
        names = {axis: name for (axis, _), (name, _) in zip(axes, picked, strict=True)}
        values = {axis: value for (axis, _), (_, value) in zip(axes, picked, strict=True)}
        combination = Combination(id=combination_id(names), names=names, values=values)
        if len(combination.id) > MAX_ID_LENGTH:
            raise ValueError(
                f"a combination's id, such as {_shown(combination.id)}, is longer than {MAX_ID_LENGTH} characters; "
                "shorten the axis or value names"
            )
        if (other := folders.setdefault(combination.folder, combination.id)) != combination.id:
            raise ValueError(
                f"the combinations {other} and {combination.id} would share the results folder "
                f"{combination.folder}; rename one of the values"
            )
        found.append(combination)
    return found


def axes(matrix: Mapping[str, MatrixAxis]) -> list[dict[str, Any]]:
    """The axes and their values' names, in the matrix's order, as ``matrix.json`` and a run's
    ``provenance.matrix`` list them: ``[{"name": "model", "values": ["small", "large"]}, ...]``."""
    return [{"name": axis, "values": [name for name, _ in axis_values(axis, given)]} for axis, given in matrix.items()]


def provenance(combination: Combination, matrix: Mapping[str, MatrixAxis], invocation: str) -> dict[str, Any]:
    """What a combination's run records under ``provenance.matrix``: its ``id``, its ``values``
    (each axis's value name), the matrix's ``axes``, and ``invocation``, the id the runs of one
    ``martian evals run`` share. Value names only: a named value's text stays in the suite file."""
    return {"id": combination.id, "values": dict(combination.names), "axes": axes(matrix), "invocation": invocation}


__all__ = [
    "MAX_COMBINATIONS",
    "MAX_ID_LENGTH",
    "Combination",
    "MatrixAxis",
    "MatrixValue",
    "axes",
    "axis_values",
    "combination_id",
    "combinations",
    "provenance",
    "value_name",
]
