"""Confidence intervals for pass rates and mean scores, with the standard library only.

Eval samples are small and often grouped, so the methods are chosen for that:

- **Pass rates** use the Wilson score interval. It stays inside 0 to 1, is never zero width, and
  keeps close to its stated coverage down to a handful of cases, including 0 of n and n of n,
  where the normal approximation collapses to a single point.
- **Mean scores** use the Student t interval, mean ± t × s / √n on n − 1 degrees of freedom. It
  needs two values; a sample with no spread has a zero-width interval.
- **Grouped cases.** Cases that share a group, such as paraphrases of one attack or turns of one
  conversation, are not independent. With two or more groups, both intervals are widened by the
  design effect of the cluster-robust (CR1) variance of the mean:

      deff = k (n − 1) / ((k − 1) n) × Σ_g (Σ_{i in g} e_i)² / Σ_i e_i²,   e_i = x_i − mean

  over n cases in k groups, and never below 1, so grouping never narrows an interval. A pass rate
  takes the Wilson interval at the effective sample size n / deff; a mean takes the t interval
  with its variance multiplied by deff, on k − 1 degrees of freedom. When every group has one case
  deff is exactly 1 and both are the ungrouped intervals. With fewer than two groups, or no spread
  in the values, there is nothing to estimate the grouping from, and the cases count as
  independent.

Every interval is two-sided at ``CONFIDENCE`` (95%) unless another level is given. A group label of
None is a case's own group.

Two runs of the same cases compare case by case, with a paired test and a label:

- **Pass/fail pairs** use the exact McNemar test: only the pairs that changed carry evidence, and
  under no change each is as likely to have improved as regressed, so the p-value is the two-sided
  binomial tail at one half.
- **Paired scores** use the paired t test on each case's difference, the same Student t as the
  mean interval, so a difference is significant exactly when its 95% interval leaves out 0.
- **The label** is better or worse when p < ``SIGNIFICANCE`` (5%, two-sided) and at least
  ``MINIMUM_CHANGED`` pairs changed, and within noise otherwise. Six all one way is the fewest
  changed pairs an exact test can tell from chance at 5%, so a handful of cases is always within
  noise, whatever moved, rather than better on one run and worse on the next.
- **Grouped cases** are not allowed for: every pair counts as independent. The comparison pairs
  case results, which carry no group, and the exact McNemar test has no grouped form.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from statistics import NormalDist, fmean
from typing import Literal

CONFIDENCE = 0.95
"""The confidence level of every interval the SDK, the core and the Evals UI report."""

SIGNIFICANCE = 0.05
"""The two-sided significance level of a paired comparison's label: the complement of ``CONFIDENCE``."""

MINIMUM_CHANGED = 6
"""The fewest changed pairs a comparison labels better or worse. Six all one way give an exact
two-sided p of 2 × 0.5⁶ = 0.031 and five 0.0625, so the exact test never labels fewer than six; the
paired t test is held to the same count."""

Interval = tuple[float, float]
Label = Literal["better", "worse", "within_noise"]


def _z(confidence: float) -> float:
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")
    return NormalDist().inv_cdf(0.5 + confidence / 2)


def wilson(passed: float, count: float, confidence: float = CONFIDENCE) -> Interval | None:
    """The Wilson score interval for ``passed`` of ``count``. None when ``count`` is 0.

    Both may be fractional, as an effective sample size is (:func:`proportion_interval`)."""
    if count < 0 or not 0 <= passed <= count:
        raise ValueError("passed must be between 0 and count")
    if count == 0:
        return None
    z = _z(confidence)
    p = passed / count
    spread = z * z / count
    centre = (p + spread / 2) / (1 + spread)
    half = z * math.sqrt(p * (1 - p) / count + spread / (4 * count)) / (1 + spread)
    # At 0 of n and n of n the bound is exactly 0 or 1; rounding must not move it.
    low = 0.0 if passed == 0 else max(0.0, centre - half)
    high = 1.0 if passed == count else min(1.0, centre + half)
    return (low, high)


def _groups(labels: Sequence[Hashable | None]) -> list[Hashable]:
    """Each case's group, with a case labelled None in a group of its own."""
    return [("case", index) if label is None else ("group", label) for index, label in enumerate(labels)]


def design_effect(values: Sequence[float], groups: Sequence[Hashable | None]) -> float:
    """How much grouping inflates the variance of the mean: the CR1 design effect, at least 1.

    1 with fewer than two groups or no spread in ``values``."""
    if len(groups) != len(values):
        raise ValueError("give one group label per value")
    n = len(values)
    keys = _groups(groups)
    k = len(set(keys))
    if n < 2 or k < 2:
        return 1.0
    centre = fmean(values)
    residuals = [value - centre for value in values]
    spread = math.fsum(e * e for e in residuals)
    if spread == 0:
        return 1.0
    sums: dict[Hashable, float] = {}
    for key, e in zip(keys, residuals, strict=True):
        sums[key] = sums.get(key, 0.0) + e
    clustered = math.fsum(s * s for s in sums.values())
    return max(1.0, k * (n - 1) / ((k - 1) * n) * clustered / spread)


