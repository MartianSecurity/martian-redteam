"""A judge version's agreement with people's labels (martian.evals.agreement), and the evals core's
own route that reports it for every judge version of a project. No model is called."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from martian.evals.agreement import (
    MINIMUM_LABELLED,
    cohen_kappa,
    compare,
    concordance,
    item_of,
    judge_agreement,
    results_agreement,
)
from martian.evals.model import CaseResult, Project, Review, Run, Score, ScorerVersion, StoreRef, Trace
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER, create_app
from martian.evals.stats import mean_interval, wilson
from martian.evals.store import MemoryStore
from martian.evals.store.mlflow_local import MlflowStore

T0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


def close(found, expected, tolerance=1e-12):
    assert found is not None
    assert found == pytest.approx(expected, abs=tolerance)


def pass_fail(both_pass: int, false_pass: int, false_fail: int, both_fail: int) -> list[tuple[bool, bool]]:
    """Judge-and-human pairs with these four counts."""
    return (
        [(True, True)] * both_pass
        + [(True, False)] * false_pass
        + [(False, True)] * false_fail
        + [(False, False)] * both_fail
    )


# --- the statistics ---------------------------------------------------------------------------


def test_pass_fail_agreement_its_interval_kappa_and_both_error_rates():
    # The textbook example: 20 both pass, 5 false passes, 10 false fails, 15 both fail. p_o = 0.7,
    # the judge passes 25 of 50 and people 30, so p_e = 0.5 × 0.6 + 0.5 × 0.4 = 0.5 and kappa 0.4.
    found = compare(pass_fail(20, 5, 10, 15))
    assert (found.kind, found.status, found.labelled, found.minimum) == ("boolean", "measured", 50, MINIMUM_LABELLED)
    assert (found.both_pass, found.false_pass, found.false_fail, found.both_fail, found.agreed) == (20, 5, 10, 15, 35)
    close(found.agreement, 0.7)
    assert found.agreement_confidence_95 == wilson(35, 50)
    close(found.kappa, 0.4)
    # False passes are of the 20 items people failed; false fails of the 30 they passed.
    close(found.false_pass_rate, 5 / 20)
    assert found.false_pass_confidence_95 == wilson(5, 20)
    close(found.false_fail_rate, 10 / 30)
    assert found.false_fail_confidence_95 == wilson(10, 30)
    assert found.categories is None and found.mean_difference is None


def test_too_few_labelled_items_report_the_counts_and_no_number():
    found = compare(pass_fail(20, 1, 1, MINIMUM_LABELLED - 23))
    assert (found.status, found.labelled) == ("too_few", MINIMUM_LABELLED - 1)
    assert (found.both_pass, found.false_pass, found.false_fail, found.agreed) == (20, 1, 1, MINIMUM_LABELLED - 3)
    assert found.agreement is None and found.agreement_confidence_95 is None and found.kappa is None
    assert found.false_pass_rate is None and found.false_fail_confidence_95 is None
    assert compare(pass_fail(20, 1, 1, MINIMUM_LABELLED - 22)).status == "measured"
    assert compare([]).status == "too_few" and compare([]).labelled == 0
    with pytest.raises(ValueError):
        compare([(True, True)], minimum=0)


def test_kappa_is_exact_and_undefined_when_both_sides_never_vary():
    assert cohen_kappa(pass_fail(10, 0, 0, 10)) == 1.0
    assert cohen_kappa(pass_fail(0, 10, 10, 0)) == -1.0
    # Agreeing as often as chance would is a kappa of 0, however high the agreement looks.
    assert cohen_kappa(pass_fail(81, 9, 9, 1)) == 0.0
    assert cohen_kappa(pass_fail(40, 0, 0, 0)) is None and cohen_kappa([]) is None
    # A rate with nothing to be a share of is no rate.
    found = compare(pass_fail(40, 0, 0, 0))
    assert found.agreement == 1.0 and found.kappa is None
    assert found.false_pass_rate is None and found.false_pass_confidence_95 is None and found.false_fail_rate == 0.0


def test_categories_are_counted_pair_by_pair_with_kappa_over_every_category():
    pairs = [("good", "good")] * 14 + [("good", "poor")] * 4 + [("poor", "poor")] * 10 + [("fair", "poor")] * 2
    found = compare(pairs)
    assert (found.kind, found.status, found.agreed) == ("categorical", "measured", 24)
    assert found.categories == (("fair", "poor", 2), ("good", "good", 14), ("good", "poor", 4), ("poor", "poor", 10))
    # S = 18 × 14 + 10 × 16 = 412: kappa = (30 × 24 − 412) / (900 − 412).
    close(found.kappa, (30 * 24 - 412) / (900 - 412))
    assert found.both_pass is None and found.false_pass_rate is None
    # A True and a 1 are different answers: the kinds differ, so they are not compared at all.
    with pytest.raises(ValueError, match="every value must be boolean"):
        compare([(True, True), (1, True)])


def test_numbers_report_the_bias_its_interval_the_distance_and_lins_concordance():
    # Every verdict is its label plus one: no spread between them, a bias of 1.
    pairs = [(float(i % 5 + 2), float(i % 5 + 1)) for i in range(30)]
    found = compare(pairs, "numeric")
    assert (found.kind, found.status, found.agreed, found.categories) == ("numeric", "measured", None, None)
    close(found.mean_difference, 1.0)
    assert found.mean_difference_confidence_95 == mean_interval([1.0] * 30)
    close(found.mean_absolute_difference, 1.0)
    # Lin's coefficient: s_x² = s_y² = s_xy = 2 and (x̄ − ȳ)² = 1, so 2 × 2 / (2 + 2 + 1) = 0.8.
    close(found.concordance, 0.8)
    close(concordance([(1, 2), (2, 3), (3, 4), (4, 5), (5, 6)]), 0.8)
    assert concordance([(3, 3), (3, 3)]) == 1.0 and concordance([(1, 1), (2, 2)]) == 1.0
    close(concordance([(1, 2), (2, 1)]), -1.0)
    assert compare(pairs[:5], "numeric").mean_difference is None


def test_free_text_verdicts_are_not_compared():
    found = compare([("Polite.", "Polite.")] * 40, "text")
    assert (found.status, found.labelled, found.agreed, found.agreement) == ("not_comparable", 40, None, None)


# --- matching verdicts and labels ---------------------------------------------------------------


def verdict(case: str | None, value, *, version: str = "2", run: str = "run-1", trace: str | None = None, minutes=0):
    return Score(
        run_id=run if case else None,
        case_id=case,
        trace_id=trace,
        scorer="polite_tone",
        scorer_version=version,
        source="llm_judge",
        source_id="openai:/judge@2026-11",
        value=value,
        passed=value if isinstance(value, bool) else None,
        created_at=T0 + timedelta(minutes=minutes),
    )


def review(case: str, value, *, criterion: str = "polite_tone", minutes=5, state: str = "complete") -> Review:
    return Review(
        id=f"review-{case}-{criterion}-{minutes}",
        run_id="run-1",
        case_id=case,
        criterion=criterion,
        state=state,  # type: ignore[arg-type]
        value=value if state == "complete" else None,
        created_at=T0 + timedelta(minutes=minutes),
    )


def annotation(trace: str, value, *, minutes=5, name: str = "polite_tone") -> Score:
    return Score(trace_id=trace, scorer=name, source="human", value=value, created_at=T0 + timedelta(minutes=minutes))


def test_a_verdict_and_a_label_meet_on_their_case_or_their_trace():
    assert item_of(verdict("c1", True, trace="tr-1")) == ("case", "run-1", "c1")
    assert item_of(annotation("tr-1", True), {"tr-1": ("run-1", "c1")}) == ("case", "run-1", "c1")
    assert item_of(annotation("tr-live", True), {"tr-1": ("run-1", "c1")}) == ("trace", "tr-live")
    assert item_of(review("c1", True)) == ("case", "run-1", "c1")
    assert item_of(Score(scorer="polite_tone", source="human", value=True)) is None


def test_each_judge_version_is_reported_from_its_newest_verdict_and_each_items_newest_label():
    verdicts = [
        verdict("c1", True),
        verdict("c1", False, minutes=10),  # scored again: the newer verdict counts
        verdict("c2", True),
        verdict("c3", True),
        verdict("c4", True),
        verdict("c5", None),  # no verdict
        verdict(None, False, trace="tr-live"),
        verdict("c1", True, version="1"),
    ]
    labels = [
        review("c1", False),
        review("c2", True),
        review("c2", False, minutes=1),  # an older label of the same item
        annotation("tr-3", False),  # on c3's trace
        review("c4", "polite"),  # another kind than the judge's verdicts
        review("c4", True, criterion="human_review"),  # the case as a whole: no judge's
        review("c5", True),
        review("c6", True, state="pending"),
        annotation("tr-live", False),
    ]
    reports = judge_agreement(
        verdicts, labels, case_traces={"tr-3": ("run-1", "c3")}, versions=[("polite_tone", "3"), ("concise", "1")]
    )
    assert [(r.judge, r.version) for r in reports] == [
        ("concise", "1"),
        ("polite_tone", "1"),
        ("polite_tone", "2"),
        ("polite_tone", "3"),
    ]
    unused, first, second, third = reports
    assert (unused.judged, unused.agreement.labelled, unused.models) == (0, 0, ())
    assert (first.judged, first.agreement.labelled, first.agreement.false_pass) == (1, 1, 1)
    # c1 False/False, c2 True/True, c3 True/False, the live trace False/False; c4 is incomparable.
    assert (second.judged, second.incomparable, second.models) == (5, 1, ("openai:/judge@2026-11",))
    found = second.agreement
    assert (found.status, found.labelled, found.both_pass, found.false_pass, found.false_fail, found.both_fail) == (
        "too_few",
        4,
        1,
        1,
        0,
        2,
    )
    assert (third.judged, third.agreement.labelled, third.agreement.status) == (0, 0, "too_few")
    # A definition's kind wins over the verdicts' own.
    text = judge_agreement(verdicts, labels, kinds={("polite_tone", "2"): "text"})
    assert [r.agreement.status for r in text if r.version == "2"] == ["not_comparable"]


def test_the_report_as_the_core_serves_it():
    report = judge_agreement(
        [verdict(f"c{i}", i % 4 != 0) for i in range(40)], [review(f"c{i}", True) for i in range(40)]
    )
    served = report[0].as_dict()
    assert set(served) == {
        "judge", "version", "kind", "models", "panel", "judged", "labelled", "incomparable", "minimum", "status",
        "agreed",
        "agreement", "agreement_confidence_95", "kappa", "confusion", "false_pass_rate", "false_pass_confidence_95",
        "false_fail_rate", "false_fail_confidence_95", "categories", "mean_difference",
        "mean_difference_confidence_95", "mean_absolute_difference", "concordance",
    }  # fmt: skip
    assert served["confusion"] == {"both_pass": 30, "false_pass": 0, "false_fail": 10, "both_fail": 0}
    # People passed everything, so the judge's 75% is no better than chance: kappa 0.
    assert (served["labelled"], served["agreement"], served["kappa"]) == (40, 0.75, 0.0)
    assert served["agreement_confidence_95"] == list(wilson(30, 40)) and served["false_pass_rate"] is None
    assert json.loads(json.dumps(served)) == served


def test_a_suite_file_runs_checks_against_labels_by_case(tmp_path):
    records = [
        {
            "id": f"case-{i}",
            "passed": i % 3 != 0,
            "checks": {"rubric": {"passed": i % 3 != 0}, "contains": {"passed": True}},
        }
        for i in range(30)
    ]
    (tmp_path / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    labels = {f"case-{i}": {"rubric": i % 5 != 0} for i in range(30)} | {"case-0": {"rubirc": False}}
    reports = results_agreement(tmp_path, labels)
    assert [(r.judge, r.version, r.judged) for r in reports] == [("rubirc", "", 0), ("rubric", "", 30)]
    found = reports[1].agreement
    pairs = [(i % 3 != 0, i % 5 != 0) for i in range(1, 30)]
    expected = compare(pairs)
    assert (found.labelled, found.agreed, found.false_pass, found.false_fail) == (
        29,
        expected.agreed,
        expected.false_pass,
        expected.false_fail,
    )
    assert found.status == "too_few"
    assert results_agreement(records, labels, minimum=29)[1].agreement.kappa == cohen_kappa(pairs)


# --- the core's route ---------------------------------------------------------------------------

RUN = str(uuid.UUID(int=40))
JUDGE = ScorerVersion(name="polite_tone", kind="llm_judge", type="instructions", version="", model="openai:/judge")
DEFINITION = {
    "name": "polite_tone",
    "description": "",
    "instructions": "Is {{ outputs }} polite? Answer pass or fail.",
    "kind": "boolean",
    "options": [],
    "minimum": 0,
    "maximum": 1,
    "operation_id": "",
}


def tid(n: int) -> str:
    return "tr-" + f"{n:032x}"


async def seed(store, records, cases: int, *, judged: list[bool], labelled: list[bool]) -> None:
    """A registered judge with two versions, a run whose cases version 2 judged, each with a
    trace, and people's labels: reviews of the first cases and annotations of the others."""
    principal = SINGLE_USER
    await store.create_project(principal, Project(id=DEFAULT_PROJECT, name="Default", created_at=T0))
    for _ in range(2):
        await store.register_scorer(principal, DEFAULT_PROJECT, JUDGE.model_copy(update={"criteria": DEFINITION}))
    run = Run(id=RUN, project_id=DEFAULT_PROJECT, code="EV-1", source="console", state="completed", created_at=T0)
    await records.create_run(principal, run)
    await store.start_run(principal, run)
    results, scores = [], []
    for i in range(cases):
        case = f"c{i}"
        await store.record_trace(
            principal, Trace(id=tid(i), project_id=DEFAULT_PROJECT, name=case, state="ok", request_time=T0, run_id=RUN)
        )
        results.append(
            CaseResult(run_id=RUN, case_id=case, position=i, status="passed", store=StoreRef(trace_id=tid(i)))
        )
        score = Score(
            run_id=RUN,
            case_id=case,
            trace_id=tid(i),
            scorer="polite_tone",
            scorer_version="2",
            source="llm_judge",
            source_id="openai:/judge",
            value=judged[i],
            passed=judged[i],
            scoring_job_id="job-1",
            created_at=T0,
        )
        # Each scored value is also kept on its case's trace, as the worker keeps it.
        await store.log_score(principal, DEFAULT_PROJECT, tid(i), score)
        scores.append(score)
    await records.put_results(principal, DEFAULT_PROJECT, results)
    await records.put_scores(principal, DEFAULT_PROJECT, scores)
    for i, value in enumerate(labelled):
        at = T0 + timedelta(minutes=5)
        if i % 2 == 0:
            await records.add_review(
                principal,
                DEFAULT_PROJECT,
                Review(
                    id=f"review-{i}",
                    run_id=RUN,
                    case_id=f"c{i}",
                    criterion="polite_tone",
                    state="complete",
                    value=value,
                    created_at=at,
                ),  # fmt: skip
            )
            # A complete review's answer is also kept on the case's trace, as the review route keeps it.
            echo = {"martian.review_criterion": "polite_tone"}
            human = Score(run_id=RUN, case_id=f"c{i}", trace_id=tid(i), scorer="polite_tone", source="human",
                          value=value, metadata=echo, created_at=at)  # fmt: skip
            await store.log_score(principal, DEFAULT_PROJECT, tid(i), human)
        else:
            annotated = {"martian.annotation": "feedback"}
            human = Score(trace_id=tid(i), scorer="polite_tone", source="human", value=value, metadata=annotated,
                          created_at=at)  # fmt: skip
            await store.log_score(principal, DEFAULT_PROJECT, tid(i), human)


