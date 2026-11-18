"""Numbers and tables read out of an output, for the ``number_equals``, ``sums_to``,
``reconciles`` and ``table_equals`` checks.

Every figure is a :class:`~decimal.Decimal`, read from what was written rather than through a
binary float, and every sum and difference is exact: one that would need rounding is an error,
so a one-cent difference is always exactly one cent. This module imports nothing from MLflow,
so the suite file runner checks the options with it when the suite loads.

Paths are written as a URL target's ``response_path`` is: dotted steps, each an object key or a
list index. In the paths whose numbers are added up, the step ``*`` is every item of a list.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal, Inexact, localcontext
from typing import Any

from martian.evals._digits import ascii_digits

# A number written as text: an optional sign, then a currency symbol or a three-letter currency
# code, digits with commas only between groups of three, an optional fraction after a point,
# then an optional % or currency code. Parentheses around the whole of it make it negative.
_MINUS = "-−"
_TEXT_NUMBER = re.compile(
    r"(?P<sign>[-+−])?"
    r"(?:(?P<symbol>[$€£¥])|(?P<code>[A-Z]{3}) ?)?"
    r"(?P<sign_after>[-+−])?"
    r"(?P<whole>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?P<fraction>\.[0-9]+)?"
    r"(?: ?(?P<suffix>%|[A-Z]{3}))?"
)
# A number inside prose. Parentheses and letter codes are left out: prose uses them for other things.
_NUMBER_IN_TEXT = re.compile(
    r"(?<![\w.,])[-+−]?[$€£¥]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?%?(?!\w|[.,][0-9])"
)
# Sums and differences keep this many significant digits; one that needs more is an error.
_DIGITS = 100


class Unreadable(Exception):
    """The output does not hold what the check reads, so the check fails with this reason."""


# --- figures ----------------------------------------------------------------------------------


def read_text(text: str) -> Decimal | None:
    """The number a text is, such as ``1234.56``, ``$1,234.56``, ``-1,234.56 USD``, ``(1,234.56)``
    or ``12.5%`` (which is 12.5), or ``None`` when the text is not one number."""
    written = text.strip()
    negative = len(written) > 2 and written[0] == "(" and written[-1] == ")"
    if negative:
        written = written[1:-1].strip()
    match = _TEXT_NUMBER.fullmatch(written)
    if match is None:
        return None
    signs = [sign for sign in (match["sign"], match["sign_after"]) if sign]
    if len(signs) > 1 or (negative and signs) or (match["suffix"] and (match["symbol"] or match["code"])):
        return None
    number = Decimal(match["whole"].replace(",", "") + (match["fraction"] or ""))
    return -number if negative or (signs and signs[0] in _MINUS) else number


def numbers_in_text(text: str) -> list[Decimal]:
    """Every number written in a text, in order."""
    found = (read_text(match[0]) for match in _NUMBER_IN_TEXT.finditer(text))
    return [number for number in found if number is not None]


def figure(value: Any) -> Decimal | None:
    """A value as a number: a JSON number, or text that is one number; ``None`` otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # repr is the shortest text that reads back as this float, so 0.1 is exactly 0.1.
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str):
        return read_text(value)
    return None


def given_figure(option: str, value: Any) -> Decimal:
    """A figure the check is given, which must be one."""
    found = figure(value)
    if found is None:
        raise ValueError(f'{option} must be a number, or text that is one number, such as 1234.56 or "$1,234.56"')
    return found


@contextmanager
def _exact() -> Iterator[None]:
    with localcontext() as context:
        context.prec = _DIGITS
        context.traps[Inexact] = True
        try:
            yield
        except Inexact:
            raise ValueError(f"a figure needs more than {_DIGITS} digits, so it cannot be added up exactly") from None


def add_up(numbers: Sequence[Decimal]) -> Decimal:
    with _exact():
        return sum(numbers, Decimal(0))


# --- tolerance --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tolerance:
    """How far a figure may be from the one it is checked against: at most the larger of
    ``absolute`` and ``relative`` times the size of that reference figure."""

    absolute: Decimal = Decimal(0)
    relative: Decimal = Decimal(0)

    def check(self, found: Decimal, reference: Decimal) -> tuple[bool, Decimal, Decimal]:
        """Whether ``found`` is close enough to ``reference``, how far off it is, and how far it may be."""
        with _exact():
            off = abs(found - reference)
            allowed = max(self.absolute, self.relative * abs(reference))
        return off <= allowed, off, allowed