def proportion_interval(
    outcomes: Sequence[bool], groups: Sequence[Hashable | None] | None = None, confidence: float = CONFIDENCE
) -> Interval | None:
    """The interval for the share of ``outcomes`` that are True: Wilson, at the effective sample
    size when ``groups`` labels each outcome's group. None for no outcomes."""
    n = len(outcomes)
    if n == 0:
        return None
    passed = sum(bool(outcome) for outcome in outcomes)
    deff = 1.0 if groups is None else design_effect([float(bool(o)) for o in outcomes], groups)
    effective = n / deff
    return wilson(passed / n * effective, effective, confidence)


@lru_cache(maxsize=256)
def t_quantile(probability: float, df: float) -> float:
    """The ``probability`` quantile of Student's t with ``df`` degrees of freedom."""
    if not 0 < probability < 1 or df <= 0:
        raise ValueError("probability must be between 0 and 1 and df positive")
    if probability < 0.5:
        return -t_quantile(1 - probability, df)
    if probability == 0.5:
        return 0.0
    tail = 1 - probability  # the upper tail beyond t: half the incomplete beta below

    def upper(t: float) -> float:
        return 0.5 * _incomplete_beta(df / 2, 0.5, df / (df + t * t))

    low, high = 0.0, 1.0
    while upper(high) > tail:
        low, high = high, high * 2
    for _ in range(200):
        middle = (low + high) / 2
        if upper(middle) > tail:
            low = middle
        else:
            high = middle
        if high - low <= 1e-15 * high:
            break
    return (low + high) / 2


def _incomplete_beta(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b), by its continued fraction."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _beta_fraction(a, b, x) / a
    return 1 - front * _beta_fraction(b, a, 1 - x) / b


def _beta_fraction(a: float, b: float, x: float) -> float:
    """The continued fraction of the incomplete beta function, by the modified Lentz method."""
    tiny = 1e-300
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    result = d
    for m in range(1, 500):
        for numerator in (
            m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
            -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1)),
        ):
            d = 1 + numerator * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + numerator / c
            c = c if abs(c) > tiny else tiny
            result *= c * d
        if abs(c * d - 1) < 1e-15:
            break
    return result


def mean_interval(
    values: Sequence[float], groups: Sequence[Hashable | None] | None = None, confidence: float = CONFIDENCE
) -> Interval | None:
    """The Student t interval for the mean of ``values``, widened by the design effect when
    ``groups`` labels each value's group. None for fewer than two values."""
    n = len(values)
    if n < 2:
        return None
    centre = fmean(values)
    variance = math.fsum((value - centre) ** 2 for value in values) / (n - 1)
    df: float = n - 1
    if groups is not None and (k := len(set(_groups(groups)))) >= 2:
        variance *= design_effect(values, groups)
        df = k - 1
    half = t_quantile(0.5 + confidence / 2, df) * math.sqrt(variance / n)
    return (centre - half, centre + half)


# --- paired comparison ----------------------------------------------------------------------


def mcnemar_exact(improved: int, regressed: int) -> float:
    """The two-sided exact McNemar p-value: the binomial test that a changed pair is as likely to
    have ``improved`` as ``regressed``, twice the smaller tail and at most 1. 1 when no pair
    changed. Exact integer arithmetic; only the final division rounds."""
    if improved < 0 or regressed < 0:
        raise ValueError("counts must not be negative")
    changed = improved + regressed
    term = tail = 1  # C(changed, 0), then the sum of C(changed, i) for i up to the smaller count
    for i in range(1, min(improved, regressed) + 1):
        term = term * (changed - i + 1) // i
        tail += term
    return min(1.0, 2 * tail / (1 << changed))


def paired_t(differences: Sequence[float]) -> float | None:
    """The two-sided p-value of the paired t test that the mean of ``differences`` is 0, on n − 1
    degrees of freedom. None for fewer than two differences. With no spread it is 1 when every
    difference is 0 and 0 when they are equal and not 0."""
    n = len(differences)
    if n < 2:
        return None
    if len(set(differences)) == 1:
        return 1.0 if differences[0] == 0 else 0.0
    centre = fmean(differences)
    variance = math.fsum((d - centre) ** 2 for d in differences) / (n - 1)
    t = centre / math.sqrt(variance / n)
    df = n - 1
    return _incomplete_beta(df / 2, 0.5, df / (df + t * t))


