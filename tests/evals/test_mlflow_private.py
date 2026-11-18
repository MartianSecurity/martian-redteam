"""MLflow internals are reached through one module, but for the documented exemption in
``OWN_IMPORTS``, and each one the SDK needs still exists."""

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
    "mlflow.environment_variables": {"MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS", "MLFLOW_MODEL_CATALOG_URI"},
}
PUBLIC_ATTRIBUTES = {
    "mlflow": {
        "set_tracking_uri",
        "get_tracking_uri",
        "set_experiment",
        "trace",
        "active_run",
        "genai",
        "update_current_trace",
    },
    "mlflow.genai": {"evaluate"},
}

# The one exemption: MLflow internals a file outside the private module imports itself, by its path
# under src. Each is still listed in PRIVATE_DEPENDENCIES, so a release that drops one fails
# test_each_mlflow_internal_the_sdk_uses_still_exists by name.
#
# _target_process.py is the process a function target runs in. It is started as a script
# (python -P) and never imports the evals package, so that the target's process holds nothing of
# the run (martian.evals.isolation, docs/security-and-data.md). martian.evals._mlflow is part of
# that package: importing it runs the package's __init__, which imports the runner, the checks and
# the judges, and loading the file on its own would still load MLflow's tracking store and judge
# internals into the target's process, about two seconds more before the run's first call. Its
# tracing also reads LiveSpan._span, an instance attribute this scan cannot see; the suite
# runner's traced function target (test_runner.py) covers it.
OWN_IMPORTS: dict[str, frozenset[tuple[str, str]]] = {
    "martian/evals/_target_process.py": frozenset(
        {
            ("mlflow.models.evaluation.utils.trace", "configure_autologging_for_evaluation"),
            ("mlflow.tracing.trace_manager", "InMemoryTraceManager"),
        }
    ),
}


def _listed(module: str, name: str) -> bool:
    return any(
        listed == module and (internal == name or internal.startswith(name + "."))
        for listed, internal in PRIVATE_DEPENDENCIES
    )


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


def _uses_outside_the_public_api(path: Path, own: frozenset[tuple[str, str]] = frozenset()) -> list[str]:
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
                if (node.module, alias.name) in own:
                    continue
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
            problems += _uses_outside_the_public_api(
                path, OWN_IMPORTS.get(path.relative_to(SRC).as_posix(), frozenset())
            )
    assert problems == [], "move these into martian/evals/_mlflow.py"


def test_each_internal_a_file_imports_itself_is_still_imported_there_and_listed():
    for file, own in OWN_IMPORTS.items():
        tree = ast.parse((SRC / file).read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        for module, name in sorted(own):
            assert (module, name) in imported, f"{file} no longer imports {module}.{name}: drop it from OWN_IMPORTS"
            assert _listed(module, name), f"{module}.{name} is not listed in PRIVATE_DEPENDENCIES"


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
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mlflow"):
            for alias in node.names:
                if node.module == "mlflow.tracking" and alias.name == "fluent":
                    continue  # read for its _active_experiment_id, which is listed
                assert _listed(node.module or "", alias.name), f"{node.module}.{alias.name} is not listed"