def tolerance(abs_tolerance: Any = 0, rel_tolerance: Any = 0) -> Tolerance:
    absolute = given_figure("abs_tolerance", abs_tolerance)
    relative = given_figure("rel_tolerance", rel_tolerance)
    if absolute < 0:
        raise ValueError("abs_tolerance must be 0 or more")
    if not 0 <= relative < 1:
        raise ValueError("rel_tolerance is a fraction of the expected figure, from 0 to below 1: 0.01 is 1%")
    return Tolerance(absolute, relative)


def closeness(found: Decimal, reference: Decimal, off: Decimal, allowed: Decimal, passed: bool, what: str) -> str:
    """How ``found`` compares with ``reference``, which ``what`` names, such as "the total"."""
    if passed and not off:
        return f"{found}, equal to {what} {reference}"
    if passed:
        return f"{found}, within {allowed} of {what} {reference}"
    over = f", more than the tolerance of {allowed}" if allowed else ""
    return f"{found}, but {what} is {reference}: off by {off}{over}"


# --- paths ------------------------------------------------------------------------------------


def parse_path(written: Any, *, option: str = "path", each: bool = False) -> tuple[str, ...]:
    """A dotted path's steps. ``each`` allows the step ``*``, every item of a list."""
    if not isinstance(written, str) or not written:
        raise ValueError(f"{option} must be a dotted path, such as totals.net")
    steps = tuple(written.split("."))
    if not all(steps):
        raise ValueError(f"{option} {written!r} has an empty step")
    if "*" in steps and not each:
        raise ValueError(f"{option} names one value, so it cannot use *")
    return steps


def parse_paths(written: Any, *, option: str, required: bool = True) -> tuple[tuple[str, ...], ...]:
    if isinstance(written, str) or not isinstance(written, Sequence):
        raise ValueError(f"{option} must be a list of dotted paths, such as [lines.*.amount]")
    if required and not written:
        raise ValueError(f"{option} needs at least one path")
    return tuple(parse_path(each, option=option, each=True) for each in written)


def _join(where: str, step: str) -> str:
    return f"{where}.{step}" if where else step


def at(document: Any, steps: Sequence[str]) -> list[tuple[str, Any]]:
    """Every value the path reaches in a JSON document, each with the path that reached it."""
    found: list[tuple[str, Any]] = [("", document)]
    for step in steps:
        reached: list[tuple[str, Any]] = []
        for where, value in found:
            if step == "*":
                if not isinstance(value, list):
                    raise Unreadable(f"the value at {where or 'the top'} is not a list, so * has no items")
                reached.extend((_join(where, str(index)), item) for index, item in enumerate(value))
            elif isinstance(value, dict) and step in value:
                reached.append((_join(where, step), value[step]))
            elif isinstance(value, list) and ascii_digits(step) and int(step) < len(value):
                reached.append((_join(where, step), value[int(step)]))
            else:
                raise Unreadable(f"the output has no value at {_join(where, step)}")
        found = reached
    return found


def numbers_at(document: Any, steps: Sequence[str]) -> list[Decimal]:
    numbers = []
    for where, value in at(document, steps):
        number = figure(value)
        if number is None:
            raise Unreadable(f"the value at {where} is not a number")
        numbers.append(number)
    return numbers


def number_at(document: Any, steps: Sequence[str]) -> Decimal:
    (number,) = numbers_at(document, steps)  # a path without * reaches one value
    return number


# --- number_equals, sums_to and reconciles ----------------------------------------------------


def _expectation(expectation: Any) -> None:
    if expectation is not None and (not isinstance(expectation, str) or not expectation.strip()):
        raise ValueError("expectation must be a nonempty expectations key")


@dataclass(frozen=True)
class NumberOptions:
    expected: Decimal | None
    steps: tuple[str, ...] | None
    pattern: re.Pattern[str] | None
    tolerance: Tolerance

    def found(self, text: str, parsed: bool, document: Any) -> Decimal:
        """The number the output gives: at the path in its JSON, where the pattern finds it in its
        text, or else the one number its text holds."""
        if self.steps is not None:
            if not parsed:
                raise Unreadable("the output is not JSON, so path cannot be read")
            return number_at(document, self.steps)
        if self.pattern is not None:
            match = self.pattern.search(text)
            if match is None:
                raise Unreadable("the pattern is not found in the output")
            written = match[1] if self.pattern.groups else match[0]
            number = None if written is None else read_text(written)
            if number is None:
                raise Unreadable("the text the pattern found is not a number")
            return number
        numbers = numbers_in_text(text)
        if len(numbers) != 1:
            many = f"{len(numbers)} numbers" if numbers else "no number"
            raise Unreadable(f"the output holds {many}; a pattern or a path picks the one to compare")
        return numbers[0]


