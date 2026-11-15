"""A suite file is the written form of the shared model's ``Suite``.

    suite, cases = load_suite_file("evals.yaml")   # the suite and its data, in the model
    text = suite_to_yaml(suite)                    # the suite as a suite file again
    data = cases_to_jsonl(cases)                   # and its data file

Every field ``martian evals run`` accepts has a place in the model (``SUITE_FILE_FIELDS``), so a
suite file loaded and written again means the same. Where the two cannot meet, a ``ConfigError``
names the field instead of dropping it: a check option that does not apply to its check, a judge
no check uses, a data row field the runner does not read, or a suite the file cannot hold, such as
an agent target, pass policies or two judge models. ``martian evals run`` loads its suite through
:func:`load_suite_file`, so it refuses the same fields. Header values stay as written, ``${NAME}``
unexpanded.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from martian.config import ConfigError
from martian.evals import runner
from martian.evals.model import (
    CacheSettings,
    Case,
    Repeats,
    RunSettings,
    ScorerVersion,
    Suite,
    Target,
    digest,
    suite_digest,
)

# Every field of a suite file, and where the model keeps it.
SUITE_FILE_FIELDS = {
    "data": "Suite.dataset_location; Suite.dataset_name and Suite.dataset_digest from its cases",
    "target": "Suite.target; a suite file without one has a Target of kind stored",
    "target.file": "Target.file (kind function)",
    "target.function": "Target.function (kind function)",
    "target.url": "Target.url (kind http)",
    "target.headers": "Target.headers (kind http), values as written",
    "target.body": "Target.body (kind http)",
    "target.response_path": "Target.response_path (kind http)",
    "target.timeout_seconds": "RunSettings.target_timeout_seconds",
    "judge": "The llm_judge scorers",
    "judge.model": "ScorerVersion.model of each llm_judge scorer",
    "judge.inference_params": "ScorerVersion.parameters of each llm_judge scorer",
    "judge.max_retries": "RunSettings.judge_max_retries",
    "judge.rate_limit_wait_seconds": "RunSettings.judge_rate_limit_wait_seconds",
    "judge.headers": "RunSettings.judge_headers, values as written",
    "checks": "Suite.scorers, in order",
    "checks.type": "ScorerVersion.type; kind llm_judge for the LLM judges (runner.JUDGES), check otherwise",
    "checks.name": "ScorerVersion.name (the check's label)",
    "checks.rubric": "ScorerVersion.criteria.rubric",
    "checks.guidelines": "ScorerVersion.criteria.guidelines",
    "checks.conditions": "ScorerVersion.criteria.conditions",
    "checks.values": "ScorerVersion.parameters.values",
    "checks.expectation": "ScorerVersion.parameters.expectation",
    "checks.any_of": "ScorerVersion.parameters.any_of",
    "checks.case_sensitive": "ScorerVersion.parameters.case_sensitive",
    "checks.min_similarity": "ScorerVersion.parameters.min_similarity",
    "checks.pattern": "ScorerVersion.parameters.pattern",
    "checks.required_keys": "ScorerVersion.parameters.required_keys",
    "checks.schema": "ScorerVersion.parameters.schema",
    "checks.min_length": "ScorerVersion.parameters.min_length",
    "checks.max_length": "ScorerVersion.parameters.max_length",
    "checks.max_ms": "ScorerVersion.parameters.max_ms",
    "checks.max_usd": "ScorerVersion.parameters.max_usd",
    "checks.max_tokens": "ScorerVersion.parameters.max_tokens",
    "checks.value": "ScorerVersion.parameters.value, as written",
    "checks.path": "ScorerVersion.parameters.path",
    "checks.abs_tolerance": "ScorerVersion.parameters.abs_tolerance, as written",
    "checks.rel_tolerance": "ScorerVersion.parameters.rel_tolerance, as written",
    "checks.parts": "ScorerVersion.parameters.parts",
    "checks.add": "ScorerVersion.parameters.add",
    "checks.subtract": "ScorerVersion.parameters.subtract",
    "checks.total": "ScorerVersion.parameters.total",
    "checks.key": "ScorerVersion.parameters.key",
    "checks.columns": "ScorerVersion.parameters.columns",
    "checks.rows": "ScorerVersion.parameters.rows",
    "checks.kinds": "ScorerVersion.parameters.kinds",
    "checks.allow": "ScorerVersion.parameters.allow",
    "checks.combine": "Suite.repeats.checks, by the check's label",
    "repeats": "Suite.repeats.attempts",
    "combine": "Suite.repeats.combine",
    "matrix": "Suite.matrix, as written: each axis a list of values or a mapping of names to values",
    "concurrency": "RunSettings.concurrency",
    "batch": "RunSettings.batch",
    "output": "RunSettings.output",
    "input_key": "Suite.input_key",
    "mlflow": "RunSettings.mlflow_tracking_uri and mlflow_experiment",
    "mlflow.tracking_uri": "RunSettings.mlflow_tracking_uri",
    "mlflow.experiment": "RunSettings.mlflow_experiment",
    "cache": "RunSettings.cache; a cache block, even an empty one, turns the cache of model calls on",
    "cache.folder": "RunSettings.cache.folder, as written",
    "cache.ttl_seconds": "RunSettings.cache.ttl_seconds",
    "cache.functions": "RunSettings.cache.functions (function target only)",
}

# The fields every check type takes: its type, its label and the rule its attempts combine by.
COMMON_CHECK_FIELDS = frozenset({"type", "name", "combine"})

# The check fields each check type uses besides COMMON_CHECK_FIELDS, as the file names them.
# Loading refuses the others.
_TEXT_OPTIONS = frozenset({"values", "expectation", "case_sensitive"})
_TOLERANCE_OPTIONS = frozenset({"abs_tolerance", "rel_tolerance"})
CHECK_OPTIONS: dict[str, frozenset[str]] = {
    "rubric": frozenset({"rubric"}),
    "guidelines": frozenset({"guidelines"}),
    "conditions": frozenset({"conditions"}),
    # The grounding judges read each case's inputs.context and take no options.
    "groundedness": frozenset(),
    "context_relevance": frozenset(),
    "context_sufficiency": frozenset(),
    "contains": _TEXT_OPTIONS | {"any_of"},
    "not_contains": _TEXT_OPTIONS,
    "contains_any": _TEXT_OPTIONS,
    "contains_all": _TEXT_OPTIONS,
    "equals": _TEXT_OPTIONS,
    "starts_with": _TEXT_OPTIONS,
    "ends_with": _TEXT_OPTIONS,
    "similarity": _TEXT_OPTIONS | {"min_similarity"},
    "matches": frozenset({"pattern"}),
    "not_matches": frozenset({"pattern"}),
    "is_json": frozenset({"required_keys"}),
    "json_schema": frozenset({"schema"}),
    "length": frozenset({"min_length", "max_length"}),
    "latency": frozenset({"max_ms"}),
    "cost": frozenset({"max_usd", "max_tokens"}),
    "number_equals": _TOLERANCE_OPTIONS | {"value", "expectation", "path", "pattern"},
    "sums_to": _TOLERANCE_OPTIONS | {"parts", "total"},
    "reconciles": _TOLERANCE_OPTIONS | {"add", "subtract", "total"},
    "table_equals": _TOLERANCE_OPTIONS | {"rows", "expectation", "key", "columns", "path", "case_sensitive"},
    "no_prohibited_phrases": frozenset({"values", "expectation"}),
    "no_personal_data": frozenset({"kinds", "allow"}),
    "no_secrets": frozenset({"kinds", "allow"}),
    "resists_injection": frozenset({"values", "expectation"}),
    "no_refusal": frozenset({"expectation"}),
}

# The fields of a JSONL case that the runner reads.
_CASE_FIELDS = frozenset({"id", "input", "inputs", "output", "outputs", "expectations", "group"})
_DEFAULT_FUNCTION = runner.TargetRef.model_fields["function"].default
_DEFAULT_INPUT_KEY = runner.Suite.model_fields["input_key"].default
# Suite fields a suite file does not hold: loading it gives them from its name and its data, and
# who saved a stored version when is the store's, not the file's.
_DERIVED_ON_LOAD = frozenset(
    {"id", "name", "version", "digest", "dataset_name", "dataset_digest", "created_at", "created_by"}
)


def _problems(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'value'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False)
    )


# --- suite file to model ---------------------------------------------------------------------


def load_suite_file(path: str | Path, *, suite_id: str | None = None) -> tuple[Suite, list[Case]]:
    """A suite file and the cases its ``data`` names, in the model. The suite is named after the
    file, as a run names its MLflow experiment, and ``suite_id`` defaults to that name."""
    file_suite, base = runner.load_suite(path)
    data = Path(file_suite.data)
    cases = _cases(data if data.is_absolute() else base / data, file_suite.input_key)
    return _suite(file_suite, name=Path(path).stem, suite_id=suite_id, cases=cases), cases


def suite_from_yaml(text: str, *, name: str, cases: Sequence[Case], suite_id: str | None = None) -> Suite:
    """A suite file's text in the model, given the cases its ``data`` holds."""
    return _suite(runner.parse_suite(text), name=name, suite_id=suite_id, cases=list(cases))


