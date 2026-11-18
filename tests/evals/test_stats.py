"""Confidence intervals and paired comparisons of pass rates and scores (martian.evals.stats), and
where the core reports them."""

import math
import random
import uuid
from datetime import UTC, datetime

import pytest

from martian.evals.model import Run
from martian.evals.server.projection import Assessment
from martian.evals.server.quality import compare_assessments, compare_runs, scorecard
from martian.evals.server.schemas import EvalItemOut
from martian.evals.stats import (
    MINIMUM_CHANGED,
    SIGNIFICANCE,
    compare_outcomes,
    compare_scores,
    design_effect,
    mcnemar_exact,
    mean_interval,
    paired_t,
    proportion_interval,
    rate_text,
    t_quantile,
    wilson,
)

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


# --- paired comparison ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("improved", "regressed", "expected"),
    [
        (0, 0, 1.0),
        (3, 3, 1.0),  # twice the tail is 42/32, held at 1
        (0, 5, 2 / 32),  # five all one way: 0.0625, never below 5%
        (0, 6, 2 / 64),  # six all one way: 0.03125, the fewest that is
        (1, 9, 22 / 1024),  # 2 × (1 + 10) / 2¹⁰
        (2, 8, 112 / 1024),  # 2 × (1 + 10 + 45) / 2¹⁰
        (2, 10, 158 / 4096),  # 2 × (1 + 12 + 66) / 2¹², R's binom.test(2, 12) = 0.03857
        (10, 2, 158 / 4096),
        (7, 3, 0.34375),  # 2 × (1 + 10 + 45 + 120) / 2¹⁰
    ],
)
def test_the_exact_mcnemar_test_gives_the_binomial_tail(improved, regressed, expected):
    assert mcnemar_exact(improved, regressed) == expected


def test_the_exact_mcnemar_test_stays_exact_for_many_changes_and_refuses_negative_counts():
    # 500 one way and 500 the other: the two tails meet at the middle and the p-value is 1.
    assert mcnemar_exact(500, 500) == 1.0
    close(mcnemar_exact(400, 600), 2 * math.fsum(math.comb(1000, i) for i in range(401)) / 2**1000, 1e-15)
    with pytest.raises(ValueError):
        mcnemar_exact(-1, 3)


def test_the_paired_t_test_matches_students_sleep_data():
    # Cushny and Peebles' extra hours of sleep, drug 2 minus drug 1: t = 4.0621 on 9 df, and R's
    # t.test(extra ~ group, data = sleep, paired = TRUE) gives p = 0.002833.
    differences = [1.2, 2.4, 1.3, 1.3, 0.0, 1.0, 1.8, 0.8, 4.6, 1.4]
    close(paired_t(differences), 0.0028328902, 1e-9)
    # 1..5: t = 3 / √(2.5 / 5) = 4.2426 on 4 df.
    close(paired_t([1, 2, 3, 4, 5]), 0.0132355996, 1e-9)
    # It agrees with the t interval of the mean difference: significant exactly when 0 is outside.
    low, high = mean_interval(differences)
    assert low > 0


def test_the_paired_t_test_needs_two_differences_and_reads_no_spread_plainly():
    assert paired_t([]) is None and paired_t([0.5]) is None
    assert paired_t([0.0, 0.0, 0.0]) == 1.0
    assert paired_t([-0.2, -0.2, -0.2]) == 0.0


def test_two_runs_of_a_deterministic_system_are_within_noise():
    outcomes = [position % 5 != 0 for position in range(40)]
    found = compare_outcomes(outcomes, list(outcomes))
    assert (found.label, found.test, found.p_value, found.changed, found.effect) == (
        "within_noise",
        "mcnemar_exact",
        1.0,
        0,
        0.0,
    )
    scores = [position / 40 for position in range(40)]
    same = compare_scores(scores, list(scores))
    assert (same.label, same.test, same.p_value, same.changed) == ("within_noise", "paired_t", 1.0, 0)


def test_a_seeded_regression_is_worse_and_its_reverse_better():
    rng = random.Random(182)
    baseline = [position % 5 != 0 for position in range(40)]
    broken = set(rng.sample([p for p in range(40) if baseline[p]], 12))
    fixed = set(rng.sample([p for p in range(40) if not baseline[p]], 2))
    candidate = [(ok and p not in broken) or p in fixed for p, ok in enumerate(baseline)]
    worse = compare_outcomes(baseline, candidate)
    assert (worse.label, worse.improved, worse.regressed, worse.compared) == ("worse", 2, 12, 40)
    assert worse.p_value == 212 / 16384 < SIGNIFICANCE  # 2 × (1 + 14 + 91) / 2¹⁴
    close((worse.baseline, worse.candidate, worse.effect), (32 / 40, 22 / 40, -10 / 40), 1e-12)
    assert compare_outcomes(candidate, baseline).label == "better"


def test_fewer_than_six_changes_are_within_noise_whatever_the_test_says():
    # Five regressions out of five: the exact test cannot reach 5%.
    assert compare_outcomes([True] * 5, [False] * 5).label == "within_noise"
    # Five scores that each drop by the same 0.1 out of 100: the t test alone would call it.
    baseline = [0.5] * 100
    candidate = [0.4] * 5 + [0.5] * 95
    assert paired_t([b - a for a, b in zip(baseline, candidate, strict=True)]) < SIGNIFICANCE
    five = compare_scores(baseline, candidate)
    assert (five.label, five.changed) == ("within_noise", 5)
    six = compare_scores(baseline, [0.4] * 6 + [0.5] * 94)
    assert (six.label, six.changed, MINIMUM_CHANGED) == ("worse", 6, 6)


