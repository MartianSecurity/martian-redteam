"""What an edition lets a principal use. The edition decides; the core only asks.

The map from edition to feature keys is MorningHQ/martian-notes#127. Until then an edition is a
name and a set of feature keys, and the core gates nothing on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Edition = Literal["community", "enterprise", "on_premises"]


@dataclass(frozen=True)
class Entitlements:
    edition: Edition
    features: frozenset[str] = field(default_factory=frozenset)

    def allows(self, feature: str) -> bool:
        return feature in self.features


COMMUNITY = Entitlements(edition="community")
