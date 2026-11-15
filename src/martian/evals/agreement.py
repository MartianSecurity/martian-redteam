"""How often a judge agrees with people: a judge version's verdicts against human labels of the
same items, with the standard library and :mod:`martian.evals.stats` only.

A judge's pass rate says nothing about how often the judge is wrong. This module compares each
judge version's verdicts with the labels people gave the same items, and reports how far they
agree:

- **Items.** A verdict or a label belongs to an item: the case result it names (a run and a case),
  else the case result whose trace it is on, else its trace. A review always names its case
  result. On each item a judge version has its newest verdict, and each criterion its newest
  label; a label pairs with the verdicts of the judges named like its criterion.
- **Kinds.** A judge's verdicts are pass/fail (``boolean``), one of a set of categories
  (``categorical``), numbers (``numeric``) or free text (``text``): its definition's kind when it
  has one, else the kind of its verdicts. A label of another kind than the judge's verdicts is
  counted as ``incomparable`` and not compared. Free-text verdicts are never compared.
- **Pass/fail and categories.** ``agreement`` is the share of items where verdict and label are
  the same, with its Wilson interval; ``kappa`` is Cohen's kappa, the agreement beyond what the
  two would reach by chance from how often each gives each answer, exact over the counts. For
  pass/fail the four counts are ``both_pass``, ``false_pass`` (the judge passed what people
  failed), ``false_fail`` (the judge failed what people passed) and ``both_fail``. The
  ``false_pass_rate`` is the share of the items people failed that the judge passed, and the
  ``false_fail_rate`` the share of the items people passed that the judge failed, each with its
  Wilson interval. Categories are counted pair by pair.
- **Numbers.** ``mean_difference`` is the mean of verdict minus label, the judge's bias, with its
  Student t interval; ``mean_absolute_difference`` is how far apart they are on average; and
  ``concordance`` is Lin's concordance correlation coefficient, which is 1 only when every verdict
  equals its label and falls with any spread, shift or change of scale between them.
- **Too few items.** Below :data:`MINIMUM_LABELLED` labelled items the status is ``too_few``: the
  counts are reported and every rate, interval and coefficient is None, because a number from a
  handful of items means nothing. At 30 items the 95% interval of an agreement rate is still up to
  34 points wide (at 50%; 28 at 80%), at 10 up to 53.
- **Independence.** Items count as independent: the core's case results carry no group.
- **Panels.** A judge version graded by a panel of judge models (:mod:`martian.evals.panel`) is
  reported as one judge: its verdicts are the panel's, ``panel`` says so, and ``models`` lists its
  members. A panel score with no majority is an error, so it is no verdict.

Every interval is two-sided at :data:`~martian.evals.stats.CONFIDENCE` (95%). Kappa and the
concordance have no interval.

:func:`compare` compares any two sets of values item by item, the first the one being checked and
the second the reference. :func:`judge_agreement` reports every judge version from scores and
reviews in the shared model, as the evals core's ``GET /v1/evals/judge-agreement`` does, and
:func:`results_agreement` reports a suite-file run's checks against labels keyed by case.

**Between reviewers.** People disagree with each other too, and a judge cannot be expected to
agree with people more often than they agree among themselves. :func:`compare_reviewers` measures
how far people's answers to one question agree on the items two or more of them answered, and
:func:`queue_agreement` and :func:`question_agreement` report it from reviews and review queues,
per queue and question and per question across queues, as the evals core's
``GET /v1/evals/reviewer-agreement`` does:

- **Items and answers.** An item is a case result (a run and a case) and the question a review
  answers (its criterion). On each item each reviewer counts once, with their newest review of it:
  a complete one is their answer, and a pending one withdraws it. A review that names no reviewer,
  or whose queue is not reported, counts for nobody. An item is ``shared`` when two or more
  reviewers answered it, and only shared items are compared.
- **Blind answers.** In a queue with blind double review (``blind``, the queues' ids;
  :mod:`martian.evals.server.blind`) each reviewer counts with the answer they gave blind
  (``Review.blind``), before they could read anyone else's, not with an answer they gave after.
  A queue's report says so with ``blind``. Across queues, a question that a blind queue asks is
  compared on the blind queues' blind answers only, and says so; one no blind queue asks, on
  every queue's answers. Answers given in sight of each other agree more for the wrong reason, so
  they are not mixed into a blind report.
- **Two reviewers or more.** With two reviewers across the shared items, their answers are paired
  item by item: ``method`` is ``cohen`` (Cohen's kappa, :func:`cohen_kappa`) for pass/fail and
  categories, and ``lin`` (Lin's concordance, :func:`concordance`) for numbers. With more,
  each item may have its own reviewers and number of them: ``method`` is ``fleiss``
  (:func:`fleiss_kappa`) or ``icc`` (the intraclass correlation ICC(1),
  :func:`intraclass_correlation`).
- **Measures.** For pass/fail and categories ``agreement`` is the share of shared items on which
  every reviewer gave the same answer, with its Wilson interval, and ``kappa`` that agreement
  corrected for chance. For numbers ``mean_absolute_difference`` is how far apart two reviewers'
  answers to an item are on average, with its Student t interval over the items, and
  ``concordance`` is the coefficient ``method`` names. Free-text answers are not compared.
- **Too few items** is :data:`MINIMUM_LABELLED` shared items, as for a judge.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from itertools import combinations
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

from martian.evals._digits import ascii_digits
from martian.evals.model import Review, ReviewQueue, Score, Value, from_results_record
from martian.evals.panel import is_panel, member_sources
from martian.evals.stats import Interval, mean_interval, wilson

MINIMUM_LABELLED = 30
"""The fewest labelled items a judge version's agreement is reported on, and the fewest shared
items people's agreement with each other is. Below it the report gives the counts and says there
are too few."""

Kind = Literal["boolean", "categorical", "numeric", "text"]
Status = Literal["measured", "too_few", "not_comparable"]
Method = Literal["cohen", "fleiss", "lin", "icc"]
KINDS: tuple[Kind, ...] = ("boolean", "categorical", "numeric", "text")

Item = tuple[str, ...]
"""An item: ``("case", run_id, case_id)`` or ``("trace", trace_id)``."""

_OLDEST = datetime.min.replace(tzinfo=UTC)


def value_kind(value: Value) -> Kind | None:
    """The kind of one verdict or label: None for no value. Text is ``categorical`` here; only a
    definition makes it ``text``."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "numeric"
    if isinstance(value, str):
        return "categorical"
    return None


