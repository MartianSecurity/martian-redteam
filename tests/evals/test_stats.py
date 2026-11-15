"""Confidence intervals for pass rates and scores (martian.evals.stats) and where the core reports them."""

import math
import uuid

import pytest

from martian.evals.server.projection import Assessment
from martian.evals.server.quality import scorecard
from martian.evals.server.schemas import EvalItemOut
from martian.evals.stats import design_effect, mean_interval, proportion_interval, rate_text, t_quantile, wilson

Z = 1.959963984540054  # the 97.5% normal quantile


def close(found, expected, tolerance=1e-9):
    assert found is not None
    assert found == pytest.approx(expected, abs=tolerance)


# --- Wilson ---------------------------------------------------------------------------------


def test_wilson_matches_its_closed_form_and_published_values():
    close(wilson(5, 10), (0.2365930905, 0.7634069095))
    close(wilson(2, 3), (0.2076596008, 0.9385080553))
    close(wilson(81, 263), (0.2553, 0.3662), 1e-4)


def test_wilson_at_the_edges_of_a_small_sample():
    # 0 of n and n of n: the bound at the edge is exact, the other is z² / (n + z²) from it.
    for n in (1, 3, 10):
        close(wilson(0, n), (0.0, Z * Z / (n + Z * Z)))
        close(wilson(n, n), (n / (n + Z * Z), 1.0))
    assert wilson(0, 5)[0] == 0.0 and wilson(5, 5)[1] == 1.0
    # n = 1 is never a zero-width interval, unlike the normal approximation.
    close(wilson(1, 1), (0.2065493144, 1.0))
    close(wilson(0, 1), (0.0, 0.7934506856))
    assert wilson(0, 0) is None
    with pytest.raises(ValueError):
        wilson(4, 3)


def test_a_higher_confidence_widens_the_interval():
    low95, high95 = wilson(5, 10)
    low99, high99 = wilson(5, 10, confidence=0.99)
    assert low99 < low95 and high99 > high95


# --- Student t ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("probability", "df", "expected"),
    [
        (0.975, 1, 12.706204736),
        (0.975, 2, 4.302652730),
        (0.975, 5, 2.570581836),
        (0.975, 10, 2.228138852),
        (0.975, 30, 2.042272456),
        (0.975, 1000, 1.962339081),
        (0.995, 1, 63.656741163),
        (0.9, 4, 1.533206274),
        (0.025, 7, -2.364624252),
    ],
)
def test_t_quantiles_match_the_published_table(probability, df, expected):
    assert t_quantile(probability, df) == pytest.approx(expected, abs=1e-8)


def test_the_t_interval_for_a_mean():
    # 1..5: mean 3, s = √2.5, t(0.975, 4) = 2.776445105.
    half = 2.776445105 * math.sqrt(2.5 / 5)
    close(mean_interval([1, 2, 3, 4, 5]), (3 - half, 3 + half), 1e-8)
    # Two values are the fewest that have a spread.
    close(mean_interval([0.0, 1.0]), (0.5 - 12.706204736 * 0.5, 0.5 + 12.706204736 * 0.5), 1e-8)


def test_a_mean_needs_two_values_and_no_spread_is_zero_width():
    assert mean_interval([]) is None and mean_interval([4.0]) is None
    assert mean_interval([2.0, 2.0, 2.0]) == (2.0, 2.0)


# --- grouped cases --------------------------------------------------------------------------


def test_cases_in_groups_of_one_give_the_ungrouped_intervals():
    outcomes = [True, False, True, True, False, True]
    assert design_effect([float(o) for o in outcomes], list("abcdef")) == 1.0
    close(proportion_interval(outcomes, list("abcdef")), wilson(4, 6), 1e-12)
    close(proportion_interval(outcomes, [None] * 6), wilson(4, 6), 1e-12)
    values = [1.0, 4.0, 2.0, 8.0]
    close(mean_interval(values, list("abcd")), mean_interval(values), 1e-12)


def test_groups_that_agree_inside_widen_a_pass_rate():
    # Two groups of four that each agree: deff = k(n-1)/((k-1)n) × Σ(Σe)²/Σe² = 2·7/8 × 8/2 = 7,
    # so the eight cases weigh as 8/7 of a case.
    outcomes = [True] * 4 + [False] * 4
    groups = ["a"] * 4 + ["b"] * 4
    assert design_effect([float(o) for o in outcomes], groups) == pytest.approx(7.0)
    grouped = proportion_interval(outcomes, groups)
    close(grouped, wilson(4 / 7, 8 / 7), 1e-12)
    close(grouped, (0.0610497, 0.9389503), 1e-6)
    low, high = wilson(4, 8)
    assert grouped[0] < low and grouped[1] > high


