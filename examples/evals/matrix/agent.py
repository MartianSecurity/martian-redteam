"""The support agent of the matrix example, standing in for a model call so that the example runs
with no model key. `martian evals run` passes each axis of the suite's matrix as a keyword argument
beside the case's input: `model`, the model's name, and `prompt`, the text of the named prompt."""

FACTS = {
    "refund": "Refunds are paid within 14 days.",
    "shipping": "Standard shipping takes 3 to 5 working days.",
    "returns": "Returns are free within 30 days.",
    "hours": "Support is open 9:00 to 17:00, Monday to Friday.",
    "warranty": "Every product has a 2-year warranty.",
    "payment": "We take cards and PayPal.",
}
# What the smaller model does not know.
UNKNOWN_TO_SMALL = ("warranty", "payment")


def answer(message: str, model: str, prompt: str) -> str:
    topic = next((name for name in FACTS if name in message.lower()), None)
    known = topic is not None and not (model == "small-model" and topic in UNKNOWN_TO_SMALL)
    reply = FACTS[topic] if topic is not None and known else "Please see our help pages."
    if prompt.startswith("Greet"):
        reply = "Hello, and thank you for getting in touch! " + reply
    return reply