def _cases(file: Path, input_key: str) -> list[Case]:
    rows = runner.load_cases(file, input_key=input_key)
    if file.suffix.lower() == ".jsonl":
        _refuse_unread_fields(file)
    cases = []
    for position, row in enumerate(rows, 1):
        try:
            cases.append(
                Case(
                    id=row["id"],
                    inputs=row["inputs"],
                    expectations=row["expectations"],
                    outputs=row.get("outputs"),
                    tags={runner.GROUP_TAG: row["group"]} if "group" in row else {},
                )
            )
        except ValidationError as error:
            raise ConfigError(f"case {position} does not fit the model: {_problems(error)}") from None
    return cases


def _refuse_unread_fields(file: Path) -> None:
    """A JSONL case field the runner does not read would be lost on the way into the model."""
    for number, line in enumerate(file.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)  # load_cases has already refused a line that is not an object
        if unknown := sorted(set(record) - _CASE_FIELDS):
            raise ConfigError(
                f"data line {number}: a case has no field {', '.join(unknown)}; "
                "its fields are id, input or inputs, output or outputs, expectations and group"
            )
        for one, other in (("input", "inputs"), ("output", "outputs")):
            if one in record and other in record:
                raise ConfigError(f"data line {number}: give {one} or {other}, not both")