def test_grouping_never_narrows_an_interval():
    # Each group holds one pass and one fail, so the groups differ less than independent cases
    # would: the design effect would be below 1, and stays at 1.
    outcomes = [True, False] * 4
    groups = ["a", "a", "b", "b", "c", "c", "d", "d"]
    assert design_effect([float(o) for o in outcomes], groups) == 1.0
    close(proportion_interval(outcomes, groups), wilson(4, 8), 1e-12)


def test_a_grouped_mean_is_the_t_interval_of_its_group_means_when_groups_are_alike():
    # Equal groups whose cases score alike: CR1 gives exactly the t interval over the group means,
    # on k - 1 = 2 degrees of freedom.
    values = [1.0, 1.0, 3.0, 3.0, 5.0, 5.0]
    groups = ["a", "a", "b", "b", "c", "c"]
    expected = mean_interval([1.0, 3.0, 5.0])
    close(mean_interval(values, groups), expected, 1e-9)
    close(expected, (3 - 4.302652730 * 2 / math.sqrt(3), 3 + 4.302652730 * 2 / math.sqrt(3)), 1e-8)


def test_without_two_groups_or_any_spread_the_cases_count_as_independent():
    assert design_effect([1.0, 0.0, 1.0], ["only"] * 3) == 1.0
    close(proportion_interval([True, False, True], ["only"] * 3), wilson(2, 3), 1e-12)
    close(mean_interval([1.0, 2.0, 4.0], ["only"] * 3), mean_interval([1.0, 2.0, 4.0]), 1e-12)
    # All passing: no spread to estimate the grouping from, so 0 of n and n of n keep their bounds.
    close(proportion_interval([True] * 6, list("aabbcc")), wilson(6, 6), 1e-12)
    close(proportion_interval([False] * 6, list("aabbcc")), wilson(0, 6), 1e-12)
    assert proportion_interval([]) is None
    with pytest.raises(ValueError):
        design_effect([1.0], ["a", "b"])


# --- how a rate is written ------------------------------------------------------------------


def test_a_rate_is_rounded_to_the_nearest_percent_and_its_bounds_outward():
    assert rate_text(2 / 3, wilson(2, 3)) == "67% (20–94%)"
    assert rate_text(1 / 3, wilson(1, 3)) == "33% (6–80%)"
    assert rate_text(1.0, wilson(3, 3)) == "100% (43–100%)"
    assert rate_text(0.0, wilson(0, 3)) == "0% (0–57%)"
    # Outward, so a near-certain rate never reads as a single point.
    assert rate_text(1.0, wilson(1000, 1000)) == "100% (99–100%)"
    assert rate_text(0.125, (0.1, 0.2)) == "13% (10–20%)"
    assert rate_text(None, None) == "—"


# --- the core's scorecard -------------------------------------------------------------------


def item(position):
    return EvalItemOut(
        id=uuid.uuid4(),
        dataset_item_id=f"case-{position}",
        position=position,
        status="passed",
        prompt="",
        expected="",
        output="",
        error="",
        finished_at=None,
    )


def score(target, name, value):
    return Assessment(
        id=uuid.uuid4(),
        item_id=target.id,
        job_id="job",
        name=name,
        value=value,
        rationale="",
        source="CODE",
        source_id=name,
        provenance={},
        error="",
        native_id=None,
        telemetry_state="unavailable",
        created_at=None,
    )


def test_the_scorecard_reports_a_wilson_interval_for_pass_rates_and_a_t_interval_for_means():
    items = [item(position) for position in range(5)]
    lengths = [1.0, 2.0, 3.0, 4.0, 5.0]
    scores = [score(i, "correct", position < 3) for position, i in enumerate(items)]
    scores += [score(i, "response_length", value) for i, value in zip(items, lengths, strict=True)]
    cards = {
        card["name"]: card for card in scorecard(items, scores, {"scorers": ["correct", "response_length"]})["criteria"]
    }
    correct, length = cards["correct"], cards["response_length"]
    close(correct["confidence_95"], wilson(3, 5))
    assert correct["mean_confidence_95"] is None
    close(length["mean_confidence_95"], mean_interval(lengths))
    assert length["confidence_95"] is None and length["mean"] == 3.0


def test_a_scorecard_of_one_value_has_no_mean_interval_and_one_verdict_a_wide_one():
    items = [item(0)]
    found = scorecard(items, [score(items[0], "correct", True)], {"scorers": ["correct"]})["criteria"][0]
    close(found["confidence_95"], wilson(1, 1))
    length = scorecard(items, [score(items[0], "response_length", 7.0)], {"scorers": ["response_length"]})
    assert length["criteria"][0]["mean_confidence_95"] is None
