"""Who is making a request. The edition decides; the core only reads it.

The local edition has one fixed user and no organisation. The server resolves an organisation
member from its own session and passes that in. Nothing in the core signs anyone in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Role = Literal["owner", "admin", "member", "viewer"]


@dataclass(frozen=True)
class Principal:
    user_id: str
    organisation_id: str | None = None
    roles: frozenset[Role] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.user_id:
            raise ValueError("a principal needs a user id")
        if self.organisation_id == "":
            raise ValueError("organisation_id is None for the single local user, never empty")

    @property
    def kind(self) -> Literal["single_user", "organisation"]:
        return "single_user" if self.organisation_id is None else "organisation"


SINGLE_USER = Principal(user_id="local", organisation_id=None, roles=frozenset({"owner"}))
"""The one user of the local edition: no organisation, no sign-in, every role."""


def organisation_principal(user_id: str, organisation_id: str, roles: frozenset[Role]) -> Principal:
    """A member of an organisation, as the server resolves it from its session."""
    if not organisation_id:
        raise ValueError("an organisation principal needs an organisation id")
    return Principal(user_id=user_id, organisation_id=organisation_id, roles=roles)