def number_options(
    *, value: Any, expectation: Any, path: Any, pattern: Any, abs_tolerance: Any, rel_tolerance: Any
) -> NumberOptions:
    _expectation(expectation)
    if value is None and expectation is None:
        raise ValueError("a number_equals check needs value, the expected number, or an expectation key")
    if value is not None and expectation is not None:
        raise ValueError("give value or expectation, not both")
    if path is not None and pattern is not None:
        raise ValueError("give path, to read the number from the JSON output, or pattern, to find it in its text")
    compiled = None
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a nonempty regular expression")
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise ValueError(f"invalid regular expression: {error}") from None
    return NumberOptions(
        expected=None if value is None else given_figure("value", value),
        steps=None if path is None else parse_path(path),
        pattern=compiled,
        tolerance=tolerance(abs_tolerance, rel_tolerance),
    )


@dataclass(frozen=True)
class BalanceOptions:
    add: tuple[tuple[str, ...], ...]
    subtract: tuple[tuple[str, ...], ...]
    total: tuple[str, ...]
    tolerance: Tolerance

    def decide(self, document: Any) -> tuple[bool, str]:
        added = [number for steps in self.add for number in numbers_at(document, steps)]
        taken = [number for steps in self.subtract for number in numbers_at(document, steps)]
        stated = number_at(document, self.total)
        plus, minus = add_up(added), add_up(taken)
        with _exact():
            net = plus - minus
        passed, off, allowed = self.tolerance.check(net, stated)
        if self.subtract:
            made = f"{plus} added ({_count(added)}) less {minus} subtracted ({_count(taken)}) is "
        else:
            made = f"{_count(added, 'part')} add up to "
        return passed, made + closeness(net, stated, off, allowed, passed, "the total")


def _count(numbers: Sequence[Decimal], noun: str = "value") -> str:
    return f"{len(numbers)} {noun}{'' if len(numbers) == 1 else 's'}"


def balance_options(
    *,
    check: str,
    add_option: str,
    add: Any,
    subtract: Any,
    total: Any,
    abs_tolerance: Any,
    rel_tolerance: Any,
) -> BalanceOptions:
    if total is None:
        raise ValueError(f"a {check} check needs total, the path to the figure the others must reach")
    return BalanceOptions(
        add=parse_paths(add, option=add_option),
        subtract=parse_paths(subtract, option="subtract", required=False),
        total=parse_path(total, option="total"),
        tolerance=tolerance(abs_tolerance, rel_tolerance),
    )


# --- tables -----------------------------------------------------------------------------------

_DELIMITER_CELL = re.compile(r":?-+:?")
# Markdown marks around a whole cell, such as a bold total.
_MARKS = ("**", "*", "`")
_SHOWN = 40
_LISTED = 10
# A row: each column, by its folded name, as (the name as written, the cell).
_Row = dict[str, tuple[str, Any]]


def _cell(text: str) -> str:
    cell = text.strip().replace("\\|", "|")
    stripped = True
    while stripped:
        stripped = False
        for mark in _MARKS:
            if len(cell) > 2 * len(mark) and cell.startswith(mark) and cell.endswith(mark):
                cell, stripped = cell[len(mark) : -len(mark)].strip(), True
                break
    return cell


