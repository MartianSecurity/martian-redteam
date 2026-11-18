"""Martian SDK: evaluations and adversarial testing in the customer's process."""

from martian._version import __version__ as __version__
from martian.config import CiContext as CiContext
from martian.config import MartianConfig as MartianConfig

__all__ = ["__version__", "MartianConfig", "CiContext"]
