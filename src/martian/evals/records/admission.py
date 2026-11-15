"""A server's own rule for admitting a run the core's launch records: the seam through which it may
refuse the run, for a reason of its own, and choose the run's code.

The core counts run codes up from ``EV-1001`` in the organisation's records
(:func:`martian.evals.server.launch.admit_run`) and refuses a launch while a dataset of the project
is being published (:func:`martian.evals.manifests.publishing`). A server whose organisations
share more than the core knows can add to both. It may hold an organisation's new work back while
it migrates or publishes something the core does not see, and it may number a run from a sequence
that other writers (a CI gate that records runs of its own) share. It gives the record store an
:class:`RunAdmission` (``SqlRecordStore(..., admission=...)``), which the store consults for each
new run that a launch records, before it stores the run. The store has no admission by default, and
a run then keeps the code it was given.

The core names nothing of why a server pauses or where its codes come from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

from martian.evals.model import Run

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

LAUNCHED: Final = "console"
"""The ``source`` of a run the server's launch records (:attr:`martian.evals.model.Run.source`):
the runs a :class:`RunAdmission` is asked about. A run recorded by a ``martian evals run`` or the
CI gate has its own source and is never asked."""


class RunAdmission(Protocol):
    """What a server decides about a new launched run, before the record store keeps it."""

    async def admit(self, principal: Principal, run: Run) -> Run:
        """Admit ``run``, a launch the store does not hold yet, as it is or with the code the
        server numbers it by (``run.model_copy(update={"code": ...})``), or refuse it by raising.

        The record store raises what this raises, so the run is not stored and the launch fails
        with it. Raise an exception the server answers: ``fastapi.HTTPException(409, ...)``, or
        ``martian.evals.store.StoreError("conflict", ...)``, which the core's own app and a mount
        that handles ``StoreError`` answer as ``409``.

        It is called once for each run the store does not hold, so a launch sent again (the same
        run id) is not asked about and keeps the code it was stored with. A number taken for a run
        that is then not stored, because the launch fails after it or two requests for the same
        new run race, is not given back."""
        ...


__all__ = ["LAUNCHED", "RunAdmission"]
