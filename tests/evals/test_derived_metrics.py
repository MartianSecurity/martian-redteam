import pytest

from martian.evals.derived_metrics import DerivedMetrics, Formula


def formulas(**values: str) -> DerivedMetrics:
    return DerivedMetrics(formulas=tuple(Formula(name=name, value=value) for name, value in values.items()))


def test_formula_order_numeric_comparisons_and_missing_inputs():
    plan = formulas(
        mean="quality / __count",
        guarded="(0 < mean) == (__count >= 3) ? -(mean + 0x2) / +2 : absent",
        undefined="0 / 0",
        without_guess="absent + 1",
        chained="undefined ? absent : 1",
    )
    reports = plan.report([{"name": "quality", "score_total": 9.0}], cases=3)
    assert [(row["score"], row["error"]) for row in reports] == [
        (3, None),
        (-2.5, None),
        (None, "Calculation did not produce a finite score"),
        (None, "Missing measurement: absent"),
        (1, None),
    ]


@pytest.mark.parametrize(
    "formula",
    ["0 < total < 3", "3 ? 1 : max(2, 3)", "total = 7", "total.value", "total ** 2", "3; 1", "1" * 1025],
)
def test_unsupported_syntax_fails_during_suite_validation(formula):
    with pytest.raises(ValueError, match="score|1024"):
        formulas(score=formula)


def test_formula_names_cannot_depend_on_their_previous_values_or_replace_measurements():
    with pytest.raises(ValueError, match="define later before using it"):
        formulas(earlier="later + 1", later="1")
    with pytest.raises(ValueError, match="reuse measured metric names"):
        formulas(quality="1").validate_measurements(["quality"])