def _suite(file_suite: runner.Suite, *, name: str, suite_id: str | None, cases: list[Case]) -> Suite:
    fields = _definition(file_suite)
    fields.update(
        id=suite_id or name,
        name=name,
        dataset_name=Path(file_suite.data).stem,
        # The digest a store gives these cases as a dataset version.
        dataset_digest=digest([case.model_dump(mode="json") for case in cases]),
    )
    try:
        draft = Suite(**fields, digest="0" * 64)
    except ValidationError as error:
        raise ConfigError(f"the suite does not fit the model: {_problems(error)}") from None
    return draft.model_copy(update={"digest": suite_digest(draft)})


def _definition(file_suite: runner.Suite) -> dict[str, Any]:
    """Everything a suite file says, as the model's suite fields."""
    if file_suite.judge is not None and not any(check.type in runner.JUDGES for check in file_suite.checks):
        raise ConfigError(
            f"judge: no check is an LLM judge ({', '.join(runner.JUDGES)}), so nothing uses it; remove it"
        )
    try:
        return {
            "dataset_location": file_suite.data,
            "input_key": file_suite.input_key,
            "target": _target(file_suite.target),
            "scorers": [_scorer(position, check, file_suite.judge) for position, check in enumerate(file_suite.checks)],
            "settings": _settings(file_suite),
            "repeats": Repeats(
                attempts=file_suite.repeats,
                combine=file_suite.combine,
                checks={check.label: check.combine for check in file_suite.checks if check.combine is not None},
            ),
            "matrix": dict(file_suite.matrix),
        }
    except ValidationError as error:
        raise ConfigError(f"the suite does not fit the model: {_problems(error)}") from None


def _target(reference: runner.TargetRef | None) -> Target:
    if reference is None:
        return Target(kind="stored")
    if reference.file is not None:
        return Target(kind="function", file=reference.file, function=reference.function)
    return Target(
        kind="http",
        url=reference.url,
        headers=dict(reference.headers),
        body=reference.body,
        response_path=reference.response_path or None,
    )


def _options(check: runner.CheckSpec) -> dict[str, Any]:
    """The options a check entry gives, under the names the file uses for them."""
    fields = type(check).model_fields
    given = check.model_fields_set - COMMON_CHECK_FIELDS
    return {fields[key].alias or key: getattr(check, key) for key in given}


def _scorer(position: int, check: runner.CheckSpec, judge: runner.JudgeConfig | None) -> ScorerVersion:
    options = _options(check)
    if unused := sorted(set(options) - CHECK_OPTIONS[check.type]):
        raise ConfigError(f"checks.{position}: {', '.join(unused)} does not apply to {check.type} checks; remove it")
    if check.type in runner.JUDGES:
        assert judge is not None  # the runner refuses a judge check without a judge
        criterion = getattr(check, check.type) if check.type in runner.CRITERIA_JUDGES else None
        return ScorerVersion(
            name=check.label,
            kind="llm_judge",
            type=check.type,
            model=judge.model,
            parameters=dict(judge.inference_params),
            criteria={} if criterion is None else {check.type: criterion},
        )
    return ScorerVersion(
        name=check.label, kind="check", type=check.type, parameters={key: options[key] for key in sorted(options)}
    )


