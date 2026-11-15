"""When a schedule's runs are due: its slots, in UTC (MorningHQ/martian-notes#197).

A schedule is due either every so many seconds or on a cron expression, never both:

- **An interval**, ``every_seconds``: the slots are the multiples of it since the Unix epoch, in
  UTC, so ``3600`` is due on the hour and ``86400`` at midnight UTC, whenever the schedule was
  created.
- **A cron expression**, ``cron``: five fields, minute (0-59), hour (0-23), day of the month
  (1-31), month (1-12) and day of the week (0-7, where 0 and 7 are Sunday), always read in UTC.
  Each field is ``*``, a number, a range ``a-b``, either of those with a step (``*/15``,
  ``8-18/2``), or a list of them separated by commas. Names (``MON``, ``JAN``) and shortcuts
  (``@daily``) are not read. When both day fields are restricted, a day is due when either
  matches, as in cron; when one is ``*``, the other decides. Slots fall on whole minutes.

:func:`timing` reads either form. Its :meth:`~Interval.after` is the first slot after a moment, and
:meth:`~Interval.count` the number of slots in a span, which a schedule's turn reports as the
slots it did not run (``missed``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

MIN_INTERVAL_SECONDS = 60
"""The shortest interval a new schedule may have, the same as cron's minute."""
SEARCH_YEARS = 9
"""How far ahead a cron expression's next slot is looked for: every date, 29 February included,
comes round within it. An expression with no slot in that time is refused."""

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of the month", 1, 31), ("month", 1, 12), ("day of the week", 0, 7))


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("a slot is computed from a time with a time zone")
    return moment.astimezone(UTC)


@dataclass(frozen=True)
class Interval:
    """Slots every ``seconds``, counted from the Unix epoch in UTC."""

    seconds: int

    def after(self, moment: datetime) -> datetime:
        """The first slot strictly after ``moment``."""
        number = int(_utc(moment).timestamp() // self.seconds) + 1
        return datetime.fromtimestamp(number * self.seconds, UTC)

    def count(self, start: datetime, end: datetime, *, up_to: int) -> int:
        """How many slots fall in ``(start, end]``, at most ``up_to``."""
        found = int(_utc(end).timestamp() // self.seconds) - int(_utc(start).timestamp() // self.seconds)
        return max(0, min(found, up_to))


@dataclass(frozen=True)
class Cron:
    """Slots on a five-field cron expression, in UTC."""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    def _day(self, day: date) -> bool:
        weekday = (day.weekday() + 1) % 7  # cron counts from Sunday
        if self.any_day or self.any_weekday:
            return day.day in self.days and weekday in self.weekdays
        return day.day in self.days or weekday in self.weekdays

    def after(self, moment: datetime) -> datetime:
        """The first slot strictly after ``moment``. Raises ``ValueError`` when there is none
        within :data:`SEARCH_YEARS`."""
        start = _utc(moment).replace(second=0, microsecond=0) + timedelta(minutes=1)
        at, horizon = start, start + timedelta(days=366 * SEARCH_YEARS)
        while at < horizon:
            if at.month not in self.months:
                year, month = (at.year + 1, 1) if at.month == 12 else (at.year, at.month + 1)
                at = datetime(year, month, 1, tzinfo=UTC)
            elif not self._day(at.date()):
                at = datetime.combine(at.date() + timedelta(days=1), datetime.min.time(), UTC)
            elif at.hour not in self.hours:
                at = at.replace(minute=0) + timedelta(hours=1)
            elif at.minute not in self.minutes:
                at += timedelta(minutes=1)
            else:
                return at
        raise ValueError(f"the cron expression {self.expression!r} is never due")

    def count(self, start: datetime, end: datetime, *, up_to: int) -> int:
        """How many slots fall in ``(start, end]``, at most ``up_to``."""
        found, at = 0, _utc(start)
        while found < up_to:
            at = self.after(at)
            if at > _utc(end):
                break
            found += 1
        return found


def _field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(","):
        part, _, step_text = item.partition("/")
        if step_text and not step_text.isdigit():
            raise ValueError(f"the {name} field's step {step_text!r} is not a number")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ValueError(f"the {name} field's step is at least 1")
        if part == "*":
            first, last = low, high
        elif "-" in part:
            first_text, _, last_text = part.partition("-")
            if not (first_text.isdigit() and last_text.isdigit()):
                raise ValueError(f"the {name} field's range {part!r} is not two numbers")
            first, last = int(first_text), int(last_text)
        elif part.isdigit():
            if step_text:
                raise ValueError(f"the {name} field takes a step after * or a range, not after {part!r}")
            first = last = int(part)
        else:
            raise ValueError(f"the {name} field's {part!r} is not *, a number or a range")
        if not (low <= first <= high and low <= last <= high):
            raise ValueError(f"the {name} field's {part!r} is not within {low}-{high}")
        if first > last:
            raise ValueError(f"the {name} field's range {part!r} runs backwards")
        values.update(range(first, last + 1, step))
    return frozenset(values)


def parse_cron(expression: str) -> Cron:
    """Read a five-field cron expression. Raises ``ValueError`` naming the field it cannot read, or
    saying that the expression is never due."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("a cron expression has five fields: minute, hour, day of the month, month and day of the week")
    minutes, hours, days, months, weekdays = (
        _field(text, name, low, high) for text, (name, low, high) in zip(fields, _FIELDS, strict=True)
    )
    found = Cron(
        expression=" ".join(fields),
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=frozenset(day % 7 for day in weekdays),
        any_day=fields[2] == "*",
        any_weekday=fields[4] == "*",
    )
    found.after(datetime(2026, 1, 1, tzinfo=UTC))  # refuse an expression that is never due
    return found


def timing(*, every_seconds: int | None, cron: str | None) -> Interval | Cron:
    """A schedule's slots: every ``every_seconds``, or on ``cron``; exactly one of them."""
    if (every_seconds is None) == (cron is None):
        raise ValueError("A schedule is due every so many seconds (every_seconds) or on a cron expression (cron)")
    if every_seconds is not None:
        if every_seconds < 1:
            raise ValueError("every_seconds is at least 1")
        return Interval(every_seconds)
    assert cron is not None
    return parse_cron(cron)


__all__ = ["MIN_INTERVAL_SECONDS", "Cron", "Interval", "parse_cron", "timing"]
