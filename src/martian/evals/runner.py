"""Run an eval suite from one YAML file, locally, with only a model key.

    martian evals run evals.yaml

The suite names a data file (JSONL or CSV), the checks to apply, and optionally the target under
test: a Python function, or an HTTP endpoint. Nothing here contacts the Martian platform: a
function target runs in this process, a URL target is called from it, LLM judges call the model
you configure, and MLflow uses a local store in the output folder unless you point it at your
own server. A run can be stopped and started again; finished cases are
skipped. Use ``martian ci`` when a result must be recorded on the platform.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from martian.config import ConfigError

JUDGES = ("rubric", "guidelines", "conditions")
CHECKS = ("contains", "not_contains", "matches", "not_matches", "is_json")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# In a URL target's body: {{input}}, {{inputs.NAME}} and {{uuid}} are filled in for each case.
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_PLACEHOLDER_NAME = re.compile(r"input|uuid|inputs\.[A-Za-z_][A-Za-z0-9_]*")
# In a URL target's url and header values: ${NAME} is read from the environment.
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _placeholders(value: Any) -> list[str]:
    """Every {{name}} in the string values of a body template."""
    if isinstance(value, str):
        return _PLACEHOLDER.findall(value)
    if isinstance(value, dict):
        return [name for item in value.values() for name in _placeholders(item)]
    if isinstance(value, list):
        return [name for item in value for name in _placeholders(item)]
    return []


class TargetCallError(RuntimeError):
    """A URL target call failed. The message is the SDK's own wording: it names what went wrong
    and never holds the address, a header, a credential or anything the server sent back."""


def _check_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        raise ValueError("target.url is not a valid address") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("target.url must be an http or https address")
    if parts.username is not None or parts.password is not None:
        raise ValueError("target.url must not contain credentials; send them in target.headers")


class TargetRef(_Model):
    """The thing under test: a Python function in a file, or an HTTP endpoint that takes a JSON POST."""

    file: str | None = None
    function: str = "target"
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    response_path: str | None = None
    timeout_seconds: float = Field(default=120, ge=1, le=600)

    @model_validator(mode="after")
    def one_kind(self) -> TargetRef:
        if (self.file is None) == (self.url is None):
            raise ValueError("a target needs a file or a url, not both")
        given = self.model_fields_set
        if self.file is not None:
            extra = sorted(given & {"headers", "body", "response_path", "timeout_seconds"})
            if extra:
                raise ValueError(f"{', '.join(extra)} only applies to a url target")
            return self
        if "function" in given:
            raise ValueError("function only applies to a file target")
        if not _ENV_REFERENCE.search(self.url or ""):  # otherwise checked once the variables are read
            _check_url(self.url or "")
        if not self.body:
            raise ValueError("a url target needs a body, with {{input}} where the case's input goes")
        names = _placeholders(self.body)
        unknown = sorted({name for name in names if not _PLACEHOLDER_NAME.fullmatch(name)})
        if unknown:
            allowed = "{{input}}, {{inputs.NAME}} or {{uuid}}"
            raise ValueError(f"unknown body placeholder(s) {', '.join(unknown)}; use {allowed}")
        if not any(name != "uuid" for name in names):
            raise ValueError("a url target's body must use {{input}} or {{inputs.NAME}}, or every case sends the same")
        return self


class JudgeConfig(_Model):
    model: str
    inference_params: dict[str, float | int] = Field(default_factory=dict)
    max_retries: int = Field(default=5, ge=0, le=10)


class CheckSpec(_Model):
    type: Literal["rubric", "guidelines", "conditions", "contains", "not_contains", "matches", "not_matches", "is_json"]
    name: str | None = None
    # Criteria for an LLM judge, applied to every row that does not carry its own.
    rubric: str | None = None
    guidelines: list[str] | None = None
    conditions: list[dict[str, str]] | None = None
    # Deterministic checks.
    values: list[str] = Field(default_factory=list)
    expectation: str | None = None
    any_of: bool = False
    case_sensitive: bool = False
    pattern: str | None = None
    required_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete(self) -> CheckSpec:
        if self.type in ("matches", "not_matches") and not self.pattern:
            raise ValueError(f"a {self.type} check needs a pattern")
        if self.type in ("contains", "not_contains") and not self.values and not self.expectation:
            raise ValueError(f"a {self.type} check needs values or an expectation key")
        return self

    @property
    def label(self) -> str:
        return self.name or (f"{self.type}_judge" if self.type in JUDGES else self.type)


class MlflowConfig(_Model):
    tracking_uri: str | None = None
    experiment: str | None = None


class Suite(_Model):
    """The YAML file. Paths are relative to the file itself."""

    data: str
    checks: list[CheckSpec] = Field(min_length=1)
    target: TargetRef | None = None
    judge: JudgeConfig | None = None
    concurrency: int = Field(default=4, ge=1, le=64)
    batch: int = Field(default=25, ge=1, le=500)
    output: str = "results"
    input_key: str = "message"
    mlflow: MlflowConfig = Field(default_factory=MlflowConfig)

    @model_validator(mode="after")
    def consistent(self) -> Suite:
        labels = [check.label for check in self.checks]
        if len(set(labels)) != len(labels):
            raise ValueError("check names must be unique; give repeated check types a name")
        kinds = [check.type for check in self.checks if check.type in JUDGES]
        if len(set(kinds)) != len(kinds):
            raise ValueError("use at most one check of each judge type (rubric, guidelines, conditions)")
        if kinds and self.judge is None:
            raise ValueError("LLM judge checks need a judge.model")
        return self


def load_suite(path: str | Path) -> tuple[Suite, Path]:
    file = Path(path).resolve()
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"could not read the suite file: {type(error).__name__}") from None
    if not isinstance(raw, dict):
        raise ConfigError("the suite file must be a YAML mapping")
    try:
        return Suite.model_validate(raw), file.parent
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'suite'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid suite file: {problems}") from None


def load_cases(path: str | Path, *, input_key: str = "message") -> list[dict[str, Any]]:
    """Read cases from JSONL or CSV into MLflow rows with a stable ``id``.

    JSONL: one object per line with ``inputs`` (an object) or ``input`` (a string), and optional
    ``id``, ``outputs``/``output`` and ``expectations``.
    CSV: columns ``input``, optional ``id`` and ``output``; every other non-empty column becomes
    an expectation of that name.
    """
    file = Path(path)
    suffix = file.suffix.lower()
    if suffix not in (".jsonl", ".csv"):
        raise ConfigError("the data file must be .jsonl or .csv")
    try:
        text = file.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ConfigError(f"could not read the data file: {type(error).__name__}") from None
    records: list[dict[str, Any]] = []
    if suffix == ".jsonl":
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                raise ConfigError(f"data line {number} is not valid JSON") from None
            if not isinstance(record, dict):
                raise ConfigError(f"data line {number} must be a JSON object")
            records.append(record)
    else:
        for record in csv.DictReader(text.splitlines()):
            known = {key: record.get(key) for key in ("id", "input", "output")}
            expectations = {k: v for k, v in record.items() if k not in known and k and v not in (None, "")}
            records.append({**{k: v for k, v in known.items() if v not in (None, "")}, "expectations": expectations})
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, record in enumerate(records, 1):
        inputs = record.get("inputs")
        if inputs is None and isinstance(record.get("input"), str):
            inputs = {input_key: record["input"]}
        if not isinstance(inputs, dict) or not inputs:
            raise ConfigError(f"case {position} needs `inputs` (an object) or `input` (a string)")
        identifier = str(record.get("id") or position)
        if identifier in seen:
            raise ConfigError(f"case id {identifier!r} appears more than once")
        seen.add(identifier)
        expectations = record.get("expectations") or {}
        if not isinstance(expectations, dict):
            raise ConfigError(f"case {identifier}: `expectations` must be an object")
        case: dict[str, Any] = {"id": identifier, "inputs": inputs, "expectations": dict(expectations)}
        outputs = record.get("outputs", record.get("output"))
        if outputs is not None:
            case["outputs"] = outputs
        cases.append(case)
    if not cases:
        raise ConfigError("the data file has no cases")
    return cases


def _from_environment(text: str) -> str:
    """Replace ${NAME} with the environment's value. The suite file itself holds no secrets."""

    def value(match: re.Match[str]) -> str:
        found = os.environ.get(match[1])
        if not found:
            raise ConfigError(f"the target needs the environment variable {match[1]}")
        return found

    return _ENV_REFERENCE.sub(value, text)