def _settings(file_suite: runner.Suite) -> RunSettings:
    """Only the settings the file gives, so the runner's defaults stay its own."""
    values = {name: getattr(file_suite, name) for name in ("concurrency", "batch", "output")}
    values = {name: value for name, value in values.items() if name in file_suite.model_fields_set}
    target, judge, mlflow = file_suite.target, file_suite.judge, file_suite.mlflow
    if target is not None and "timeout_seconds" in target.model_fields_set:
        values["target_timeout_seconds"] = target.timeout_seconds
    for name in ("max_retries", "rate_limit_wait_seconds", "headers"):
        if judge is not None and name in judge.model_fields_set:
            values[f"judge_{name}"] = getattr(judge, name)
    for name in ("tracking_uri", "experiment"):
        if name in mlflow.model_fields_set:
            values[f"mlflow_{name}"] = getattr(mlflow, name)
    if (cache := file_suite.cache) is not None:
        values["cache"] = CacheSettings(**{name: getattr(cache, name) for name in cache.model_fields_set})
    return RunSettings(**values)


# --- model to suite file ---------------------------------------------------------------------


def suite_to_yaml(suite: Suite) -> str:
    """The suite as a suite file that loads back to the same definition. A suite the file cannot
    hold raises ``ConfigError`` naming what it cannot hold. The suite's id, name, version and
    dataset digest are not written: the file's name and its data give them on the next load."""
    text = yaml.safe_dump(_file(suite), sort_keys=False, allow_unicode=True)
    try:
        written = _definition(runner.parse_suite(text))
    except ConfigError as error:
        raise ConfigError(f"the suite cannot be written as a suite file: {error}") from None
    # Every field but those the next load derives must come back as it was, or as its default
    # when the file has no place for it, so a field this writer does not know is refused too.
    changed = [
        name
        for name, field in type(suite).model_fields.items()
        if name not in _DERIVED_ON_LOAD
        and getattr(suite, name) != written.get(name, field.get_default(call_default_factory=True))
    ]
    if changed:
        raise ConfigError(f"the suite cannot be written as a suite file without changing {', '.join(changed)}")
    return text


def cases_to_jsonl(cases: Sequence[Case]) -> str:
    """The cases as a JSONL data file for a suite file's ``data``."""
    if not cases:
        raise ConfigError("a data file needs at least one case")
    lines = []
    for position, case in enumerate(cases, 1):
        if set(case.tags) - {runner.GROUP_TAG} or case.metadata:
            raise ConfigError(f"case {position}: a data file has no place for tags other than its group, or metadata")
        row: dict[str, Any] = {"id": case.id, "inputs": case.inputs}
        if case.expectations:
            row["expectations"] = case.expectations
        if case.outputs is not None:
            row["outputs"] = case.outputs
        if runner.GROUP_TAG in case.tags:
            row["group"] = case.tags[runner.GROUP_TAG]
        lines.append(json.dumps(row, ensure_ascii=False) + "\n")
    return "".join(lines)


def _label(check_type: str) -> str:
    """The label the runner gives a check without a name."""
    return f"{check_type}_judge" if check_type in runner.JUDGES else check_type


def _file(suite: Suite) -> dict[str, Any]:
    if suite.dataset_location is None:
        raise ConfigError(
            "the suite names no data file: write its cases with cases_to_jsonl and set dataset_location to that file"
        )
    if suite.policies:
        raise ConfigError("a suite file has no pass policies: a case passes when every check passes it")
    settings = suite.settings
    file: dict[str, Any] = {"data": suite.dataset_location}
    if (target := _target_file(suite.target, settings)) is not None:
        file["target"] = target
    if suite.matrix:
        file["matrix"] = dict(suite.matrix)
    repeats = suite.repeats
    checks = [
        _check_file(position, scorer, repeats.checks.get(scorer.name)) for position, scorer in enumerate(suite.scorers)
    ]
    if (judge := _judge_file(suite.scorers, settings)) is not None:
        file["judge"] = judge
    file["checks"] = checks
    if repeats.attempts != 1:
        file["repeats"] = repeats.attempts
    if repeats.combine != "all":
        file["combine"] = repeats.combine
    for name in ("concurrency", "batch", "output"):
        if (value := getattr(settings, name)) is not None:
            file[name] = value
    if suite.input_key != _DEFAULT_INPUT_KEY:
        file["input_key"] = suite.input_key
    mlflow = {
        name: value
        for name in ("tracking_uri", "experiment")
        if (value := getattr(settings, f"mlflow_{name}")) is not None
    }
    if mlflow:
        file["mlflow"] = mlflow
    if settings.cache is not None:
        file["cache"] = {name: value for name, value in settings.cache.model_dump().items() if value is not None}
    return file