def _split(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [_cell(part) for part in re.split(r"(?<!\\)\|", text)]


def markdown_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Every pipe table in a Markdown text: a header row, a delimiter row such as ``| --- | ---: |``,
    then body rows up to the first line without a ``|``. Each is its header cells and its rows."""
    lines = text.splitlines()
    tables = []
    index = 0
    while index + 1 < len(lines):
        header, delimiter = lines[index], lines[index + 1]
        if "|" in header and "|" in delimiter:
            names, marks = _split(header), _split(delimiter)
            if len(names) == len(marks) and all(_DELIMITER_CELL.fullmatch(mark) for mark in marks):
                index += 2
                body = []
                while index < len(lines) and "|" in lines[index]:
                    body.append(_split(lines[index]))
                    index += 1
                tables.append((names, body))
                continue
        index += 1
    return tables


def rows_from_markdown(text: str) -> list[dict[str, Any]]:
    tables = markdown_tables(text)
    if len(tables) != 1:
        many = f"{len(tables)} Markdown tables" if tables else "no Markdown table"
        raise Unreadable(f"the output is not JSON and holds {many}; the check reads one")
    names, body = tables[0]
    rows = []
    for number, cells in enumerate(body, 1):
        if len(cells) > len(names):
            raise Unreadable(f"row {number} of the table has {len(cells)} cells and its header {len(names)}")
        rows.append(dict(zip(names, cells + [""] * (len(names) - len(cells)), strict=True)))
    return rows


def rows_from_json(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise Unreadable(f"{where} is not a list of row objects")
    return value


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: Any) -> str:
    """A cell as text: a string without surrounding whitespace, a JSON value as its JSON text."""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    if (number := figure(value)) is not None:
        return str(number)
    return json.dumps(value, sort_keys=True, default=str)


def _show(value: Any) -> str:
    if _blank(value):
        return "blank"
    if not isinstance(value, str) and (number := figure(value)) is not None:
        return str(number)
    text = _text(value)
    return json.dumps(text if len(text) <= _SHOWN else text[: _SHOWN - 1] + "…", ensure_ascii=False)


def _listed(items: list[str]) -> str:
    more = f"; and {len(items) - _LISTED} more" if len(items) > _LISTED else ""
    return "; ".join(items[:_LISTED]) + more


def _names(option: str, written: Any, *, required: bool) -> tuple[str, ...]:
    if isinstance(written, str) or not isinstance(written, Sequence):
        raise ValueError(f"{option} must be a column name or a list of them")
    if required and not written:
        raise ValueError(f"{option} needs at least one column name")
    if not all(isinstance(name, str) and name.strip() for name in written):
        raise ValueError(f"{option} must be nonempty column names")
    return tuple(name.strip() for name in written)


@dataclass(frozen=True)
class TableOptions:
    key: tuple[str, ...]
    columns: tuple[str, ...]
    steps: tuple[str, ...] | None
    tolerance: Tolerance
    case_sensitive: bool
    rows: list[_Row] | None = None

    def _fold(self, text: str) -> str:
        return text if self.case_sensitive else text.casefold()

    def _named(self, row: Mapping[Any, Any]) -> tuple[_Row, str | None]:
        """A row's cells by folded column name, and a name two of its columns share, if any."""
        named: _Row = {}
        for name, value in row.items():
            if not isinstance(name, str) or not name.strip():
                continue
            label = name.strip()
            if (folded := self._fold(label)) in named:
                return named, label
            named[folded] = (label, value)
        return named, None

    def _identity(self, row: _Row) -> tuple[str, tuple[str, ...]]:
        cells = [_text(row[self._fold(column)][1]) for column in self.key]
        return ", ".join(cells), tuple(self._fold(cell) for cell in cells)

    def expected(self, rows: Any, where: str) -> list[_Row]:
        """The expected rows: a list of objects, or JSON text of one, as a CSV data file gives it."""
        if isinstance(rows, str):
            try:
                rows = json.loads(rows, parse_float=Decimal)
            except ValueError:
                raise ValueError(f"{where} must be a list of row objects, or JSON text of one") from None
        if not isinstance(rows, list | tuple) or not rows or not all(isinstance(row, Mapping) for row in rows):
            raise ValueError(f"{where} must be a nonempty list of row objects")
        found: list[_Row] = []
        seen: set[tuple[str, ...]] = set()
        for number, row in enumerate(rows, 1):
            named, twice = self._named(row)
            problem = None
            if any(not isinstance(name, str) or not name.strip() for name in row):
                problem = "has a column with no name"
            elif twice is not None:
                problem = f"has two columns named {twice}"
            elif listed := [label for label, cell in named.values() if isinstance(cell, Mapping | list | tuple)]:
                problem = f"has a list or an object in {listed[0]}, not one value"
            elif absent := [name for name in self.key if _blank(named.get(self._fold(name), ("", None))[1])]:
                problem = f"has no {absent[0]}, a key column"
            elif absent := [name for name in self.columns if self._fold(name) not in named]:
                problem = f"has no {absent[0]}, which columns lists"
            if problem:
                raise ValueError(f"{where}: row {number} {problem}")
            label, identity = self._identity(named)
            if identity in seen:
                raise ValueError(f"{where}: two rows have the key {label}")
            seen.add(identity)
            found.append(named)
        return found

    def actual(self, text: str, parsed: bool, document: Any) -> list[dict[str, Any]]:
        """The output's rows: JSON objects, at the path or the whole JSON output, or else the one
        Markdown table in its text."""
        if self.steps is not None:
            if not parsed:
                raise Unreadable("the output is not JSON, so path cannot be read")
            ((where, value),) = at(document, self.steps)
            return rows_from_json(value, f"the value at {where}")
        if parsed:
            return rows_from_json(document, "the JSON output")
        return rows_from_markdown(text)

    def _cell_problem(self, want: Any, got: Any) -> str | None:
        if _blank(want):
            return None if _blank(got) else f"is {_show(got)}, expected blank"
        number = figure(want)
        if number is not None:
            found = figure(got)
            if found is None:
                return f"is {_show(got)}, not a number; expected {number}"
            passed, off, _ = self.tolerance.check(found, number)
            return None if passed else f"is {found}, expected {number}, off by {off}"
        if self._fold(_text(want)) == self._fold(_text(got)):
            return None
        return f"is {_show(got)}, expected {_show(want)}"

    def compare(self, expected: list[_Row], rows: list[dict[str, Any]]) -> tuple[bool, str]:
        actual: dict[tuple[str, ...], tuple[str, _Row]] = {}
        for number, row in enumerate(rows, 1):
            named, twice = self._named(row)
            if twice is not None:
                raise Unreadable(f"row {number} of the output has two columns named {twice}")
            if absent := [name for name in self.key if self._fold(name) not in named]:
                raise Unreadable(f"row {number} of the output has no {absent[0]}, a key column")
            label, identity = self._identity(named)
            if identity in actual:
                raise Unreadable(f"the output has two rows with the key {label}")
            actual[identity] = (label, named)
        keys = {self._fold(name) for name in self.key}
        differ: list[str] = []
        cells = 0
        for row in expected:
            label, identity = self._identity(row)
            if (match := actual.pop(identity, None)) is None:
                differ.append(f"{label} is missing")
                continue
            got = match[1]
            columns = [self._fold(name) for name in self.columns] or [name for name in row if name not in keys]
            problems = []
            for column in columns:
                written, want = row[column]
                cells += 1
                if column not in got:
                    if not _blank(want):  # a row that leaves a blank cell out still has it blank
                        problems.append(f"no {written}")
                elif (problem := self._cell_problem(want, got[column][1])) is not None:
                    problems.append(f"{written} {problem}")
            if problems:
                differ.append(f"{label}: {', '.join(problems)}")
        extra = [label for label, _ in actual.values()]
        if not differ and not extra:
            return True, f"{_count_rows(len(expected))} match, {cells} cells compared"
        found = [f"{len(differ)} of {_count_rows(len(expected))} differ: {_listed(differ)}"] if differ else []
        if extra:
            found.append(f"{_count_rows(len(extra), 'unexpected')}: {_listed(extra)}")
        return False, ". ".join(found)


def _count_rows(count: int, kind: str = "") -> str:
    return f"{count} {kind + ' ' if kind else ''}row{'' if count == 1 else 's'}"


def table_options(
    *,
    rows: Any,
    expectation: Any,
    key: Any,
    columns: Any,
    path: Any,
    abs_tolerance: Any,
    rel_tolerance: Any,
    case_sensitive: bool,
) -> TableOptions:
    _expectation(expectation)
    if key is None:
        raise ValueError("a table_equals check needs key, the column or columns that identify a row")
    if rows is None and expectation is None:
        raise ValueError("a table_equals check needs rows, the expected table, or an expectation key")
    if rows is not None and expectation is not None:
        raise ValueError("give rows or expectation, not both")
    options = TableOptions(
        key=_names("key", [key] if isinstance(key, str) else key, required=True),
        columns=_names("columns", columns, required=False),
        steps=None if path is None else parse_path(path),
        tolerance=tolerance(abs_tolerance, rel_tolerance),
        case_sensitive=case_sensitive,
    )
    folded = [options._fold(name) for name in options.key + options.columns]
    if len(set(folded)) != len(folded):
        raise ValueError("key and columns name each column once; a key column is matched, not compared")
    return options if rows is None else replace(options, rows=options.expected(rows, "rows"))