def _fill(template: Any, inputs: dict[str, Any], input_key: str) -> Any:
    """One case's request body. Case text is inserted as data and never expanded itself."""

    def lookup(name: str) -> Any:
        if name == "uuid":
            return str(uuid.uuid4())
        key = input_key if name == "input" else name.removeprefix("inputs.")
        if key not in inputs:
            raise TargetCallError(f"the case has no input named {key!r}")
        return inputs[key]

    if isinstance(template, dict):
        return {key: _fill(item, inputs, input_key) for key, item in template.items()}
    if isinstance(template, list):
        return [_fill(item, inputs, input_key) for item in template]
    if not isinstance(template, str):
        return template
    whole = _PLACEHOLDER.fullmatch(template)
    if whole:  # keeps a number or an object as it is, rather than its text
        return lookup(whole[1])
    return _PLACEHOLDER.sub(
        lambda match: found if isinstance(found := lookup(match[1]), str) else json.dumps(found), template
    )


def _http_target(reference: TargetRef, input_key: str) -> Callable[..., Any]:
    """Call the endpoint once per case: no retry, no redirect, no proxy from the environment.

    Errors name what went wrong but never the address, the headers or the response body. The
    returned function has a ``failure_reason(inputs)`` attribute, so the runner can report why
    a case's call failed.
    """
    url = _from_environment(reference.url or "")
    try:
        _check_url(url)
    except ValueError as error:
        raise ConfigError(str(error)) from None
    headers = {name: _from_environment(value) for name, value in reference.headers.items()}
    steps = [part for part in (reference.response_path or "").split(".") if part]

    def send(inputs: dict[str, Any]) -> Any:
        body = _fill(reference.body, inputs, input_key)
        try:
            with httpx.Client(timeout=reference.timeout_seconds, follow_redirects=False, trust_env=False) as client:
                response = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as error:
            raise TargetCallError(f"the target could not be reached: {type(error).__name__}") from None
        if not response.is_success:
            raise TargetCallError(f"the target returned HTTP {response.status_code}")
        if not steps:
            return response.text
        try:
            reply = response.json()
        except ValueError:
            raise TargetCallError("the target's response is not JSON, so response_path cannot be read") from None
        for step in steps:
            if isinstance(reply, dict) and step in reply:
                reply = reply[step]
            elif isinstance(reply, list) and step.isdigit() and int(step) < len(reply):
                reply = reply[int(step)]
            else:
                raise TargetCallError("the target's response has no value at response_path")
        if reply is None:
            raise TargetCallError("the target's response has no value at response_path")
        return reply

    # Why each failed call failed, by the inputs it was given: the evaluator reports only that
    # a call failed. Two cases with identical inputs send identical requests, so sharing is safe.
    reasons: dict[str, list[str]] = {}
    lock = threading.Lock()

    def key(inputs: dict[str, Any]) -> str:
        return json.dumps(inputs, sort_keys=True, default=str)

    def call(**inputs: Any) -> Any:
        try:
            return send(inputs)
        except TargetCallError as error:
            with lock:
                reasons.setdefault(key(inputs), []).append(str(error))
            raise

    def failure_reason(inputs: dict[str, Any]) -> str | None:
        with lock:
            found = reasons.get(key(inputs))
            return found.pop(0) if found else None

    call.failure_reason = failure_reason  # type: ignore[attr-defined]
    return call