def comparable(kind: Kind, value: Value) -> bool:
    """Whether a value is one a judge of ``kind`` gives."""
    found = value_kind(value)
    return found == kind or (kind == "text" and found == "categorical")


@dataclass(frozen=True)
class Agreement:
    """The values being checked against the reference values of the same items.

    ``labelled`` is the number of items compared and ``status`` says what was measured:
    ``measured``, ``too_few`` (fewer than ``minimum`` items: only the counts) or
    ``not_comparable`` (free text). The counts (``agreed``, the four pass/fail counts and
    ``categories``, each a checked value, a reference value and how many items had both) are
    given whatever the status; every rate, interval and coefficient only when measured.
    ``kappa`` is None when every value on both sides is the same, which leaves no chance
    agreement to correct for."""

    kind: Kind
    labelled: int
    minimum: int
    status: Status
    agreed: int | None = None
    agreement: float | None = None
    agreement_confidence_95: Interval | None = None
    kappa: float | None = None
    both_pass: int | None = None
    false_pass: int | None = None
    false_fail: int | None = None
    both_fail: int | None = None
    false_pass_rate: float | None = None
    false_pass_confidence_95: Interval | None = None
    false_fail_rate: float | None = None
    false_fail_confidence_95: Interval | None = None
    categories: tuple[tuple[str, str, int], ...] | None = None
    mean_difference: float | None = None
    mean_difference_confidence_95: Interval | None = None
    mean_absolute_difference: float | None = None
    concordance: float | None = None