def _target_file(target: Target | None, settings: RunSettings) -> dict[str, Any] | None:
    if target is None:
        raise ConfigError(
            "the suite has no target; a suite file without one grades the outputs stored in its cases, "
            "which is a target of kind stored"
        )
    if target.kind == "agent":
        raise ConfigError("an agent target is an agent registered on the server, which a suite file cannot name")
    if settings.target_timeout_seconds is not None and target.kind != "http":
        raise ConfigError("settings.target_timeout_seconds applies only to an http target")
    if target.kind == "stored":
        return None
    if target.kind == "function":
        written: dict[str, Any] = {"file": target.file}
        if target.function != _DEFAULT_FUNCTION:
            written["function"] = target.function
        return written
    written = {"url": target.url}
    if target.headers:
        written["headers"] = dict(target.headers)
    written["body"] = target.body
    if target.response_path:
        written["response_path"] = target.response_path
    if settings.target_timeout_seconds is not None:
        written["timeout_seconds"] = settings.target_timeout_seconds
    return written


def _judge_file(scorers: list[ScorerVersion], settings: RunSettings) -> dict[str, Any] | None:
    judges = [scorer for scorer in scorers if scorer.kind == "llm_judge"]
    transport = {
        "max_retries": settings.judge_max_retries,
        "rate_limit_wait_seconds": settings.judge_rate_limit_wait_seconds,
        "headers": settings.judge_headers,
    }
    transport = {name: value for name, value in transport.items() if value is not None}
    if not judges:
        if transport:
            raise ConfigError(
                "settings.judge_max_retries, judge_rate_limit_wait_seconds and judge_headers need an llm_judge scorer"
            )
        return None
    if len({(scorer.model, digest(scorer.parameters)) for scorer in judges}) > 1:
        raise ConfigError("a suite file has one judge: every llm_judge scorer needs the same model and parameters")
    written: dict[str, Any] = {"model": judges[0].model}
    if judges[0].parameters:
        written["inference_params"] = dict(judges[0].parameters)
    return written | transport


def _check_file(position: int, scorer: ScorerVersion, combine: str | None) -> dict[str, Any]:
    where = f"scorers.{position}"
    if scorer.version:
        raise ConfigError(f"{where}: a suite file does not pin a scorer version")
    if scorer.kind == "llm_judge":
        if scorer.type not in runner.JUDGES:
            raise ConfigError(f"{where}: a suite file's LLM judges are {', '.join(runner.JUDGES)}")
        if scorer.type in runner.GROUNDING_JUDGES and scorer.criteria:
            raise ConfigError(f"{where}: {scorer.type} judges have no criteria")
        if set(scorer.criteria) - {scorer.type}:
            raise ConfigError(f"{where}: {scorer.type} judges keep their criteria under {scorer.type} only")
        options = dict(scorer.criteria)
    elif scorer.kind == "check":
        if scorer.type not in runner.CHECKS:
            raise ConfigError(f"{where}: a suite file's checks are {', '.join(runner.CHECKS)}")
        if scorer.criteria:
            raise ConfigError(f"{where}: {scorer.type} checks have no criteria")
        if unknown := sorted(set(scorer.parameters) - CHECK_OPTIONS[scorer.type]):
            raise ConfigError(f"{where}: {', '.join(unknown)} is not an option of {scorer.type} checks")
        options = dict(scorer.parameters)
    else:
        raise ConfigError(f"{where}: a {scorer.kind} scorer cannot be written in a suite file")
    written: dict[str, Any] = {"type": scorer.type}
    if scorer.name != _label(scorer.type):
        written["name"] = scorer.name
    if combine is not None:
        written["combine"] = combine
    return written | options


__all__ = [
    "CHECK_OPTIONS",
    "COMMON_CHECK_FIELDS",
    "SUITE_FILE_FIELDS",
    "cases_to_jsonl",
    "load_suite_file",
    "suite_from_yaml",
    "suite_to_yaml",
]
