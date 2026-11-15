"""Custom Python scorers: a team's own rule, written as a Python function.

One set of rules for every place a custom scorer runs: ``python_check`` in Python, ``type: python``
in a suite file (``martian evals run``), and the scorers a worker's setup installs for the evals
core (``WorkerServices.scorers``).

**What it is given.** A scorer function names, as its parameters, the arguments it reads, and is
called with those as keyword arguments:

- ``inputs``: the case's inputs, a dict, as a function target is called with them;
- ``outputs``: the reply, as the target returned it or as the case stores it;
- ``expectations``: the case's expectations, a dict (empty when it has none);
- ``trace``: the MLflow trace of the target's call, in a suite-file run and in ``evaluate``. A
  worker scores saved outputs, so it gives no trace, and a scorer it installs may not read one.

Any other parameter needs a default, which it keeps. ``*args``, ``**kwargs``, positional-only
parameters and ``async`` functions are refused when the scorer is loaded, before any case runs.

**What it may return.** ``True`` or ``False``, the verdict; a number (finite, not a bool), a score,
which a check passes or fails by its ``min_score`` and ``max_score``; or a pair ``(value, reason)``
of either with a string that becomes the check's reason. Anything else, and an exception the
scorer raises, is a scorer error for that case: the case has no verdict, the rest of the run goes
on, and a gate fails closed. :class:`ScorerError` carries the message.

The code runs with the full privileges of the process that calls it. Nothing here is a sandbox;
``docs/security-and-data.md`` says who may add a scorer and what the worker isolates.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import math
import numbers
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from martian.config import ConfigError

CODE_TYPES = ("python",)
"""The scorer types that run a team's own code: a suite file's ``type: python``, a ``code`` scorer in the model."""

ARGUMENTS = ("inputs", "outputs", "expectations", "trace")
"""The arguments a scorer may read in a suite-file run and in ``evaluate``."""

WORKER_ARGUMENTS = ("inputs", "outputs", "expectations")
"""The arguments a scorer installed in a worker may read: a worker scores saved outputs, with no trace."""

ERROR_LENGTH = 300

Score = bool | int | float


class ScorerError(ValueError):
    """A custom scorer gave no verdict for a case: it raised, or returned something it may not.
    The message names what happened; the case's check has no verdict."""


def _named(function: Any) -> str:
    return str(getattr(function, "__name__", None) or type(function).__name__)


def arguments(function: Any, allowed: Sequence[str] = ARGUMENTS) -> tuple[str, ...]:
    """The arguments ``function`` reads, in the order it takes them. Raises ``ValueError`` naming
    the problem when it is not a function a scorer can be: not callable, ``async``, taking
    ``*args`` or ``**kwargs``, a parameter it would not be given, or none it would."""
    if not callable(function):
        raise ValueError("a python check needs a function to call")
    name = _named(function)
    if inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(type(function).__call__):
        raise ValueError(f"the scorer {name} is async; a scorer is a plain function")
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        raise ValueError(f"the scorer {name} has no signature to read its arguments from") from None
    choices = ", ".join(allowed)
    reads = []
    for parameter in signature.parameters.values():
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise ValueError(f"the scorer {name} takes {parameter}; name the arguments it reads, from {choices}")
        if parameter.name in allowed:
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                raise ValueError(f"the scorer {name} takes {parameter.name} by position only; it is called by name")
            reads.append(parameter.name)
        elif parameter.default is inspect.Parameter.empty:
            raise ValueError(
                f"the scorer {name} takes {parameter.name}, which a scorer is not given; it takes {choices}"
            )
    if not reads:
        raise ValueError(f"the scorer {name} reads none of {choices}")
    return tuple(reads)


def bounds(min_score: Any, max_score: Any) -> None:
    """``min_score`` and ``max_score`` are finite numbers, when given, and the first is not above the second."""
    for label, value in (("min_score", min_score), ("max_score", max_score)):
        if value is not None and not (_number(value) and math.isfinite(value)):
            raise ValueError(f"{label} must be a number")
    if min_score is not None and max_score is not None and min_score > max_score:
        raise ValueError("min_score is more than max_score")


def _number(value: Any) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _described(value: Any) -> str:
    if value is None:
        return "None"
    if _number(value):
        return f"the number {value}"
    return f"a {type(value).__name__}"