def cohen_kappa(pairs: Sequence[tuple[Value, Value]]) -> float | None:
    """Cohen's kappa of paired categorical values: (p_o − p_e) / (1 − p_e), with p_o the share of
    pairs that match and p_e the share expected by chance from each side's own frequencies. Exact
    over the counts: (n·agreed − S) / (n² − S), S = Σ_k n_checked(k) · n_reference(k). None for no
    pairs, or when every value on both sides is the same."""
    n = len(pairs)
    if n == 0:
        return None
    agreed = sum(_same(a, b) for a, b in pairs)
    checked, reference = Counter(_key(a) for a, _ in pairs), Counter(_key(b) for _, b in pairs)
    chance = sum(count * reference[key] for key, count in checked.items())
    if n * n == chance:
        return None
    return (n * agreed - chance) / (n * n - chance)


def concordance(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Lin's concordance correlation coefficient of paired numbers: 2·s_xy / (s_x² + s_y² + (x̄ − ȳ)²),
    with the moments over n. 1 when every pair is equal, including when neither side varies. None
    for no pairs."""
    n = len(pairs)
    if n == 0:
        return None
    xs, ys = [float(a) for a, _ in pairs], [float(b) for _, b in pairs]
    mx, my = fmean(xs), fmean(ys)
    sxx = math.fsum((x - mx) ** 2 for x in xs) / n
    syy = math.fsum((y - my) ** 2 for y in ys) / n
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / n
    spread = sxx + syy + (mx - my) ** 2
    if spread == 0:
        return 1.0
    return 2 * sxy / spread


def fleiss_kappa(items: Sequence[Sequence[Value]]) -> float | None:
    """Fleiss' kappa of categorical answers, each item answered by two or more people, not
    necessarily the same people or as many: (P̄ − P_e) / (1 − P_e), with P̄ the mean over the items
    of the share of an item's pairs of answers that match, Σ_k n_ik(n_ik − 1) / (n_i(n_i − 1)), and
    P_e = Σ_k p_k², p_k the share of every answer that is k. With the same number of answers on
    every item it is Fleiss' (1971). Exact over the counts. Items with fewer than two answers are
    left out. None for no such item, or when every answer is the same."""
    rated = [Counter(_key(value) for value in item) for item in items if len(item) >= 2]
    if not rated:
        return None
    matching = Fraction(0)
    totals: Counter[tuple[str, Value]] = Counter()
    for counts in rated:
        n = sum(counts.values())
        matching += Fraction(sum(c * (c - 1) for c in counts.values()), n * (n - 1))
        totals.update(counts)
    observed = matching / len(rated)
    answers = sum(totals.values())
    chance = sum(Fraction(count, answers) ** 2 for count in totals.values())
    if chance == 1:
        return None
    return float((observed - chance) / (1 - chance))


def intraclass_correlation(items: Sequence[Sequence[float]]) -> float | None:
    """The intraclass correlation ICC(1) of numeric answers, each item answered by two or more
    people, not necessarily the same people or as many: the one-way random-effects model,
    (MS_between − MS_within) / (MS_between + (n₀ − 1)·MS_within), with n₀ = (N − Σ n_i² / N) / (a − 1)
    for a items of n_i answers, N in all. 1 when every item's answers are equal, including when no
    answer differs from another. Items with fewer than two answers are left out. None for fewer
    than two such items."""
    rated = [[float(value) for value in item] for item in items if len(item) >= 2]
    a = len(rated)
    if a < 2:
        return None
    n = sum(len(item) for item in rated)
    grand = math.fsum(value for item in rated for value in item) / n
    means = [fmean(item) for item in rated]
    between = math.fsum(len(item) * (mean - grand) ** 2 for item, mean in zip(rated, means, strict=True)) / (a - 1)
    within = math.fsum((value - mean) ** 2 for item, mean in zip(rated, means, strict=True) for value in item) / (n - a)
    n0 = (n - math.fsum(len(item) ** 2 for item in rated) / n) / (a - 1)
    spread = between + (n0 - 1) * within
    if spread == 0:
        return 1.0
    return (between - within) / spread


def _key(value: Value) -> tuple[str, Value]:
    # True and 1 are different answers, so each value is told apart by its kind as well.
    return (type(value).__name__, value)


def _same(a: Value, b: Value) -> bool:
    return _key(a) == _key(b)


def _rate(count: int, total: int) -> tuple[float | None, Interval | None]:
    return (count / total, wilson(count, total)) if total else (None, None)


def compare(
    pairs: Sequence[tuple[Value, Value]], kind: Kind | None = None, *, minimum: int = MINIMUM_LABELLED
) -> Agreement:
    """Compare the first value of each pair, the one being checked, with the second, the reference.
    ``kind`` is the values' kind; without one it is the kind of the first pair's checked value
    (``boolean`` for no pairs). Every value must be of that kind (text for ``text`` and
    ``categorical``): filter pairs with :func:`comparable` first."""
    if minimum < 1:
        raise ValueError("minimum must be at least 1")
    found: Kind = kind or (value_kind(pairs[0][0]) if pairs else None) or "boolean"
    if found not in KINDS:
        raise ValueError(f"unknown kind {found!r}")
    if any(not comparable(found, a) or not comparable(found, b) for a, b in pairs):
        raise ValueError(f"every value must be {found}")
    n = len(pairs)
    if found == "text":
        return Agreement(kind=found, labelled=n, minimum=minimum, status="not_comparable")
    measured = n >= minimum
    status: Status = "measured" if measured else "too_few"
    if found == "numeric":
        numbers = [(float(a), float(b)) for a, b in pairs]  # type: ignore[arg-type]
        differences = [a - b for a, b in numbers]
        if not measured:
            return Agreement(kind=found, labelled=n, minimum=minimum, status=status)
        return Agreement(
            kind=found,
            labelled=n,
            minimum=minimum,
            status=status,
            mean_difference=fmean(differences),
            mean_difference_confidence_95=mean_interval(differences),
            mean_absolute_difference=fmean(abs(d) for d in differences),
            concordance=concordance(numbers),
        )
    agreed = sum(_same(a, b) for a, b in pairs)
    rates: dict[str, Any] = {}
    if measured:
        rates = {
            "agreement": agreed / n,
            "agreement_confidence_95": wilson(agreed, n),
            "kappa": cohen_kappa(pairs),
        }
    if found == "categorical":
        counted = Counter((str(a), str(b)) for a, b in pairs)
        categories = tuple((a, b, count) for (a, b), count in sorted(counted.items()))
        return Agreement(
            kind=found, labelled=n, minimum=minimum, status=status, agreed=agreed, categories=categories, **rates
        )
    both_pass = sum(a is True and b is True for a, b in pairs)
    false_pass = sum(a is True and b is False for a, b in pairs)
    false_fail = sum(a is False and b is True for a, b in pairs)
    both_fail = sum(a is False and b is False for a, b in pairs)
    if measured:
        rates["false_pass_rate"], rates["false_pass_confidence_95"] = _rate(false_pass, false_pass + both_fail)
        rates["false_fail_rate"], rates["false_fail_confidence_95"] = _rate(false_fail, false_fail + both_pass)
    return Agreement(
        kind=found,
        labelled=n,
        minimum=minimum,
        status=status,
        agreed=agreed,
        both_pass=both_pass,
        false_pass=false_pass,
        false_fail=false_fail,
        both_fail=both_fail,
        **rates,
    )


# --- judge versions ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    """One compared item of a judge version's report: the item, the judge's verdict on it and the
    label people gave it."""

    item: Item
    verdict: Value
    label: Value


@dataclass(frozen=True)
class JudgeAgreement:
    """One judge version's verdicts against people's labels. ``judged`` counts the items it gave a
    verdict, ``incomparable`` those whose label is of another kind than its verdicts, and
    ``agreement.labelled`` those compared, which ``pairs`` holds in item order. ``models`` are the
    models its verdicts name: for a panel's verdicts (``panel``), its members' models. A panel is
    reported as one judge, since its verdict is the panel's (``martian.evals.panel``)."""

    judge: str
    version: str
    models: tuple[str, ...]
    judged: int
    incomparable: int
    agreement: Agreement
    panel: bool = False
    pairs: tuple[Pair, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """The report as the evals core serves it, with the reference side named ``human``."""
        found = self.agreement
        pass_fail = found.kind == "boolean"
        return {
            "judge": self.judge,
            "version": self.version,
            "kind": found.kind,
            "models": list(self.models),
            "panel": self.panel,
            "judged": self.judged,
            "labelled": found.labelled,
            "incomparable": self.incomparable,
            "minimum": found.minimum,
            "status": found.status,
            "agreed": found.agreed,
            "agreement": found.agreement,
            "agreement_confidence_95": _interval(found.agreement_confidence_95),
            "kappa": found.kappa,
            "confusion": {
                "both_pass": found.both_pass,
                "false_pass": found.false_pass,
                "false_fail": found.false_fail,
                "both_fail": found.both_fail,
            }
            if pass_fail
            else None,
            "false_pass_rate": found.false_pass_rate,
            "false_pass_confidence_95": _interval(found.false_pass_confidence_95),
            "false_fail_rate": found.false_fail_rate,
            "false_fail_confidence_95": _interval(found.false_fail_confidence_95),
            "categories": [{"judge": a, "human": b, "count": count} for a, b, count in found.categories]
            if found.categories is not None
            else None,
            "mean_difference": found.mean_difference,
            "mean_difference_confidence_95": _interval(found.mean_difference_confidence_95),
            "mean_absolute_difference": found.mean_absolute_difference,
            "concordance": found.concordance,
        }


def _interval(found: Interval | None) -> list[float] | None:
    return list(found) if found is not None else None


def item_of(entry: Score | Review, case_traces: Mapping[str, tuple[str, str]] | None = None) -> Item | None:
    """The item a verdict or label belongs to: the case result it names, else the case result whose
    trace it is on (``case_traces`` maps a case result's trace id to its run and case ids), else
    its trace. None when it names none of them."""
    if entry.run_id and entry.case_id:
        return ("case", entry.run_id, entry.case_id)
    if isinstance(entry, Score) and entry.trace_id:
        if case_traces and (found := case_traces.get(entry.trace_id)):
            return ("case", *found)
        return ("trace", entry.trace_id)
    return None


def _newest[T: Score | Review](entries: Iterable[tuple[tuple[Any, ...], T]]) -> dict[tuple[Any, ...], T]:
    """The newest entry for each key; of two at the same time, the later one given."""
    found: dict[tuple[Any, ...], T] = {}
    for key, entry in entries:
        current = found.get(key)
        if current is None or (entry.created_at or _OLDEST) >= (current.created_at or _OLDEST):
            found[key] = entry
    return found


def _version_order(key: tuple[str, str]) -> tuple[str, bool, int, str]:
    name, version = key
    numbered = ascii_digits(version)
    return (name, not numbered, int(version) if numbered else 0, version)


def judge_agreement(
    verdicts: Iterable[Score],
    labels: Iterable[Score | Review],
    *,
    case_traces: Mapping[str, tuple[str, str]] | None = None,
    kinds: Mapping[tuple[str, str], Kind] | None = None,
    versions: Iterable[tuple[str, str]] = (),
    minimum: int = MINIMUM_LABELLED,
) -> list[JudgeAgreement]:
    """Each judge version's verdicts against people's labels of the same items, by judge name and
    then version.

    ``verdicts`` are judge scores: a judge version is a scorer name and version. A score with an
    error or no value is no verdict. ``labels`` are people's answers: complete reviews (their
    criterion is what they label) and human scores (their scorer name is). ``kinds`` gives a
    judge version's kind from its definition; ``versions`` names judge versions to report even
    with no verdict, such as registered ones never applied."""
    found = [v for v in verdicts if v.value is not None and not v.error]
    by_judge = _newest(
        ((item, v.scorer, v.scorer_version), v) for v in found if (item := item_of(v, case_traces)) is not None
    )
    answered = [
        label
        for label in labels
        if label.value is not None
        and (isinstance(label, Review) and label.state == "complete" or isinstance(label, Score) and not label.error)
    ]
    by_criterion = _newest(
        ((item, label.criterion if isinstance(label, Review) else label.scorer), label)
        for label in answered
        if (item := item_of(label, case_traces)) is not None
    )
    grouped: dict[tuple[str, str], list[tuple[Item, Score]]] = {key: [] for key in versions}
    for (item, name, version), verdict in by_judge.items():
        grouped.setdefault((name, version), []).append((item, verdict))
    reports = []
    for key in sorted(grouped, key=_version_order):
        name, version = key
        judged = grouped[key]
        kind = (kinds or {}).get(key) or _inferred([v.value for _, v in judged])
        pairs: list[Pair] = []
        incomparable = 0
        for item, verdict in sorted(judged, key=lambda each: each[0]):
            label = by_criterion.get((item, name))
            if label is None:
                continue
            if comparable(kind, verdict.value) and comparable(kind, label.value):
                pairs.append(Pair(item, verdict.value, label.value))
            else:
                incomparable += 1
        reports.append(
            JudgeAgreement(
                judge=name,
                version=version,
                models=tuple(sorted({model for _, v in judged for model in _models(v) if model})),
                judged=len(judged),
                incomparable=incomparable,
                agreement=compare([(p.verdict, p.label) for p in pairs], kind, minimum=minimum),
                panel=any(is_panel(v.metadata) for _, v in judged),
                pairs=tuple(pairs),
            )
        )
    return reports


def _models(verdict: Score) -> list[str]:
    """The models a verdict names: a panel's members, or the one model in its ``source_id``."""
    return member_sources(verdict.metadata) if is_panel(verdict.metadata) else [verdict.source_id]


def _inferred(values: Sequence[Value]) -> Kind:
    """A judge's kind from its verdicts: categorical when any is text, numeric when any is a number
    and none text, boolean otherwise (including for no verdicts)."""
    kinds = {value_kind(value) for value in values}
    if "categorical" in kinds:
        return "categorical"
    if "numeric" in kinds:
        return "numeric"
    return "boolean"


# --- a suite-file run's results ----------------------------------------------------------------


def results_agreement(
    results: str | Path | Iterable[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Value]],
    *,
    minimum: int = MINIMUM_LABELLED,
) -> list[JudgeAgreement]:
    """A suite-file run's checks against people's labels of the same cases.

    ``results`` is the run's ``results.jsonl``, the output folder that holds it, or its records.
    ``labels`` maps a case id to a label for each check it labels, by the check's label, such as
    ``{"case-1": {"rubric": False}}``. Every check a label names is compared, model judge or not;
    a check's verdicts are pass/fail. A results file does not carry scorer versions, so each
    report's ``version`` is empty and its ``models`` none."""
    records: Iterable[Mapping[str, Any]]
    if isinstance(results, str | Path):
        path = Path(results)
        path = path / "results.jsonl" if path.is_dir() else path
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        records = results
    verdicts: list[Score] = []
    for position, record in enumerate(records):
        _, scores = from_results_record(dict(record), run_id="results", position=position)
        verdicts.extend(scores)
    human = [
        Score(run_id="results", case_id=str(case_id), scorer=label, source="human", value=value)
        for case_id, labelled in labels.items()
        for label, value in labelled.items()
    ]
    # A check a label names is reported even when the results have no verdict of it, so a label
    # under a mistyped name shows as judged by nothing.
    named = {(score.scorer, score.scorer_version) for score in human}
    reports = judge_agreement(verdicts, human, kinds=dict.fromkeys(named, "boolean"), versions=named, minimum=minimum)
    return [report for report in reports if (report.judge, report.version) in named]


# --- between reviewers -------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewerAgreement:
    """People's answers to one question against each other, on the items two or more of them
    answered.

    ``answered`` counts the items anyone answered, ``shared`` those two or more reviewers answered,
    which are the ones compared, and ``reviewers`` the people who answered the shared items.
    ``incomparable`` counts answers of another kind than the question's, which are left out.
    ``method`` names the coefficient: ``cohen`` or ``lin`` for two reviewers, ``fleiss`` or
    ``icc`` for more; it, every rate, interval and coefficient are given only when measured, and
    ``agreed`` (the shared items on which every reviewer gave the same answer, for pass/fail and
    categories) whatever the status."""

    question: str
    kind: Kind
    answered: int
    shared: int
    reviewers: int
    incomparable: int
    minimum: int
    status: Status
    method: Method | None = None
    agreed: int | None = None
    agreement: float | None = None
    agreement_confidence_95: Interval | None = None
    kappa: float | None = None
    mean_absolute_difference: float | None = None
    mean_absolute_difference_confidence_95: Interval | None = None
    concordance: float | None = None
    blind: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The report as the evals core serves it."""
        return {
            "question": self.question,
            "kind": self.kind,
            "answered": self.answered,
            "shared": self.shared,
            "reviewers": self.reviewers,
            "incomparable": self.incomparable,
            "minimum": self.minimum,
            "status": self.status,
            "method": self.method,
            "agreed": self.agreed,
            "agreement": self.agreement,
            "agreement_confidence_95": _interval(self.agreement_confidence_95),
            "kappa": self.kappa,
            "mean_absolute_difference": self.mean_absolute_difference,
            "mean_absolute_difference_confidence_95": _interval(self.mean_absolute_difference_confidence_95),
            "concordance": self.concordance,
            "blind": self.blind,
        }


@dataclass(frozen=True)
class QueueAgreement:
    """A review queue's reviewers' agreement, one report per question in the queue's order."""

    queue_id: str
    name: str
    run_id: str
    questions: tuple[ReviewerAgreement, ...]

    def as_dict(self) -> dict[str, Any]:
        """The queue's report as the evals core serves it."""
        return {
            "queue_id": self.queue_id,
            "name": self.name,
            "run_id": self.run_id,
            "questions": [question.as_dict() for question in self.questions],
        }


def compare_reviewers(
    items: Iterable[Mapping[str, Value]],
    kind: Kind,
    *,
    question: str = "",
    minimum: int = MINIMUM_LABELLED,
    blind: bool = False,
) -> ReviewerAgreement:
    """People's answers to one question of ``kind`` against each other. Each item maps a reviewer
    to their answer; an answer that is None or of another kind is left out. ``blind`` says the
    answers were given blind."""
    if minimum < 1:
        raise ValueError("minimum must be at least 1")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    answered = incomparable = 0
    shared: list[dict[str, Value]] = []
    for item in items:
        given = {reviewer: value for reviewer, value in item.items() if value is not None}
        kept: dict[str, Value] = {reviewer: value for reviewer, value in given.items() if comparable(kind, value)}
        incomparable += len(given) - len(kept)
        answered += bool(kept)
        if len(kept) >= 2:
            shared.append(kept)
    people = sorted({reviewer for item in shared for reviewer in item})
    counts: dict[str, Any] = {
        "question": question,
        "kind": kind,
        "answered": answered,
        "shared": len(shared),
        "reviewers": len(people),
        "incomparable": incomparable,
        "minimum": minimum,
        "blind": blind,
    }
    if kind == "text":
        return ReviewerAgreement(**counts, status="not_comparable")
    measured = len(shared) >= minimum
    status: Status = "measured" if measured else "too_few"
    two = len(people) == 2
    if kind == "numeric":
        if not measured:
            return ReviewerAgreement(**counts, status=status)
        numbers = [{reviewer: float(value) for reviewer, value in item.items()} for item in shared]  # type: ignore[arg-type]
        apart = [fmean(abs(a - b) for a, b in combinations(item.values(), 2)) for item in numbers]
        coefficient = (
            concordance([(item[people[0]], item[people[1]]) for item in numbers])
            if two
            else intraclass_correlation([list(item.values()) for item in numbers])
        )
        return ReviewerAgreement(
            **counts,
            status=status,
            method="lin" if two else "icc",
            mean_absolute_difference=fmean(apart),
            mean_absolute_difference_confidence_95=mean_interval(apart),
            concordance=coefficient,
        )
    agreed = sum(len({_key(value) for value in item.values()}) == 1 for item in shared)
    if not measured:
        return ReviewerAgreement(**counts, status=status, agreed=agreed)
    return ReviewerAgreement(
        **counts,
        status=status,
        method="cohen" if two else "fleiss",
        agreed=agreed,
        agreement=agreed / len(shared),
        agreement_confidence_95=wilson(agreed, len(shared)),
        kappa=cohen_kappa([(item[people[0]], item[people[1]]) for item in shared])
        if two
        else fleiss_kappa([list(item.values()) for item in shared]),
    )


def _answers(
    reviews: Iterable[Review], queue_ids: set[str], *, by_queue: bool, blind: AbstractSet[str] = frozenset()
) -> dict[tuple[str, ...], dict[str, Value]]:
    """Each item's answers by reviewer: each reviewer's newest review of the item, when it is
    complete, and in a queue of ``blind`` the answer they gave blind. An item is its queue
    (``by_queue``), run, case and question."""
    newest = _newest(
        (((r.queue_id or "") if by_queue else "", r.run_id, r.case_id, r.criterion, r.reviewer), r)
        for r in reviews
        if r.reviewer and r.queue_id in queue_ids and (r.queue_id not in blind or r.blind)
    )
    found: dict[tuple[str, ...], dict[str, Value]] = {}
    for (queue, run, case, question, reviewer), review in newest.items():
        if review.state == "complete" and review.value is not None:
            found.setdefault((queue, run, case, question), {})[reviewer] = review.value
    return found


def queue_agreement(
    reviews: Iterable[Review],
    queues: Iterable[ReviewQueue],
    *,
    minimum: int = MINIMUM_LABELLED,
    blind: AbstractSet[str] = frozenset(),
) -> list[QueueAgreement]:
    """Each review queue's reviewers' agreement on each of its questions, in the order the queues
    are given. ``reviews`` are reviews of the queues' cases, every link of each chain
    (``list_reviews(..., current_only=False)``): an answer a later review replaced still counts for
    the reviewer who gave it. A queue in ``blind`` has blind double review, and its report compares
    the answers given blind."""
    queues = list(queues)
    answers = _answers(reviews, {queue.id for queue in queues}, by_queue=True, blind=blind)
    grouped: dict[tuple[str, str], list[dict[str, Value]]] = {}
    for (queue_id, _, _, question), found in answers.items():
        grouped.setdefault((queue_id, question), []).append(found)
    return [
        QueueAgreement(
            queue_id=queue.id,
            name=queue.name,
            run_id=queue.run_id,
            questions=tuple(
                compare_reviewers(
                    grouped.get((queue.id, q.name), []),
                    q.kind,
                    question=q.name,
                    minimum=minimum,
                    blind=queue.id in blind,
                )
                for q in queue.questions
            ),
        )
        for queue in queues
    ]


def question_agreement(
    reviews: Iterable[Review],
    queues: Iterable[ReviewQueue],
    *,
    minimum: int = MINIMUM_LABELLED,
    blind: AbstractSet[str] = frozenset(),
) -> list[ReviewerAgreement]:
    """Reviewers' agreement on each question the queues ask, across every queue that asks it, by
    question name. A question's kind is its kind in the newest queue that asks it; an answer of
    another kind is counted in ``incomparable``. ``reviews`` are as :func:`queue_agreement` takes.
    A question that a queue of ``blind`` asks is compared on the blind queues' answers given blind
    only, and says so."""
    queues = sorted(queues, key=lambda queue: (queue.created_at or _OLDEST, queue.id), reverse=True)
    kinds: dict[str, Kind] = {}
    blinded: set[str] = set()
    for queue in queues:
        for question in queue.questions:
            kinds.setdefault(question.name, question.kind)
            if queue.id in blind:
                blinded.add(question.name)
    reviews = list(reviews)
    grouped: dict[tuple[bool, str], list[dict[str, Value]]] = {}
    for only_blind, ids in ((False, {q.id for q in queues}), (True, {q.id for q in queues if q.id in blind})):
        for (_, _, _, name), found in _answers(reviews, ids, by_queue=False, blind=blind).items():
            grouped.setdefault((only_blind, name), []).append(found)
    return [
        compare_reviewers(
            grouped.get((name in blinded, name), []),
            kinds[name],
            question=name,
            minimum=minimum,
            blind=name in blinded,
        )
        for name in sorted(kinds)
    ]


__all__ = [
    "KINDS",
    "MINIMUM_LABELLED",
    "Agreement",
    "Item",
    "JudgeAgreement",
    "Kind",
    "Method",
    "Pair",
    "QueueAgreement",
    "ReviewerAgreement",
    "Status",
    "cohen_kappa",
    "comparable",
    "compare",
    "compare_reviewers",
    "concordance",
    "fleiss_kappa",
    "intraclass_correlation",
    "item_of",
    "judge_agreement",
    "queue_agreement",
    "question_agreement",
    "results_agreement",
    "value_kind",
]
