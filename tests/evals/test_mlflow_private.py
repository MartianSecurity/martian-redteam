"""MLflow internals are reached through one module, and each one the SDK needs still exists."""

import ast
import importlib
import inspect
import re
from pathlib import Path

import pytest

from martian.evals._mlflow import PRIVATE_DEPENDENCIES, TYPE_ONLY

SRC = Path(__file__).resolve().parents[2] / "src"
PRIVATE_MODULE = SRC / "martian" / "evals" / "_mlflow.py"

# MLflow's public API: what may be imported or used anywhere in the SDK. Exported in MLflow's
# __all__, or a documented module.
PUBLIC_IMPORTS: dict[str, set[str] | None] = {
    "mlflow": None,
    "mlflow.genai": None,
    "mlflow.entities": {
        "AssessmentError",
        "AssessmentSource",
        "AssessmentSourceType",
        "ExperimentTag",
        "Feedback",
        "Metric",
        "RunStatus",
        "RunTag",
        "Span",
        "Trace",
        "TraceInfo",
        "TraceLocation",
        "TraceState",
        "ViewType",
        "Workspace",
    },
    "mlflow.exceptions": {"MlflowException"},
    "mlflow.genai.label_schemas": {"InputCategorical", "InputNumeric", "InputPassFail", "InputText", "LabelSchemaType"},
    "mlflow.genai.scorers": {"scorer", "Scorer"},
    "mlflow.types.llm": {"ChatMessage"},
    "mlflow.environment_variables": {"MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS"},
}
PUBLIC_ATTRIBUTES = {
    "mlflow": {"set_tracking_uri", "get_tracking_uri", "set_experiment", "trace", "active_run", "genai"},
    "mlflow.genai": {"evaluate"},
}


@pytest.mark.parametrize(("module", "name"), PRIVATE_DEPENDENCIES, ids=[f"{m}:{n}" for m, n in PRIVATE_DEPENDENCIES])
def test_each_mlflow_internal_the_sdk_uses_still_exists(module, name):
    imported = importlib.import_module(module)
    if (module, name) in TYPE_ONLY:
        assert re.search(rf"\b{name}\s*=", inspect.getsource(imported)), f"MLflow no longer defines {module}.{name}"
    elif name:
        found: object = imported
        for part in name.split("."):
            assert hasattr(found, part), f"MLflow no longer has {module}.{name}"
            found = getattr(found, part)


def _uses_outside_the_public_api(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] != "mlflow":
                    continue
                if alias.name not in PUBLIC_IMPORTS or PUBLIC_IMPORTS[alias.name] is not None:
                    problems.append(f"{path.name}:{node.lineno} import {alias.name}")
                names[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else "mlflow"
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "mlflow":
            allowed = PUBLIC_IMPORTS.get(node.module or "")
            for alias in node.names:
                if allowed is None or alias.name not in allowed:
                    problems.append(f"{path.name}:{node.lineno} from {node.module} import {alias.name}")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            chain.append(current.attr)
            current = current.value
        if not isinstance(current, ast.Name) or current.id not in names:
            continue
        module = names[current.id]
        for attribute in reversed(chain):
            if attribute not in PUBLIC_ATTRIBUTES.get(module, set()):
                problems.append(f"{path.name}:{node.lineno} {module}.{attribute}")
                break
            if f"{module}.{attribute}" not in PUBLIC_ATTRIBUTES:
                break
            module = f"{module}.{attribute}"
    return sorted(set(problems))


def test_mlflow_internals_are_used_only_in_the_private_module():
    problems = []
    for path in sorted(SRC.rglob("*.py")):
        if path != PRIVATE_MODULE:
            problems += _uses_outside_the_public_api(path)
    assert problems == [], "move these into martian/evals/_mlflow.py"


def test_the_scan_catches_a_private_import_and_a_private_attribute(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import mlflow\n"
        "from mlflow.tracking import fluent\n"
        "x = mlflow.tracking.fluent._active_experiment_id\n"
        "mlflow.set_tracking_uri('sqlite://')\n",
        encoding="utf-8",
    )
    found = _uses_outside_the_public_api(sample)
    assert any("from mlflow.tracking import fluent" in p for p in found)
    assert any("mlflow.tracking" in p and "import" not in p for p in found)
    assert not any("set_tracking_uri" in p for p in found)


def test_the_private_module_lists_every_internal_it_imports():
    tree = ast.parse(PRIVATE_MODULE.read_text(encoding="utf-8"))
    listed = set(PRIVATE_DEPENDENCIES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mlflow"):
            for alias in node.names:
                if node.module == "mlflow.tracking" and alias.name == "fluent":
                    continue  # read for its _active_experiment_id, which is listed
                found = any(
                    module == node.module and (name == alias.name or name.startswith(alias.name + "."))
                    for module, name in listed
                )
                assert found, f"{node.module}.{alias.name} is not listed"
