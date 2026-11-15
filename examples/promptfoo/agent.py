"""A stand-in for the travel assistant promptfooconfig.yaml tests. It answers from a table, so the
suites converted from that config run with no model call.

`martian evals convert promptfoo ... --target agent.py:answer` calls ``answer(prompt=..., vars=...)``
for each case. ``call_api`` is the same answer as a promptfoo Python provider, which the config
names as ``file://agent.py``.
"""

import json
from typing import Any

ANSWERS = {
    "What is the capital of France?": "The capital of France is Paris.",
    "What is the capital of Japan?": "The capital of Japan is Tokyo.",
    # Fails the config's starts-with assertion on purpose.
    "What is the capital of Portugal?": "Lisbon is the capital of Portugal.",
    "Plan a day in Paris as JSON with the keys city, traveller and stops.": json.dumps(
        {"city": "Paris", "traveller": "Sam", "stops": ["Louvre", "Musée d'Orsay", "Seine cruise"]}
    ),
    "Plan a day in Lisbon as JSON with the keys city, traveller and stops.": json.dumps(
        {"city": "Lisbon", "traveller": "Sam", "stops": ["Alfama", "Belém Tower"]}
    ),
}


def answer(prompt: str, vars: dict[str, Any]) -> str:
    """The reply to one case: ``prompt`` is the rendered prompt, ``vars`` the test's vars."""
    return ANSWERS[vars["question"]]


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """The same reply as a promptfoo Python provider."""
    return {"output": answer(prompt, context["vars"]), "cost": 0}
