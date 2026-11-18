"""Built-in vulnerability ids, types, and gate policy, from the catalogue entries (:mod:`.entries`)."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from martian_redteam.catalog.entries import CATALOG_VERSION as CATALOG_VERSION
from martian_redteam.catalog.entries import ENTRIES, CatalogEntry


class Vulnerability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    allowed_types: tuple[str, ...]
    gated: bool = False
    types: tuple[str, ...] = Field(default_factory=tuple)
    criteria: str | None = None

    def with_types(self, types: Sequence[str] | None) -> Vulnerability:
        selected = tuple(types) if types else self.allowed_types
        unknown = [item for item in selected if item not in self.allowed_types]
        if unknown:
            raise ValueError(f"{self.id} does not allow types {unknown}")
        return self.model_copy(update={"types": selected})


def _vuln(id: str, label: str, allowed: tuple[str, ...], *, gated: bool = False) -> Vulnerability:
    return Vulnerability(id=id, label=label, allowed_types=allowed, gated=gated, types=allowed)


def from_entry(entry: CatalogEntry, types: Sequence[str] | None = None) -> Vulnerability:
    """The catalogue entry as the vulnerability a probe runs, with ``types`` selected (all by default)."""
    return _vuln(entry.id, entry.label, entry.types, gated=entry.gated).with_types(types)


def _factory(name: str) -> Callable[..., Vulnerability]:
    item = ENTRIES[name]

    def make(types: Sequence[str] | None = None) -> Vulnerability:
        return from_entry(item, types)

    make.__name__ = make.__qualname__ = name
    make.__doc__ = f"{item.label}: the catalogue entry ``{item.id}``."
    return make


PromptLeakage = _factory("PromptLeakage")
ExcessiveAgency = _factory("ExcessiveAgency")
Toxicity = _factory("Toxicity")
IllegalActivity = _factory("IllegalActivity")
PIILeakage = _factory("PIILeakage")
Bias = _factory("Bias")
Misinformation = _factory("Misinformation")
RBAC = _factory("RBAC")
SSRF = _factory("SSRF")
SQLInjection = _factory("SQLInjection")
ShellInjection = _factory("ShellInjection")
Robustness = _factory("Robustness")


def CustomVulnerability(
    *,
    id: str,
    criteria: str,
    types: Sequence[str] = ("custom",),
    gated: bool = False,
    label: str | None = None,
) -> Vulnerability:
    """Customer-authored vulnerability. The evaluator treats `criteria` as authoritative."""
    if not id.strip():
        raise ValueError("custom vulnerability id must be non-empty")
    if not criteria.strip():
        raise ValueError("custom vulnerability criteria must be non-empty")
    selected = tuple(types) if types else ("custom",)
    return Vulnerability(
        id=id,
        label=label or id,
        allowed_types=selected,
        gated=gated,
        types=selected,
        criteria=criteria,
    )


VULNERABILITIES: dict[str, Callable[..., Vulnerability]] = {name: _factory(name) for name in ENTRIES}
"""A factory for every catalogue entry, by YAML name. Each takes ``types`` (all by default)."""

_VULNERABILITY_LOOKUP: dict[str, Callable[..., Vulnerability]] = {}
for _name, _make in VULNERABILITIES.items():
    _VULNERABILITY_LOOKUP[_name.lower()] = _make
    _VULNERABILITY_LOOKUP[ENTRIES[_name].id] = _make


def get_vulnerability(
    name: str,
    types: Sequence[str] | None = None,
    *,
    id: str | None = None,
    criteria: str | None = None,
    gated: bool | None = None,
) -> Vulnerability:
    """Resolve a YAML or Python name to a vulnerability.

    ``gated`` overrides the catalog gate policy when it is not ``None``, for
    built-ins and custom vulnerabilities alike. ``criteria`` is only valid for
    custom vulnerabilities: built-in rubrics live in martian-agents, so a
    criteria string on a built-in would be silently unused.
    """
    key = name.strip().lower().replace("-", "_")
    if key in {"custom", "customvulnerability"}:
        if id is None or criteria is None:
            raise ValueError("custom vulnerability requires id and criteria")
        return CustomVulnerability(id=id, criteria=criteria, types=types or ("custom",), gated=bool(gated))
    factory = _VULNERABILITY_LOOKUP.get(key)
    if factory is None:
        raise KeyError(f"unknown vulnerability: {name}")
    if criteria is not None:
        raise ValueError("criteria is only valid for custom vulnerabilities")
    vulnerability = factory(types)
    if gated is None:
        return vulnerability
    # An explicit gate choice must win over the catalog default; dropping it
    # would leave a customer-gated type report-only and the build green.
    return vulnerability.model_copy(update={"gated": gated})
