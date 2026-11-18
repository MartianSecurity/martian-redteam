"""A shop assistant for the simulated-users example: no model, and a bug that a patient user never
meets.

It is given the earlier turns in ``history`` and looks for the customer's order number there, but
only in the last two things the customer said. A customer who gives the number and then talks
about something else is asked for it again when they finally ask to cancel.
"""

import re

ORDERS = {"A-1001": "shipped", "A-1002": "waiting for stock"}


def reply(message, history):
    said = [turn["content"] for turn in history if turn["role"] == "user"] + [message]
    recent = said[-2:]  # the bug: everything said before that is forgotten
    order = next((found for text in reversed(recent) for found in re.findall(r"A-\d{4}", text)), None)
    if order is None:
        return "I can help with that. What is your order number?"
    if "cancel" in message.lower():
        if ORDERS.get(order) == "shipped":
            return f"Order {order} has already shipped, so it cannot be cancelled. You can return it within 30 days."
        return f"Order {order} is cancelled, and you will be refunded in full."
    return f"Order {order} is {ORDERS.get(order, 'not one I can find')}."
