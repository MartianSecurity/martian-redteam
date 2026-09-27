from pathlib import Path

import pytest
from pydantic import ValidationError

from martian_redteam.config import RunConfig


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
