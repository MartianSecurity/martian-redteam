from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from martian_redteam.config import ALLOW_INSECURE_API_URL_ENV, RunConfig, Settings


@pytest.fixture(autouse=True)
def _no_insecure_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer shell's escape hatch from changing test results."""
    monkeypatch.delenv(ALLOW_INSECURE_API_URL_ENV, raising=False)


def test_example_yaml_loads() -> None:
    config = RunConfig.from_yaml(Path("examples/config.yaml"))
    assert config.target.purpose
    assert config.vulnerabilities[0].name == "PromptLeakage"
    assert config.attacks[0].name == "PromptInjection"
    assert config.system_config.max_concurrent == 2
    assert config.system_config.target_timeout_seconds == 120.0
    assert Path(config.target.callback.file).is_absolute()
    assert Path(config.target.callback.file).name == "callback.py"
    assert config.gate.ignore_target_errors is False


def test_relative_callback_is_resolved_from_yaml(tmp_path: Path) -> None:
    callback = tmp_path / "target.py"
    callback.write_text("def model_callback(prompt: str) -> str:\n    return 'no'\n", encoding="utf-8")
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
target:
  purpose: test
  callback:
    file: target.py
vulnerabilities:
  - name: PromptLeakage
attacks:
  - name: PromptInjection
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = RunConfig.from_yaml(config_path)
    assert config.target.callback.file == str(callback.resolve())


def test_invalid_threshold_rejected() -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {
                "target": {"purpose": "x", "callback": {"file": "a.py"}},
                "vulnerabilities": [{"name": "PromptLeakage"}],
                "attacks": [{"name": "PromptInjection"}],
                "gate": {"pass_rate_threshold": 1.5},
            }
        )


def _minimal_config(**overrides: object) -> dict[str, object]:
    return {
        "target": {"purpose": "x", "callback": {"file": "a.py"}},
        "vulnerabilities": [{"name": "PromptLeakage"}],
        "attacks": [{"name": "PromptInjection"}],
        **overrides,
    }


def test_target_timeout_is_configurable() -> None:
    config = RunConfig.model_validate(_minimal_config(system_config={"target_timeout_seconds": 30}))
    assert config.system_config.target_timeout_seconds == 30.0


@pytest.mark.parametrize("value", [0, 0.5, -1])
def test_target_timeout_below_one_second_rejected(value: float) -> None:
    with pytest.raises(ValidationError, match="target_timeout_seconds"):
        RunConfig.model_validate(_minimal_config(system_config={"target_timeout_seconds": value}))


@pytest.mark.parametrize(
    "url",
    [
        "https://api.agents.martiansecurity.ai",
        "https://agents.test.example/",
        "http://localhost:2026",
        "http://127.0.0.1:2026",
    ],
)
def test_secure_and_loopback_api_urls_accepted(url: str) -> None:
    settings = Settings(api_url=url, api_key=SecretStr("k"))
    assert settings.api_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example",
        "http://127.0.0.1.evil.example",
        "http://localhost@evil.example",
        "http://10.0.0.5:2026",
        "ftp://api.agents.martiansecurity.ai",
        "api.agents.martiansecurity.ai",
    ],
)
def test_insecure_api_urls_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match="MARTIAN_REDTEAM_API_URL"):
        Settings(api_url=url, api_key=SecretStr("k"))


def test_insecure_api_url_from_environment_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARTIAN_REDTEAM_API_URL", "http://evil.example")
    with pytest.raises(ValidationError, match="must use https://"):
        Settings()


def test_escape_hatch_allows_insecure_api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_INSECURE_API_URL_ENV, "1")
    assert Settings(api_url="http://staging.internal:2026", api_key=SecretStr("k")).api_url == (
        "http://staging.internal:2026"
    )
    config = RunConfig.model_validate(_minimal_config(api={"url": "http://staging.internal:2026"}))
    assert config.api is not None


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_escape_hatch_requires_exact_value(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ALLOW_INSECURE_API_URL_ENV, value)
    with pytest.raises(ValidationError, match="must use https://"):
        Settings(api_url="http://evil.example", api_key=SecretStr("k"))


def test_yaml_api_url_must_be_secure() -> None:
    with pytest.raises(ValidationError, match="api.url must use https://"):
        RunConfig.model_validate(_minimal_config(api={"url": "http://evil.example"}))


def test_yaml_cannot_redirect_an_origin_bound_legacy_credential() -> None:
    base = Settings(api_url="https://agents.test.example", api_key=SecretStr("k"))
    same = RunConfig.model_validate(_minimal_config(api={"url": "https://agents.test.example/"}))
    assert same.settings_overlay(base) is base
    for url in ("https://other.test.example/", "http://localhost:2026"):
        other = RunConfig.model_validate(_minimal_config(api={"url": url}))
        with pytest.raises(ValueError, match="origin"):
            other.settings_overlay(base)


def test_legacy_settings_do_not_read_generic_env_or_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MARTIAN_API_URL", "https://generic.test.example")
    monkeypatch.setenv("MARTIAN_API_KEY", "generic-secret")
    monkeypatch.delenv("MARTIAN_REDTEAM_API_URL", raising=False)
    monkeypatch.delenv("MARTIAN_REDTEAM_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "MARTIAN_REDTEAM_API_URL=https://dotenv.test.example\nMARTIAN_REDTEAM_API_KEY=dotenv-key\n"
    )
    with pytest.raises(ValidationError):
        Settings()
