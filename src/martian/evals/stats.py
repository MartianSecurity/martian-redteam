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
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence
from functools import lru_cache
from statistics import NormalDist, fmean

CONFIDENCE = 0.95
"""The confidence level of every interval the SDK, the core and the Evals UI report."""

Interval = tuple[float, float]


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
    "Interval",
    "design_effect",
    "mean_interval",
    "proportion_interval",
    "rate_text",
    "t_quantile",
    "wilson",
]
