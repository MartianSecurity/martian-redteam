"""Red team attack methods: how an attempt writes its attacks, sends them and reads the result.

A version 2 run file's ``attacks`` entries name methods (:mod:`martian.redteam.methods.registry`).
Each is an :class:`AttackMethod` of one of four kinds (transform, rewrite, conversation, search) and
one arm (control, static, adaptive), with its own settings, optional transform ``layers``, and a
budget of target queries per attempt. The engine runs each attempt through the method's
:meth:`AttackMethod.run`, with an :class:`AttackContext` that counts every call on the run's budget.
``docs/redteam-methods.md`` describes every method.
"""

from martian.redteam.methods.base import (
    SCORES,
    Arm,
    AttackContext,
    AttackMethod,
    Exchange,
    Feedback,
    GenerationMode,
    MethodKind,
    Rung,
    Transform,
)
from martian.redteam.methods.registry import (
    LEGACY,
    MethodError,
    MethodFactory,
    all_transforms,
    method_names,
    methods_of,
    needs_attacker,
    register_method,
    register_transform,
    resolve,
    transform,
    unregister_method,
)
from martian.redteam.methods.rewrites import Rewrite

__all__ = [
    "LEGACY",
    "SCORES",
    "Arm",
    "AttackContext",
    "AttackMethod",
    "Exchange",
    "Feedback",
    "GenerationMode",
    "MethodError",
    "MethodFactory",
    "MethodKind",
    "Rewrite",
    "Rung",
    "Transform",
    "method_names",
    "methods_of",
    "needs_attacker",
    "register_method",
    "register_transform",
    "resolve",
    "transform",
    "all_transforms",
    "unregister_method",
]
