"""A shop assistant for the conversation example: no model, and a bug a single-turn suite misses.

It is given the earlier turns in ``history`` and looks for the customer's order number there, but
only in the last two things the customer said. A customer who gave the number three turns ago is
asked for it again.
"""

import re

ORDERS = {"A-1001": "shipped", "A-1002": "waiting for stock"}


def reply(message, history):
    said = [turn["content"] for turn in history if turn["role"] == "user"] + [message]
    recent = said[-2:]  # the bug: everything said before that is forgotten
    order = next((found for text in reversed(recent) for found in re.findall(r"A-\d{4}", text)), None)
    asked = message.lower()
    if order is None:
        return "I can help with that. What is your order number?"
    if "cancel" in asked:
        if ORDERS.get(order) == "shipped":
            return f"Order {order} has already shipped, so it cannot be cancelled. You can return it within 30 days."
        return f"Order {order} is cancelled, and you will be refunded in full."
    return f"Order {order} is {ORDERS.get(order, 'not one I can find')}."
