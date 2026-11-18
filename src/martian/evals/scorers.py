"""Scorers with the same semantics as Backend's canonical exact match (strip + casefold)."""

import json
from typing import Any

from mlflow.genai.scorers import scorer

# Backend rejects an output over this many characters; the kit reports such a row as an error
# instead of failing the whole upload or silently truncating the answer.
MAX_OUTPUT_CHARS = 20_000


def output_text(value: object) -> str:
    """The text of an agent output as uploaded. Local scoring uses the same function, so a
    structured output cannot pass locally and fail on Backend (or the reverse)."""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, sort_keys=True)


@scorer
def exact_match(outputs: Any, expectations: dict[str, Any]) -> bool:
    expected = expectations["expected_response"]
    return output_text(outputs).strip().casefold() == str(expected).strip().casefold()
