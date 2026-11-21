"""Evaluate numeric expressions over the weighted totals of an Evals run."""

from __future__ import annotations

import math
import operator
import re
from collections.abc import Callable, Iterable, Mapping
from functools import lru_cache
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_COUNT = "__count"
_MAX_LENGTH = 1024
_DIGITS = r"[0-9](?:_?[0-9])*"
_FRACTION = rf"\.(?:{_DIGITS})?"
_EXPONENT = rf"(?:[eE][+-]?{_DIGITS})?"
_DECIMAL = rf"(?:(?:0|[1-9](?:_?[0-9])*)(?:{_FRACTION})?|\.{_DIGITS}){_EXPONENT}"
_BASE = r"0(?:[xX][0-9a-fA-F](?:_?[0-9a-fA-F])*|[bB][01](?:_?[01])*|[oO][0-7](?:_?[0-7])*)"
_LEGACY = rf"0[0-7]*[89][0-9]*(?:{_FRACTION})?{_EXPONENT}|0[0-7]+"
_TOKENS = re.compile(
    rf"(?P<space>[\s\ufeff]+)|(?P<number>{_BASE}|{_LEGACY}|{_DECIMAL})|(?P<name>(?:[^\W\d]|\$)[\w$]*)|"
    r"(?P<operator>\+\+|--|<=|>=|==|!=|[+*/<>()?:-])|(?P<invalid>.)"
)
_KEYWORDS = frozenset(
    [
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "function",
        "if",
        "import",
        "in",
        "instanceof",
        "new",
        "null",
        "return",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "with",
    ]
)

_Formula = Callable[[Mapping[str, float]], float]
_Operation = Callable[[float, float], float]


def _divide(left: float, right: float) -> float:
    # Preserve numeric comparison and branching on zero denominators. The final score must be finite.
    if right != 0:
        return left / right
    if left == 0 or math.isnan(left):
        return math.nan
    return math.copysign(math.inf, math.copysign(1, left) * math.copysign(1, right))


_COMPARISONS: dict[str, _Operation] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}
_OPERATIONS: dict[str, _Operation] = {
    **_COMPARISONS,
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": _divide,
}
_PRECEDENCE = {**dict.fromkeys(_COMPARISONS, 1), "+": 2, "-": 2, "*": 3, "/": 3}


def _apply(op: _Operation, left: _Formula, right: _Formula) -> _Formula:
    return lambda scores: float(op(left(scores), right(scores)))


def _choose(test: _Formula, yes: _Formula, no: _Formula) -> _Formula:
    def evaluate(scores: Mapping[str, float]) -> float:
        value = test(scores)
        return no(scores) if value == 0 or math.isnan(value) else yes(scores)

    return evaluate


class _Parser:
    """Parse only literals, score references, and the operators in the tables above."""

    def __init__(self, source: str) -> None:
        self.tokens = iter(
            (token.lastgroup, token[0]) for token in _TOKENS.finditer(source) if token.lastgroup != "space"
        )
        self.current = next(self.tokens, (None, ""))
        self.references: set[str] = set()

    def take(self) -> tuple[str | None, str]:
        token = self.current
        self.current = next(self.tokens, (None, ""))
        return token

    def expect(self, symbol: str) -> None:
        if self.take()[1] != symbol:
            raise ValueError(f"expected {symbol}")

    def atom(self) -> _Formula:
        kind, token = self.take()
        if kind == "number":
            raw = token.replace("_", "")
            if raw.lower().startswith(("0x", "0b", "0o")):
                value = float(int(raw, 0))
            elif re.fullmatch(r"0[0-7]+", raw):
                value = float(int(raw, 8))
            else:
                value = float(raw)
            if not math.isfinite(value):
                raise ValueError("numeric literals must be finite")
            return lambda _: value
        if kind == "name" and token not in _KEYWORDS:
            self.references.add(token)
            return lambda scores: scores[token]
        if token == "(":
            value_from, _ = self.expression()
            self.expect(")")
            return value_from
        if token in ("+", "-"):
            operand = self.atom()
            return operand if token == "+" else lambda scores: -operand(scores)
        raise ValueError("expected a number, metric name or parenthesized expression")

    def expression(self, minimum: int = 0) -> tuple[_Formula, bool]:
        left, compared = self.atom(), False
        while (precedence := _PRECEDENCE.get(self.current[1], -1)) >= minimum:
            _, symbol = self.take()
            right, right_compared = self.expression(precedence + 1)
            comparison = symbol in _COMPARISONS
            if comparison and (compared or right_compared):
                raise ValueError("use separate comparisons in a conditional")
            left, compared = _apply(_OPERATIONS[symbol], left, right), comparison
        if minimum == 0 and self.current[1] == "?":
            self.take()
            yes, _ = self.expression()
            self.expect(":")
            no, _ = self.expression()
            return _choose(left, yes, no), False
        return left, compared


@lru_cache(maxsize=256)
def _compile(formula: str) -> tuple[_Formula, frozenset[str]]:
    if len(formula.encode("utf-16-le", errors="surrogatepass")) > 2 * _MAX_LENGTH:
        raise ValueError(f"formula exceeds {_MAX_LENGTH} characters")
    parser = _Parser(formula)
    try:
        evaluate, _ = parser.expression()
    except RecursionError:
        raise ValueError("formula is nested too deeply") from None
    except OverflowError:
        raise ValueError("numeric literals must be finite") from None
    if parser.current[0] is not None:
        raise ValueError("expected a single numeric expression")
    return evaluate, frozenset(parser.references)


class Formula(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=_MAX_LENGTH)


class DerivedMetrics(BaseModel):
    """An ordered set of numeric metrics, over a run or its dataset groups."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    formulas: tuple[Formula, ...] = Field(min_length=1)
    group_by: Literal["group"] | None = None

    @model_validator(mode="after")
    def valid_formulas(self) -> Self:
        names = {formula.name for formula in self.formulas}
        if len(names) != len(self.formulas) or _COUNT in names:
            raise ValueError(f"derived metric names must be unique and cannot use {_COUNT}")
        earlier: set[str] = set()
        for formula in self.formulas:
            try:
                _, referenced = _compile(formula.value)
            except ValueError as error:
                raise ValueError(f"{formula.name}: {error}") from None
            if unavailable := sorted(referenced & (names - earlier)):
                raise ValueError(f"{formula.name}: define {', '.join(unavailable)} before using it")
            earlier.add(formula.name)
        return self

    def validate_measurements(self, names: Iterable[str]) -> None:
        if overlap := sorted({formula.name for formula in self.formulas}.intersection(names)):
            raise ValueError(f"derived metrics reuse measured metric names: {', '.join(overlap)}")

    def report(self, summaries: list[dict[str, Any]], *, cases: int, group: str | None = None) -> list[dict[str, Any]]:
        inputs: dict[str, float] = {
            row["name"]: row["score_total"] for row in summaries if row["score_total"] is not None
        }
        inputs[_COUNT] = float(cases)
        reports: list[dict[str, Any]] = []
        for formula in self.formulas:
            evaluate, _ = _compile(formula.value)
            score, error = None, None
            try:
                result = evaluate(inputs)
                inputs[formula.name] = result
                if math.isfinite(result):
                    score = result
                else:
                    error = "Calculation did not produce a finite score"
            except KeyError as missing:
                error = f"Missing measurement: {missing.args[0]}"
            except RecursionError:
                error = "Formula is nested too deeply"
            reports.append(
                {
                    "name": formula.name,
                    "formula": formula.value,
                    "group": group,
                    "cases": cases,
                    "score": score,
                    "error": error,
                }
            )
        return reports
