"""Compatibility alias; the implementation lives in martian.evals.client."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("martian.evals.client")
