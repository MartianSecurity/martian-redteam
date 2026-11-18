"""Compatibility alias; the implementation lives in martian.evals.state."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("martian.evals.state")
