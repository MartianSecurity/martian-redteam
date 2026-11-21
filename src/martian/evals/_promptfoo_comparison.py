"""Evidence for comparing saved Promptfoo cases with local deterministic checks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import TypeAdapter

from martian.evals.case_gate import CASE_GATE, CaseChecks, CaseGate, case_check_index, gate_definition, validate_gates
from martian.evals.model import CaseResult, Run, ScorerVersion, digest

COMPARISON_BASIS = "promptfoo_deterministic"
CHECKS = frozenset(
    {
        "contains",
        "not_contains",
        "matches",
        "not_matches",
        "is_json",
        "is_xml",
        "json_schema",
        "levenshtein",
        "word_count",
    }
)
_HTTP_INPUT = re.compile(r"promptfoo_field_[0-9]+")
_CHECK_SETS = TypeAdapter(list[CaseChecks])


def case_contract(
    inputs: dict[str, Any],
    expectations: dict[str, Any],
    scorers: Sequence[ScorerVersion],
    gate: CaseGate | None,
) -> str | None:
    """Identify the rendered inputs and effective deterministic scoring rules of a case."""
    prompt, variables = inputs.get("prompt"), inputs.get("vars")
    if not isinstance(prompt, str) or not isinstance(variables, dict):
        return None
    if any(key not in {"prompt", "vars", "promptfoo_messages"} and not _HTTP_INPUT.fullmatch(key) for key in inputs):
        return None
    if not scorers:
        return None
    used: set[str] = set()
    checks: list[tuple[str, str, dict[str, Any]]] = []
    for scorer in scorers:
        if (
            scorer.kind != "check"
            or scorer.type not in CHECKS
            or scorer.scope != "turn"
            or scorer.version
            or scorer.criteria
        ):
            return None
        options = dict(scorer.parameters)
        reference = options.pop("expectation", None)
        if reference is not None:
            if not isinstance(reference, str) or "values" in options:
                return None
            values = expectations.get(reference)
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                return None
            used.add(reference)
            options["values"] = values
        checks.append((scorer.name, scorer.type, options))
    if used != set(expectations):
        return None
    # The order of a weighted sum can matter. An all-checks gate only needs the same checks.
    rules = None if gate is None else gate.scoring_rules()
    checks = checks if rules is not None else sorted(checks, key=lambda check: check[0])
    content = {
        # Earlier XML imports could record a signature without rechecking the source's XML verdict.
        "version": 2 if any(kind == "is_xml" for _, kind, _ in checks) else 1,
        "prompt": prompt,
        "vars": variables,
        "checks": checks,
        "gate": rules,
    }
    try:
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _contracts(run: Run, cases: Sequence[CaseResult]) -> dict[str, str | None]:
    if run.provenance.get("promptfoo"):
        return {case.case_id: case.promptfoo_contract for case in cases}
    if not run.provenance.get("suite") or any(key in run.provenance for key in ("repeats", "matrix", "simulator")):
        return {}
    scorers = {scorer.name: scorer for scorer in run.scorers if scorer.type != CASE_GATE}
    try:
        value = run.provenance.get(CASE_GATE)
        gate = None if value is None else CaseGate.model_validate(value)
        check_sets = _CHECK_SETS.validate_python(run.provenance.get("check_sets", []))
        validate_gates(gate, check_sets, scorers.keys())
        assigned = case_check_index(check_sets)
    except ValueError:
        return {}
    if definition := gate_definition(gate, check_sets):
        gate_version = digest(definition)
        recorded = [scorer for scorer in run.scorers if scorer.type == CASE_GATE]
        if len(recorded) != 1 or recorded[0].name != CASE_GATE or recorded[0].version != gate_version:
            return {}
    elif len(scorers) != len(run.scorers):
        return {}
    contracts: dict[str, str | None] = {}
    for case in cases:
        if case.attempts or case.turns or case.simulated:
            continue
        if not check_sets:
            contracts[case.case_id] = case_contract(case.inputs, case.expectations, list(scorers.values()), gate)
        elif (selected := assigned.get(case.case_id)) is not None:
            aliases = selected.aliases
            checks = [scorers[name].model_copy(update={"name": aliases.get(name, name)}) for name in selected.checks]
            rules = selected.case_gate.rename_checks(aliases) if selected.case_gate is not None else None
            contracts[case.case_id] = case_contract(case.inputs, case.expectations, checks, rules)
    return contracts


def promptfoo_mismatch(
    baseline: Run, before: Sequence[CaseResult], candidate: Run, after: Sequence[CaseResult]
) -> str | None:
    """The reason a migration comparison is unavailable, or None for verified shared cases."""
    if (
        baseline.project_id != candidate.project_id
        or baseline.source != "local"
        or candidate.source != "local"
        or not (baseline.provenance.get("promptfoo") or candidate.provenance.get("promptfoo"))
    ):
        return "Promptfoo migration comparisons require local runs in the same project."
    before_by_id = {case.case_id: case for case in before}
    after_by_id = {case.case_id: case for case in after}
    shared = before_by_id.keys() & after_by_id.keys()
    if not shared:
        return "The runs have no shared Promptfoo cases."
    if any(
        before_by_id[identity].inputs.get("prompt") != after_by_id[identity].inputs.get("prompt")
        or before_by_id[identity].inputs.get("vars") != after_by_id[identity].inputs.get("vars")
        for identity in shared
    ):
        return "The shared cases have different rendered prompts or variables."
    left = _contracts(baseline, [before_by_id[identity] for identity in shared])
    right = _contracts(candidate, [after_by_id[identity] for identity in shared])
    if any(left.get(identity) is None or right.get(identity) is None for identity in shared):
        return "A shared case lacks verified deterministic checks or complete grading evidence."
    if any(left[identity] != right[identity] for identity in shared):
        return "The shared cases use different deterministic checks, weights or thresholds."
    return None