def test_a_flaky_system_compared_with_itself_is_rarely_labelled():
    # The same system, each case passing nine times in ten, run twice under 200 seeds: at a 5%
    # level at most one comparison in twenty may be labelled, and the exact test keeps under it.
    labelled = 0
    for seed in range(200):
        rng = random.Random(seed)
        first = [rng.random() < 0.9 for _ in range(60)]
        second = [rng.random() < 0.9 for _ in range(60)]
        labelled += compare_outcomes(first, second).label != "within_noise"
    assert labelled <= 10


def test_the_minimum_detectable_effect_is_six_cases_or_the_t_half_width():
    assert compare_outcomes([True] * 40, [True] * 40).minimum_detectable_effect == 6 / 40
    # Too few pairs to label any change.
    assert compare_outcomes([True] * 5, [True] * 5).minimum_detectable_effect is None
    baseline = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    candidate = [1.5, 2.0, 2.5, 4.5, 5.0, 5.5]
    differences = [b - a for a, b in zip(baseline, candidate, strict=True)]
    low, high = mean_interval(differences)
    found = compare_scores(baseline, candidate)
    close(found.minimum_detectable_effect, (high - low) / 2, 1e-12)
    assert found.label == "within_noise" and abs(found.effect) < found.minimum_detectable_effect
    # Differences with no spread give no estimate of the noise.
    assert compare_scores([1.0] * 6, [1.0] * 6).minimum_detectable_effect is None


def test_a_lower_is_better_measure_flips_the_label():
    falls = compare_scores([float(p) for p in range(10)], [p - 1.0 for p in range(10)], higher_is_better=False)
    assert (falls.label, falls.improved, falls.regressed, falls.effect) == ("better", 10, 0, -1.0)
    leaks = compare_outcomes([False] * 10, [True] * 10, higher_is_better=False)
    assert (leaks.label, leaks.regressed) == ("worse", 10)


def test_a_comparison_needs_pairs_of_the_same_length():
    assert compare_outcomes([], []) is None and compare_scores([], []) is None
    with pytest.raises(ValueError):
        compare_outcomes([True], [])
    with pytest.raises(ValueError):
        compare_scores([1.0], [1.0, 2.0])


# --- the core's scorecard and comparison ----------------------------------------------------


def item(position, status="passed"):
    return EvalItemOut(
        id=uuid.uuid4(),
        dataset_item_id=f"case-{position}",
        position=position,
        status=status,
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


def completed_run(run_id):
    at = datetime(2026, 11, 15, tzinfo=UTC)
    return Run(
        id=run_id,
        project_id="default",
        code=run_id,
        source="local",
        state="completed",
        dataset_name="cases",
        dataset_digest="d" * 64,
        created_at=at,
        provenance={"dataset": {"digest": "d" * 64}},
    )


def job(run_id, policies):
    return {"id": f"job-{run_id}", "config": {"scorers": ["correct", "latency", "tone"], "policies": policies}}


def test_a_comparison_labels_the_exact_match_and_each_scorer():
    # Twelve cases: the candidate breaks eight exact matches, answers about a second faster
    # everywhere (lower latency is better), and changes the tone of none.
    left = [item(p) for p in range(12)]
    right = [item(p, "failed" if p < 8 else "passed") for p in range(12)]
    for i in right:
        i.dataset_item_id = f"case-{i.position}"
    result = compare_runs(completed_run("a"), left, completed_run("b"), right)
    assert result["changes"]["regressed"] == 8
    canonical = result["significance"]
    assert (canonical["label"], canonical["test"], canonical["regressed"], canonical["alpha"]) == (
        "worse",
        "mcnemar_exact",
        8,
        0.05,
    )
    assert canonical["p_value"] == 2 / 256
    assert set(canonical) == {
        "label",
        "test",
        "p_value",
        "alpha",
        "compared",
        "changed",
        "improved",
        "regressed",
        "baseline",
        "candidate",
        "effect",
        "minimum_detectable_effect",
    }

    def scores(items, correct, latency):
        return [
            row
            for p, i in enumerate(items)
            for row in (score(i, "correct", correct(p)), score(i, "latency", latency(p)), score(i, "tone", "calm"))
        ]

    policies = [{"name": "latency", "direction": "lower", "minimum_coverage": 1, "threshold": None}]
    scored = compare_assessments(
        result,
        left,
        right,
        job("a", policies),
        job("b", policies),
        scores(left, lambda p: True, lambda p: 3.0 + p % 3),
        scores(right, lambda p: p >= 1, lambda p: 2.0 + p % 3 + 0.25 * (p % 2)),
    )
    by_name = scored["criteria_significance"]
    assert (by_name["correct"]["label"], by_name["correct"]["regressed"]) == ("within_noise", 1)
    assert (by_name["latency"]["label"], by_name["latency"]["test"], by_name["latency"]["improved"]) == (
        "better",
        "paired_t",
        12,
    )
    assert by_name["latency"]["effect"] == pytest.approx(-0.875)
    assert by_name["latency"]["p_value"] < 1e-6 and by_name["latency"]["minimum_detectable_effect"] > 0
    assert by_name["tone"] is None  # categorical values have no direction to test


def test_an_incompatible_comparison_has_no_label():
    left = [item(p) for p in range(8)]
    other = completed_run("b").model_copy(update={"dataset_digest": "e" * 64, "provenance": {}})
    result = compare_runs(completed_run("a"), left, other, [item(p, "failed") for p in range(8)])
    assert not result["compatible"] and result["significance"] is None
