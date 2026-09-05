from pathlib import Path

from martian_redteam.config import RunConfig


def test_example_yaml_loads() -> None:
    config = RunConfig.from_yaml(Path("examples/config.yaml"))
    assert config.target.purpose
    assert config.vulnerabilities[0].name == "PromptLeakage"
    assert config.attacks[0].name == "PromptInjection"
    assert config.system_config.max_concurrent == 2