@pytest.fixture(params=["memory", "mlflow"])
def stores(request, tmp_path):
    store = MemoryStore() if request.param == "memory" else MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path)
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store, records
    records.close()


def report(store, records, **params):
    client = TestClient(create_app(store=store, records=records, principal=lambda: SINGLE_USER))
    response = client.get("/v1/evals/judge-agreement", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_route_matches_reviews_and_trace_annotations_to_each_judge_versions_verdicts(stores):
    store, records = stores
    judged, labelled = [True, True, False, True], [True, False, False]
    asyncio.run(seed(store, records, 4, judged=judged, labelled=labelled))
    principal = SINGLE_USER

    async def more():
        # An expectation is no label, and a live trace's judge and person meet on the trace.
        await store.log_score(
            principal,
            DEFAULT_PROJECT,
            tid(3),
            Score(trace_id=tid(3), scorer="polite_tone", source="human", value="Polite",
                  metadata={"martian.annotation": "expectation"}, created_at=T0),
        )  # fmt: skip
        await store.record_trace(principal, Trace(id=tid(99), project_id=DEFAULT_PROJECT, name="live", request_time=T0))
        live = Score(trace_id=tid(99), scorer="polite_tone", scorer_version="2", source="llm_judge",
                     source_id="openai:/judge", value=True, passed=True, created_at=T0)  # fmt: skip
        await store.log_score(principal, DEFAULT_PROJECT, tid(99), live)
        await store.log_score(principal, DEFAULT_PROJECT, tid(99), annotation(tid(99), False))
        # c2's answer is withdrawn: its current review is pending, so the copy of the earlier
        # answer on its trace is no label.
        withdrawn = Review(id="review-2b", run_id=RUN, case_id="c2", criterion="polite_tone", state="pending",
                           previous_id="review-2", created_at=T0 + timedelta(minutes=9))  # fmt: skip
        await records.add_review(principal, DEFAULT_PROJECT, withdrawn)

    asyncio.run(more())
    found = report(store, records)
    assert (found["project_id"], found["minimum"], found["traces_examined"], found["traces_truncated"]) == (
        None,
        MINIMUM_LABELLED,
        5,
        False,
    )
    assert [(r["judge"], r["version"]) for r in found["items"]] == [("polite_tone", "1"), ("polite_tone", "2")]
    unused, second = found["items"]
    assert (unused["judged"], unused["labelled"], unused["status"]) == (0, 0, "too_few")
    # c0 True/True (review), c1 True/False (annotation) and the live trace True/False; c2's
    # answer was withdrawn and c3 has only an expectation.
    assert (second["kind"], second["judged"], second["labelled"], second["status"]) == ("boolean", 5, 3, "too_few")
    assert second["confusion"] == {"both_pass": 1, "false_pass": 2, "false_fail": 0, "both_fail": 0}
    assert second["agreement"] is None and second["kappa"] is None and second["models"] == ["openai:/judge"]
    assert [r["judge"] for r in report(store, records, name="concise")["items"]] == []
    client = TestClient(create_app(store=store, records=records, principal=lambda: SINGLE_USER))
    assert client.get("/v1/evals/judge-agreement", params={"project_id": str(uuid.uuid4())}).status_code == 404
    assert client.get("/v1/evals/judge-agreement", params={"name": "Not A Name"}).status_code == 422


def test_the_route_measures_a_judge_version_once_enough_items_are_labelled(stores):
    store, records = stores
    judged = [i % 4 != 0 for i in range(MINIMUM_LABELLED)]
    labelled = [i % 5 != 0 for i in range(MINIMUM_LABELLED)]
    asyncio.run(seed(store, records, MINIMUM_LABELLED, judged=judged, labelled=labelled))
    second = report(store, records, name="polite_tone")["items"][1]
    expected = compare(list(zip(judged, labelled, strict=True)))
    assert (second["status"], second["labelled"], second["agreed"]) == ("measured", MINIMUM_LABELLED, expected.agreed)
    assert second["agreement_confidence_95"] == list(expected.agreement_confidence_95)
    assert (second["kappa"], second["false_pass_rate"], second["false_fail_rate"]) == (
        expected.kappa,
        expected.false_pass_rate,
        expected.false_fail_rate,
    )


async def seed_a_review_named_like_the_judge(store, records) -> None:
    """A suite-file run's own judge verdict on one case (no scoring job), and a queue question
    named like that judge, answered: the review and its human score, as the review route writes
    them (MartianSecurity/martian-notes#345)."""
    principal = SINGLE_USER
    await store.create_project(principal, Project(id=DEFAULT_PROJECT, name="Default", created_at=T0))
    run = Run(id=RUN, project_id=DEFAULT_PROJECT, code="EV-1", source="local", state="completed", created_at=T0)
    await records.create_run(principal, run)
    await store.start_run(principal, run)
    await store.record_trace(
        principal, Trace(id=tid(0), project_id=DEFAULT_PROJECT, name="c0", state="ok", request_time=T0, run_id=RUN)
    )
    result = CaseResult(run_id=RUN, case_id="c0", status="passed", store=StoreRef(trace_id=tid(0)))
    judged = Score(run_id=RUN, case_id="c0", trace_id=tid(0), scorer="polite_tone", scorer_version="2",
                   source="llm_judge", source_id="openai:/judge", value=True, passed=True, created_at=T0)  # fmt: skip
    await store.log_score(principal, DEFAULT_PROJECT, tid(0), judged)
    await records.put_results(principal, DEFAULT_PROJECT, [result], scores=[judged])
    at = T0 + timedelta(minutes=5)
    await records.add_review(principal, DEFAULT_PROJECT, Review(id="review-0", run_id=RUN, case_id="c0",
                             criterion="polite_tone", state="complete", value=False, created_at=at))  # fmt: skip
    human = Score(run_id=RUN, case_id="c0", trace_id=tid(0), scorer="polite_tone", source="human", value=False,
                  passed=False, metadata={"martian.review_criterion": "polite_tone"}, created_at=at)  # fmt: skip
    await records.put_scores(principal, DEFAULT_PROJECT, [human])
    await store.log_score(principal, DEFAULT_PROJECT, tid(0), human)


def test_a_review_named_like_a_suite_runs_judge_is_counted_beside_its_verdict(stores):
    # The person's answer sits beside the judge's score in the record store, so the verdict is
    # still there to compare with it.
    store, records = stores
    asyncio.run(seed_a_review_named_like_the_judge(store, records))
    (found,) = report(store, records)["items"]
    assert (found["judge"], found["version"], found["judged"], found["labelled"]) == ("polite_tone", "2", 1, 1)
    assert found["confusion"] == {"both_pass": 0, "false_pass": 1, "false_fail": 0, "both_fail": 0}


def test_a_project_with_nothing_yet_reports_nothing(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    assert report(MemoryStore(), records)["items"] == []
    records.close()
