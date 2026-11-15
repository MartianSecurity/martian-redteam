"""Reading a number someone else wrote: only the digits 0-9 count.

``str.isdigit()`` is true of digits ``int()`` refuses, such as ``²``, and of other scripts' digits
``int()`` reads as a number, such as ``١``, so a value checked with it alone is a ``ValueError`` or
a number nobody typed.
"""

from __future__ import annotations


def ascii_digits(text: str) -> bool:
    """Whether ``text`` is one or more of the digits 0-9 and nothing else."""
    return text.isascii() and text.isdigit()


__all__ = ["ascii_digits"]
