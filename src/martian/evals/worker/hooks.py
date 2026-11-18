"""Per-case hooks and installed checks: what an edition adds around each case a worker runs.

An edition installs both on the worker, in the setup that builds its services
(``WorkerServices.hooks`` and ``WorkerServices.checks``, or ``InProcessWorker(hooks=...,
checks=...)``), the way it installs custom scorers and its target caller. The open edition
installs none.

- **Hooks** (:class:`CaseHooks`) run around each case a run calls its target for: every hook's
  ``setup`` in order before the call, and the ``teardown`` of each hook whose setup returned, in
  the opposite order, after the case is graded, whether the case passed, failed, timed out, its
  target raised or a later hook's setup did, and when the work is cancelled during the case. Each
  call of a case's target is one round: a case run again, after its worker stopped or its run was
  resumed, is set up again. A case the run skips, a conversation case among them (the worker runs
  no conversation), runs no hook, and neither does a scoring job, a monitor or a schedule's turn.
- **State.** The worker gives each case a new :class:`CaseState`, which the hooks fill in.
  ``target_inputs`` reach the target beside the case's own inputs; ``values`` reach the installed
  checks and the teardown, never the target.
- **What a hook is given** (:class:`CaseContext`): the principal, the run, the case without its
  expectations or stored output, its position and its state. So the expected answers never reach
  a hook, and through it the target (MartianSecurity/martian-notes#215); the installed checks grade with
  them.
- **Installed checks** are plain functions the setup installs by name
  (:data:`~martian.evals.worker.execute.InstalledChecks`). A suite, a run or a scoring job names
  one as a ``code`` scorer of type ``installed`` (:func:`installed_check`); a suite file writes it
  as ``type: installed`` with its ``name`` and ``options``. A check reads, by name, the case's
  ``inputs``, the ``outputs``, the case's ``expectations``, the case's ``state`` and the check's
  ``options``, and returns what a custom scorer returns (:mod:`martian.evals._custom`). A run's
  installed checks grade each case while its state lives, between the target's answer and the
  teardown; a scoring job's grade saved outputs and are given ``state=None``.
- **Required checks.** An installed check whose options say ``required: true``
  (:func:`installed_check`, :func:`is_required`) decides the status of the case a run grades it on:
  ``passed`` only when the answer matched, when the case has an expected answer, and every required
  check returned true; ``failed`` when one returned false; ``error`` when one gave no verdict. The
  check's reason is the case's. A check without the option only adds a score, as before, and a
  scoring job's required check does too: a job never changes a stored case's status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from martian.evals import _custom
from martian.evals._custom import INSTALLED_TYPES, REQUIRED_OPTION
from martian.evals.model import Case, Run, ScorerVersion

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

INSTALLED_TYPE = INSTALLED_TYPES[0]
"""The type of a ``code`` scorer that names a check the worker installs."""

SETUP_FAILED = "The case's setup failed: {error}"
TEARDOWN_FAILED = "The case's teardown failed: {error}"
INPUT_TAKEN = "The case's setup gave the target {names}, which the case's own inputs already name"
HOOK_ERROR_LENGTH = 1800


@dataclass
class CaseState:
    """One case's state, from its setup to its teardown. ``target_inputs`` are inputs the target is
    given beside the case's own, such as the address of something the setup prepared; a name the
    case's inputs already have is the case's error. ``values`` are for the installed checks and
    the teardown, and never reach the target."""

    target_inputs: dict[str, Any] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseContext:
    """What a hook is given: the principal the work runs for, the run, the case without its
    expectations or stored output (:func:`hooked_case`), its position in the run and its state."""

    principal: Principal
    run: Run
    case: Case
    position: int
    state: CaseState


@runtime_checkable
class CaseHooks(Protocol):
    """Hooks an edition installs to run around each case of a run (:mod:`martian.evals.worker.hooks`).

    ``setup`` prepares the case and fills in its state; an exception is the case's error, its
    target is not called, and the hooks set up before it are torn down. ``teardown`` runs once the
    case is graded, given the case's error so far (empty when the target answered); an exception
    is the case's error too, and the other hooks are still torn down. Neither has a time limit:
    the edition bounds its own."""

    async def setup(self, context: CaseContext) -> None: ...

    async def teardown(self, context: CaseContext, error: str) -> None: ...


def hooked_case(case: Case) -> Case:
    """What a hook is given of ``case``: everything but its expectations and stored output, which
    grade it."""
    return case.model_copy(update={"expectations": {}, "outputs": None})


def with_target_inputs(sent: Case, state: CaseState) -> Case:
    """``sent``, the case as the target is given it, with the inputs the setup gave the target.
    Raises ``ValueError`` when the setup gave one the case already has."""
    if not state.target_inputs:
        return sent
    if taken := sorted(set(state.target_inputs) & set(sent.inputs)):
        raise ValueError(INPUT_TAKEN.format(names=", ".join(taken)))
    return sent.model_copy(update={"inputs": {**sent.inputs, **state.target_inputs}})


def installed_check(name: str, options: dict[str, Any] | None = None, *, required: bool = False) -> ScorerVersion:
    """A scorer naming the check a worker installs under ``name``, with ``options`` for it.
    ``required`` adds the option that makes the check decide its case's status
    (:data:`~martian.evals._custom.REQUIRED_OPTION`); a suite file writes the same option itself."""
    given = {**(options or {}), **({REQUIRED_OPTION: True} if required else {})}
    return ScorerVersion(name=name, kind="code", type=INSTALLED_TYPE, parameters={"options": given} if given else {})


def is_installed(scorer: ScorerVersion) -> bool:
    return scorer.kind == "code" and scorer.type == INSTALLED_TYPE


def is_required(scorer: ScorerVersion) -> bool:
    """Whether ``scorer`` is an installed check marked required. Raises ``ValueError`` when its
    ``required`` option is not ``true`` or ``false`` (:func:`~martian.evals._custom.required`)."""
    return is_installed(scorer) and _custom.required(scorer.parameters.get("options"))


async def set_up(hooks: tuple[CaseHooks, ...], context: CaseContext, prepared: list[CaseHooks]) -> str:
    """Run each hook's setup in order, adding each that returned to ``prepared``, and return the
    case's error: empty, or the first setup's failure, which ends the setup there."""
    for hook in hooks:
        try:
            await hook.setup(context)
        except Exception as failure:  # noqa: BLE001 - a hook's failure is the case's error
            return SETUP_FAILED.format(error=_described(failure))[:HOOK_ERROR_LENGTH]
        prepared.append(hook)
    return ""


async def tear_down(prepared: list[CaseHooks], context: CaseContext, error: str) -> str:
    """Run the teardown of each hook in ``prepared``, last set up first, each whatever the others
    do, and return the case's error from them: empty, or each failure's."""
    failures = []
    for hook in reversed(prepared):
        try:
            await hook.teardown(context, error)
        except Exception as failure:  # noqa: BLE001 - a hook's failure is the case's error
            failures.append(_described(failure))
    return TEARDOWN_FAILED.format(error="; ".join(failures))[:HOOK_ERROR_LENGTH] if failures else ""


def _described(failure: Exception) -> str:
    return f"{type(failure).__name__}: {failure}"


__all__ = [
    "INSTALLED_TYPE",
    "CaseContext",
    "CaseHooks",
    "CaseState",
    "hooked_case",
    "installed_check",
    "is_installed",
    "is_required",
    "set_up",
    "tear_down",
    "with_target_inputs",
]
