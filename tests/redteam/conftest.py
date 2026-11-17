import json
import os
from collections.abc import Iterator

import pytest

from martian.redteam.engine.hostile import secret_values
from tests.redteam.fake_models import KEY, FakeModels


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeModels]:
    """The fake attacker and judge, reached as ``openai:/`` models with only a model key set: no
    Martian variable is in the environment.

    When the test ends, every prompt the fake models received is scanned: none may hold the model
    key, or the value of any environment variable named like a credential (martian-notes#239)."""
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    fake = FakeModels()
    monkeypatch.setenv("OPENAI_API_BASE", fake.base)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    try:
        yield fake
        prompts = json.dumps([request["body"]["messages"] for request in fake.requests], ensure_ascii=False)
        hidden = {KEY, *secret_values(os.environ)}
        leaked = sorted(name for name, value in os.environ.items() if value in hidden and value in prompts)
        assert KEY not in prompts and not leaked, f"a prompt held the value of {leaked or ['the model key']}"
    finally:
        fake.close()