@dataclass(frozen=True)
class PairedComparison:
    """Two runs compared case by case on one measure: a pass rate or a mean score.

    ``baseline`` and ``candidate`` are the measure over the compared pairs, and ``effect`` the
    candidate's minus the baseline's. ``improved`` and ``regressed`` count the pairs that moved
    the better or the worse way. ``minimum_detectable_effect`` is the smallest change of the
    measure that this comparison could label: for a pass rate, six cases all one way over the
    compared pairs; for a mean, the half-width of the difference's 95% t interval. None when no
    change could be labelled: fewer than six pairs, or differences with no spread."""

    label: Label
    test: Literal["mcnemar_exact", "paired_t"]
    p_value: float | None
    alpha: float
    compared: int
    changed: int
    improved: int
    regressed: int
    baseline: float
    candidate: float
    effect: float
    minimum_detectable_effect: float | None


def _label(p_value: float | None, changed: int, gain: float) -> Label:
    if p_value is None or p_value >= SIGNIFICANCE or changed < MINIMUM_CHANGED:
        return "within_noise"
    return "better" if gain > 0 else "worse"


def compare_outcomes(
    baseline: Sequence[bool], candidate: Sequence[bool], *, higher_is_better: bool = True
) -> PairedComparison | None:
    """Pass/fail outcomes of the same cases, paired by position, by the exact McNemar test.
    ``higher_is_better`` False makes True the worse outcome. None for no pairs."""
    if len(baseline) != len(candidate):
        raise ValueError("give one candidate outcome per baseline outcome")
    n = len(baseline)
    if n == 0:
        return None
    sign = 1 if higher_is_better else -1
    moves = [sign * (bool(after) - bool(before)) for before, after in zip(baseline, candidate, strict=True)]
    improved, regressed = moves.count(1), moves.count(-1)
    p_value = mcnemar_exact(improved, regressed)
    before, after = sum(map(bool, baseline)) / n, sum(map(bool, candidate)) / n
    return PairedComparison(
        label=_label(p_value, improved + regressed, improved - regressed),
        test="mcnemar_exact",
        p_value=p_value,
        alpha=SIGNIFICANCE,
        compared=n,
        changed=improved + regressed,
        improved=improved,
        regressed=regressed,
        baseline=before,
        candidate=after,
        effect=after - before,
        minimum_detectable_effect=MINIMUM_CHANGED / n if n >= MINIMUM_CHANGED else None,
    )


def compare_scores(
    baseline: Sequence[float], candidate: Sequence[float], *, higher_is_better: bool = True
) -> PairedComparison | None:
    """Scores of the same cases, paired by position, by the paired t test on candidate minus
    baseline. ``higher_is_better`` False makes a lower score the better one. None for no pairs."""
    if len(baseline) != len(candidate):
        raise ValueError("give one candidate score per baseline score")
    n = len(baseline)
    if n == 0:
        return None
    differences = [float(after) - float(before) for before, after in zip(baseline, candidate, strict=True)]
    sign = 1 if higher_is_better else -1
    improved = sum(sign * d > 0 for d in differences)
    regressed = sum(sign * d < 0 for d in differences)
    effect = fmean(differences)
    p_value = paired_t(differences)
    found = mean_interval(differences) if len(set(differences)) > 1 else None
    return PairedComparison(
        label=_label(p_value, improved + regressed, sign * effect),
        test="paired_t",
        p_value=p_value,
        alpha=SIGNIFICANCE,
        compared=n,
        changed=improved + regressed,
        improved=improved,
        regressed=regressed,
        baseline=fmean(map(float, baseline)),
        candidate=fmean(map(float, candidate)),
        effect=effect,
        minimum_detectable_effect=(found[1] - found[0]) / 2 if found and n >= MINIMUM_CHANGED else None,
    )


def rate_text(rate: float | None, interval: Sequence[float] | None) -> str:
    """A rate and its interval as the SDK and the Evals UI write them: ``62% (48–75%)``. The rate is
    rounded to the nearest whole percent and the bounds outward, so the written interval holds the
    computed one. ``—`` when there is no rate."""
    if rate is None or interval is None:
        return "—"
    low, high = interval
    return f"{math.floor(rate * 100 + 0.5)}% ({math.floor(low * 100 + 1e-9)}–{math.ceil(high * 100 - 1e-9)}%)"


__all__ = [
    "CONFIDENCE",
    "MINIMUM_CHANGED",
    "SIGNIFICANCE",
    "Interval",
    "Label",
    "PairedComparison",
    "compare_outcomes",
    "compare_scores",
    "design_effect",
    "mcnemar_exact",
    "mean_interval",
    "paired_t",
    "proportion_interval",
    "rate_text",
    "t_quantile",
    "wilson",
]