def _load_target(reference: TargetRef, base: Path, input_key: str = "message") -> Callable[..., Any]:
    if reference.url is not None:
        return _http_target(reference, input_key)
    file = Path(reference.file or "")
    file = file if file.is_absolute() else (base / file)
    spec = importlib.util.spec_from_file_location("martian_evals_target", file)
    if spec is None or spec.loader is None or not file.is_file():
        raise ConfigError("could not load the target file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # noqa: BLE001 - the customer's module may raise anything on import
        raise ConfigError(f"the target file failed to import: {type(error).__name__}") from None
    function: Callable[..., Any] | None = getattr(module, reference.function, None)
    if function is None or not callable(function):
        raise ConfigError(f"the target file has no function {reference.function!r}")
    return function


def _build_scorers(suite: Suite) -> list[Any]:
    from martian.evals import checks as deterministic
    from martian.evals import judges

    built = []
    for check in suite.checks:
        if check.type in JUDGES:
            assert suite.judge is not None
            factory = {
                "rubric": judges.response_rubric,
                "guidelines": judges.response_guidelines,
                "conditions": judges.response_conditions,
            }[check.type]
            built.append(
                factory(
                    model=suite.judge.model,
                    name=check.label,
                    input_key=suite.input_key,
                    inference_params=dict(suite.judge.inference_params) or None,
                    max_retries=suite.judge.max_retries,
                )
            )
        elif check.type == "contains":
            built.append(
                deterministic.contains(
                    *check.values,
                    expectation=check.expectation,
                    name=check.label,
                    any_of=check.any_of,
                    case_sensitive=check.case_sensitive,
                )
            )
        elif check.type == "not_contains":
            built.append(
                deterministic.not_contains(
                    *check.values, expectation=check.expectation, name=check.label, case_sensitive=check.case_sensitive
                )
            )
        elif check.type == "matches":
            built.append(deterministic.matches(check.pattern or "", name=check.label))
        elif check.type == "not_matches":
            built.append(deterministic.not_matches(check.pattern or "", name=check.label))
        else:
            built.append(deterministic.is_json(required_keys=check.required_keys, name=check.label))
    return built


def _with_suite_criteria(case: dict[str, Any], suite: Suite) -> dict[str, Any]:
    """Fill in criteria given once in the suite file for rows that do not carry their own."""
    expectations = dict(case["expectations"])
    for check in suite.checks:
        for key in ("rubric", "guidelines", "conditions"):
            value = getattr(check, key)
            if check.type == key and value is not None and key not in expectations:
                expectations[key] = value
    return expectations


def _missing(value: Any) -> bool:
    return value is None or value != value  # NaN


class Results:
    """``results.jsonl`` in the output folder: one record per case, the latest record wins."""

    def __init__(self, folder: Path, *, resume: bool) -> None:
        self.folder = folder
        self.path = folder / "results.jsonl"
        folder.mkdir(parents=True, exist_ok=True)
        if not resume and self.path.exists():
            self.path.unlink()

    def records(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    found[record["id"]] = record
        return found

    def append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _summary(records: list[dict[str, Any]], labels: list[str], seconds: float) -> dict[str, Any]:
    per_check = {
        label: {
            "passed": sum(r["checks"].get(label, {}).get("passed") is True for r in records),
            "failed": sum(r["checks"].get(label, {}).get("passed") is False for r in records),
            "errors": sum(r["checks"].get(label, {}).get("passed") is None for r in records),
        }
        for label in labels
    }
    target_errors: dict[str, int] = {}
    for record in records:
        if record.get("error"):
            target_errors[str(record["error"])] = target_errors.get(str(record["error"]), 0) + 1
    return {
        "cases": len(records),
        "passed": sum(r["passed"] is True for r in records),
        "failed": sum(r["passed"] is False for r in records),
        "errors": sum(r["passed"] is None for r in records),
        "checks": per_check,
        "target_errors": target_errors,
        "seconds_this_run": round(seconds, 1),
    }


def write_junit(records: list[dict[str, Any]], path: Path, *, suite_name: str) -> None:
    """JUnit XML for CI systems: a failed check is a failure, a case without a verdict an error."""
    failures = sum(r["passed"] is False for r in records)
    errors = sum(r["passed"] is None for r in records)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name={quoteattr(suite_name)} tests="{len(records)}" failures="{failures}" errors="{errors}">',
    ]
    for record in records:
        lines.append(f"  <testcase classname={quoteattr(suite_name)} name={quoteattr(str(record['id']))}>")
        details = [
            f"{label}: {check.get('rationale') or check.get('error') or 'no detail'}"
            for label, check in record["checks"].items()
            if check.get("passed") is not True
        ]
        message = str(record["error"]) if record.get("error") else "; ".join(details) or "no verdict"
        if record["passed"] is False:
            lines.append(f"    <failure message={quoteattr(message[:2000])}/>")
        elif record["passed"] is None:
            lines.append(f"    <error message={quoteattr(message[:2000])}/>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_suite(
    path: str | Path,
    *,
    output: str | Path | None = None,
    concurrency: int | None = None,
    limit: int | None = None,
    resume: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the suite and return its summary. Writes results.jsonl, summary.json and junit.xml."""
    suite, base = load_suite(path)
    cases = load_cases(
        base / suite.data if not Path(suite.data).is_absolute() else suite.data, input_key=suite.input_key
    )
    if limit is not None:
        cases = cases[:limit]
    stored = [case for case in cases if "outputs" in case]
    if suite.target is None and len(stored) != len(cases):
        raise ConfigError("without a target, every case needs an `outputs` (or `output`) value to grade")
    if suite.target is not None and stored:
        raise ConfigError("the data file has stored outputs and the suite also names a target; use one or the other")
    target = _load_target(suite.target, base, suite.input_key) if suite.target is not None else None
    failure_reason: Callable[[dict[str, Any]], str | None] | None = getattr(target, "failure_reason", None)
    scorers = _build_scorers(suite)
    labels = [check.label for check in suite.checks]

    folder = Path(output) if output is not None else base / suite.output
    results = Results(folder, resume=resume)
    done = {i for i, r in results.records().items() if r["passed"] is not None}
    todo = [case for case in cases if case["id"] not in done]
    say = progress or (lambda message: None)
    say(f"{len(cases)} cases, {len(cases) - len(todo)} already done")

    import mlflow

    from martian.evals.evaluate import evaluate
    from martian.evals.gate import _interpret
    from martian.evals.scorers import output_text

    # Leave MLflow as it was found: another evaluation in this process must not inherit this
    # run's store, experiment or worker settings.
    previous_uri = mlflow.get_tracking_uri()
    previous_experiment = mlflow.tracking.fluent._active_experiment_id
    restored = ("MLFLOW_GENAI_EVAL_MAX_WORKERS", "MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "MLFLOW_EXPERIMENT_ID")
    previous_env = {name: os.environ.get(name) for name in restored}
    os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = str(concurrency or suite.concurrency)
    os.environ["MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION"] = "true"  # otherwise the first case runs twice
    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
    started = time.monotonic()
    try:
        mlflow.set_tracking_uri(suite.mlflow.tracking_uri or f"sqlite:///{folder.resolve() / 'mlflow.db'}")
        mlflow.set_experiment(suite.mlflow.experiment or Path(path).stem)
        for start in range(0, len(todo), suite.batch):
            batch = todo[start : start + suite.batch]
            data = []
            for case in batch:
                row: dict[str, Any] = {
                    "inputs": case["inputs"],
                    "expectations": _with_suite_criteria(case, suite),
                    "tags": {"martian_case_id": case["id"]},
                }
                if "outputs" in case:
                    row["outputs"] = case["outputs"]
                data.append(row)
            frame: Any = evaluate(
                data=data, scorers=scorers, predict_fn=target
            ).result_df  # never None: evaluate raises
            by_id = {case["id"]: case for case in batch}
            for position in range(len(frame)):
                case = by_id[str((frame["tags"].iloc[position] or {}).get("martian_case_id"))]
                response = frame["response"].iloc[position] if "response" in frame.columns else None
                state = str(frame["state"].iloc[position]).upper() if "state" in frame.columns else "OK"
                broke = _missing(response) or "ERROR" in state
                # A function target's exception may hold anything, so only a URL target gives a reason.
                reason = (failure_reason(case["inputs"]) if broke and failure_reason else None) or (
                    "the target call failed" if broke else None
                )
                verdicts: dict[str, dict[str, Any]] = {}
                for label in labels:
                    value = frame[f"{label}/value"].iloc[position] if f"{label}/value" in frame.columns else None
                    rationale = (
                        frame[f"{label}/rationale"].iloc[position] if f"{label}/rationale" in frame.columns else None
                    )
                    detail = (
                        frame[f"{label}/error_message"].iloc[position]
                        if f"{label}/error_message" in frame.columns
                        else None
                    )
                    passed = None if broke else _interpret(value, None)
                    verdicts[label] = {
                        "passed": passed,
                        "rationale": None if _missing(rationale) else str(rationale),
                        "error": None
                        if passed is not None
                        else (
                            "the target failed"
                            if broke
                            else str(detail)[:300]
                            if not _missing(detail)
                            else "no verdict"
                        ),
                    }
                values = [verdict["passed"] for verdict in verdicts.values()]
                results.append(
                    {
                        "id": case["id"],
                        "passed": None if any(v is None for v in values) else all(values),
                        "checks": verdicts,
                        "inputs": case["inputs"],
                        "output": None if broke else output_text(response),
                        "error": reason,
                    }
                )
            say(f"{min(start + suite.batch, len(todo))}/{len(todo)} cases graded")
    finally:
        mlflow.set_tracking_uri(previous_uri)
        vars(mlflow.tracking.fluent)["_active_experiment_id"] = previous_experiment
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    final = results.records()
    ordered = [final[case["id"]] for case in cases if case["id"] in final]
    summary = _summary(ordered, labels, time.monotonic() - started)
    summary["output"] = str(folder)
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    write_junit(ordered, folder / "junit.xml", suite_name=Path(path).stem)
    return summary
