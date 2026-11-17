"""The attack methods a version 2 run file can name, and the transforms it can layer.

``attacks`` entries name a method (PascalCase, or its snake_case id, as the catalogue's lookup reads
names) and may add ``layers`` and the method's own settings. :func:`resolve` turns an entry into a
configured :class:`~martian.redteam.methods.base.AttackMethod`; :func:`register_method` and
:func:`register_transform` add methods and transforms defined outside the package.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from martian.redteam.methods.base import AttackMethod, Transform
from martian.redteam.methods.rewrites import Rewrite
from martian.redteam.methods.transforms import TRANSFORMS
from martian_redteam.catalog.attacks import ATTACKS

if TYPE_CHECKING:
    from martian.redteam.runfile import AttackSpec, RunFile

MethodFactory = Callable[["AttackSpec", tuple[Transform, ...], dict[str, Any]], AttackMethod]
"""Builds a method from its ``attacks`` entry, its resolved layers and the settings the entry sets."""

SPEC_FIELDS = frozenset({"name", "types", "weight", "id", "criteria", "gated", "layers"})
"""The fields of an ``attacks`` entry that are not a method's own settings."""

LEGACY = ("prompt_injection", "roleplay", "prompt_probing", "rot13", "base64", "leetspeak")
"""The six version 0.2 attack methods."""


class MethodError(ValueError):
    """An ``attacks`` entry that names no method or transform, or sets what its method does not take."""


@dataclass(frozen=True)
class _Entry:
    name: str
    factory: MethodFactory


_METHODS: dict[str, _Entry] = {}
_TRANSFORMS: dict[str, Transform] = {}


def _key(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def register_method(name: str, factory: MethodFactory, *, aliases: Iterable[str] = ()) -> None:
    """Make ``name`` (and each alias) an attack method of version 2 run files. A name already taken
    is refused."""
    entry = _Entry(name, factory)
    keys = {_key(name), *(_key(alias) for alias in aliases)}
    if taken := sorted(key for key in keys if key in _METHODS):
        raise ValueError(f"attack method {taken[0]} is already registered")
    for key in keys:
        _METHODS[key] = entry


def unregister_method(name: str) -> None:
    """Remove ``name`` and its aliases."""
    entry = _METHODS.get(_key(name))
    for key in [key for key, found in _METHODS.items() if found is entry]:
        del _METHODS[key]


def register_transform(transform: Transform) -> None:
    """Make ``transform`` a layer, under its id and its name. A name already taken is refused."""
    keys = {_key(transform.id), _key(transform.name)}
    if taken := sorted(key for key in keys if key in _TRANSFORMS):
        raise ValueError(f"transform {taken[0]} is already registered")
    for key in keys:
        _TRANSFORMS[key] = transform


def transform(name: str) -> Transform:
    found = _TRANSFORMS.get(_key(name))
    if found is None:
        raise MethodError(f"unknown transform: {name}")
    return found


def method_names() -> list[str]:
    """Every registered method's name, in the order registered."""
    return list(dict.fromkeys(entry.name for entry in _METHODS.values()))


def all_transforms() -> list[Transform]:
    return list({id(item): item for item in _TRANSFORMS.values()}.values())


def settings_of(spec: AttackSpec) -> dict[str, Any]:
    """The method settings an ``attacks`` entry sets."""
    return {key: value for key, value in spec.model_dump(exclude_none=True).items() if key not in SPEC_FIELDS}


def resolve(spec: AttackSpec) -> AttackMethod:
    """The configured method of an ``attacks`` entry. Raises :class:`MethodError` when it names no
    method, a layer that is no transform, or a setting its method does not take."""
    entry = _METHODS.get(_key(spec.name))
    if entry is None:
        raise MethodError(f"unknown attack method: {spec.name}")
    layers = tuple(transform(name) for name in spec.layers)
    values = settings_of(spec)
    method = entry.factory(spec, layers, values)
    if extra := sorted(set(values) - set(method.settings)):
        takes = ", ".join(sorted(method.settings)) or "no settings"
        raise MethodError(f"{extra[0]} does not apply to {entry.name}, which takes {takes}")
    if layers:
        method.id = "+".join([method.id, *(layer.id for layer in layers)])
    if spec.id:
        method.id = spec.id
    method.legacy = method.id in LEGACY and not layers and type(method) is Rewrite
    return method


def methods_of(run_file: RunFile) -> list[AttackMethod]:
    return [resolve(spec) for spec in run_file.attacks]


def needs_attacker(run_file: RunFile) -> bool:
    """Whether any of the run file's methods calls the attacker model."""
    return any(method.needs_attacker for method in methods_of(run_file))


# --- the built-in methods -----------------------------------------------------------------------


def _catalogue_rewrite(style: str) -> MethodFactory:
    def make(spec: AttackSpec, layers: tuple[Transform, ...], values: dict[str, Any]) -> AttackMethod:
        label = next(factory().label for factory in ATTACKS.values() if factory().id == style)
        return Rewrite(id=style, label=label, style=style, layers=layers, values=values)

    return make


def _transform_attack(item: Transform) -> MethodFactory:
    def make(spec: AttackSpec, layers: tuple[Transform, ...], values: dict[str, Any]) -> AttackMethod:
        return Rewrite(
            id=item.id,
            label=item.label,
            style="prompt_injection",
            layers=(item, *layers),
            values=values,
            kind="transform",
        )

    return make


for _item in TRANSFORMS.values():
    register_transform(_item)
_CATALOGUE = (("PromptInjection", "prompt_injection"), ("Roleplay", "roleplay"), ("PromptProbing", "prompt_probing"))
for _name, _style in _CATALOGUE:
    register_method(_name, _catalogue_rewrite(_style), aliases=[_style])
for _item in TRANSFORMS.values():
    register_method(_item.name, _transform_attack(_item), aliases=[_item.id])


__all__ = [
    "LEGACY",
    "MethodError",
    "MethodFactory",
    "method_names",
    "methods_of",
    "needs_attacker",
    "register_method",
    "register_transform",
    "resolve",
    "settings_of",
    "transform",
    "all_transforms",
    "unregister_method",
]
