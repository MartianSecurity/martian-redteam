"""The support team's own refund rules, as Python functions the suite file names.

Each function names the arguments it reads (any of inputs, outputs, expectations and trace) and
returns True or False, a number, or (value, reason). It runs in the process of
``martian evals run``, with that process's privileges: it is not sandboxed.
"""

import json

REASON_CODES = ("DAMAGED", "LATE", "WRONG_ITEM")


def refund_share(outputs, expectations):
    """The refund as a share of what the customer paid; the suite fails a share above 1."""
    refund = json.loads(outputs)["refund"]
    paid = expectations["order_total"]
    return refund / paid, f"refund {refund:.2f} of an order of {paid:.2f}"


def cites_reason_code(outputs):
    """Every refund names one of the reason codes."""
    reason = json.loads(outputs).get("reason")
    return reason in REASON_CODES, f"reason {reason!r}"
