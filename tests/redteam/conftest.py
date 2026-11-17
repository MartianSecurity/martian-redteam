from collections.abc import Iterator

import pytest

from tests.redteam.fake_models import KEY, FakeModels


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeModels]:
    """The fake attacker and judge, reached as ``openai:/`` models with only a model key set: no
    Martian variable is in the environment."""
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    fake = FakeModels()
    monkeypatch.setenv("OPENAI_API_BASE", fake.base)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    try:
        yield fake
    finally:
        fake.close()