def result_of(result: Any) -> tuple[Score, str]:
    """What a scorer returned, as its value and its reason. Raises :class:`ScorerError` for
    anything it may not return."""
    reason = ""
    if isinstance(result, tuple):
        if len(result) != 2 or not isinstance(result[1], str):
            raise ScorerError("the scorer returned a tuple that is not (value, reason) with a string reason")
        result, reason = result
    if isinstance(result, bool):
        return result, reason
    if _number(result) and math.isfinite(result):
        return (int(result) if isinstance(result, numbers.Integral) else float(result)), reason
    raise ScorerError(f"the scorer returned {_described(result)}; return True or False, a number, or (value, reason)")


def call(function: Callable[..., Any], given: Mapping[str, Any]) -> tuple[Score, str]:
    """Call the scorer with the arguments it reads and return its value and reason. Raises
    :class:`ScorerError` when it raises or returns something it may not; any exception of its own
    is caught, so a scorer never stops the run."""
    try:
        result = function(**given)
    except Exception as error:  # noqa: BLE001 - the scorer is the user's code and may raise anything
        raise ScorerError(f"the scorer raised {type(error).__name__}: {error}"[:ERROR_LENGTH]) from None
    return result_of(result)


def verdict(value: Score, reason: str, *, min_score: float | None, max_score: float | None) -> tuple[bool, str]:
    """A check's pass or fail from what its scorer returned, and the reason it gives. ``True`` or
    ``False`` is the verdict; a number passes when it is at least ``min_score`` and at most
    ``max_score``, which it needs one of."""
    bounded = min_score is not None or max_score is not None
    if isinstance(value, bool):
        if bounded:
            raise ScorerError(f"the scorer returned {value}; min_score and max_score apply to a number it returns")
        return value, reason or ("the scorer passed the reply" if value else "the scorer failed the reply")
    if not bounded:
        raise ScorerError(f"the scorer returned the number {value}; give min_score or max_score to pass or fail it")
    if min_score is not None and value < min_score:
        passed, said = False, f"score {value} is below min_score {min_score}"
    elif max_score is not None and value > max_score:
        passed, said = False, f"score {value} is above max_score {max_score}"
    else:
        limits = [
            f"at least {min_score}" if min_score is not None else "",
            f"at most {max_score}" if max_score is not None else "",
        ]
        passed, said = True, f"score {value} is {' and '.join(limit for limit in limits if limit)}"
    return passed, f"{said}: {reason}" if reason else said


def load_module(file: Path, *, what: str, module: str) -> ModuleType:
    """Import ``file`` as the module ``module``, afresh each time. Raises ``ConfigError`` naming
    the ``what`` file when it cannot be read or fails to import. The module runs as it is
    imported, with this process's privileges."""
    spec = importlib.util.spec_from_file_location(module, file)
    if spec is None or spec.loader is None or not file.is_file():
        raise ConfigError(f"could not load the {what} file")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    try:
        spec.loader.exec_module(loaded)
    except Exception as error:  # noqa: BLE001 - the customer's module may raise anything on import
        raise ConfigError(f"the {what} file failed to import: {type(error).__name__}") from None
    return loaded


def function_of(loaded: ModuleType, function: str, *, what: str) -> Callable[..., Any]:
    """The function ``function`` of a module :func:`load_module` imported, or ``ConfigError``."""
    found: Callable[..., Any] | None = getattr(loaded, function, None)
    if found is None or not callable(found):
        raise ConfigError(f"the {what} file has no function {function!r}")
    return found


def load_function(file: Path, function: str, *, what: str, module: str) -> Callable[..., Any]:
    """:func:`load_module`, then its ``function``."""
    return function_of(load_module(file, what=what, module=module), function, what=what)


def module_name(file: Path) -> str:
    """The module name a scorer file is imported under: one per file, so a scorer file never
    replaces the target's module or another scorer file's."""
    return "martian_evals_scorer_" + hashlib.sha256(str(file.resolve()).encode("utf-8")).hexdigest()[:16]


__all__ = [
    "ARGUMENTS",
    "CODE_TYPES",
    "WORKER_ARGUMENTS",
    "ScorerError",
    "arguments",
    "bounds",
    "call",
    "function_of",
    "load_function",
    "load_module",
    "module_name",
    "result_of",
    "verdict",
]
