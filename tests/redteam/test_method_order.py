"""Attack methods ordered by their recorded success (martian-notes#318): best first on this target, at
most ``methods_per_objective`` of them, a share kept for exploration, and the order reproducible from
a seed.

The refund desk refuses every fixed template and gives way to refinement, so after one run the record
has ``templates`` resisted and ``refine`` confirmed."""

import random

import pytest

from martian.redteam.engine import Engine
from martian.redteam.engine.memory import StrategyLibrary
from tests.redteam.engine_helpers import run_file
from tests.redteam.test_search import GOAL, Learner, RefundDesk

LISTED = [{"name": "Templates"}, {"name": "Refine"}]


def ordered_file(folder, attacks=LISTED, **memory):
    return run_file(
        attacks=attacks,
        vulnerabilities=[GOAL],
        memory={"order": True, "folder": str(folder), "explore": 0, "seed": 7, **memory},
    )


def methods(engine):
    return [planned.attack_method.id for planned in engine.planned]


async def test_a_second_run_puts_the_method_that_succeeded_first_and_the_cap_leaves_the_rest_out(tmp_path):
    folder = tmp_path / "memory"
    first = Engine(ordered_file(folder), service=Learner(), target=RefundDesk())
    assert methods(first) == ["templates", "refine"]  # nothing recorded: the listed order
    run = await first.run()
    assert [attempt.outcome for attempt in run.attempts] == ["resisted", "hit"]
    rows = {key[1]: (row.attempts, row.successes) for key, row in StrategyLibrary(folder).rows.items()}
    assert rows == {"templates": (1, 0), "refine": (1, 1)}

    second = Engine(ordered_file(folder), service=Learner(), target=RefundDesk())
    assert methods(second) == ["refine", "templates"]
    capped = Engine(ordered_file(folder, methods_per_objective=1), service=Learner(), target=RefundDesk())
    assert methods(capped) == ["refine"]
    run = await capped.run()
    assert run.expected_attempts == 1 and [probe.attack_method for probe in run.probes] == ["refine"]
    [(objective, left_out)] = run.provenance["memory"]["left_out"].items()
    assert left_out == ["templates"] and run.provenance["memory"]["order"][objective] == ["refine"]


async def test_another_target_has_a_record_of_its_own(tmp_path):
    folder = tmp_path / "memory"
    await Engine(ordered_file(folder), service=Learner(), target=RefundDesk()).run()
    other = ordered_file(folder)
    other = other.model_copy(update={"target": other.target.model_copy(update={"purpose": "Another desk."})})
    assert methods(Engine(other, service=Learner(), target=RefundDesk())) == ["templates", "refine"]


def library_with_record(folder, target="t-1", objective="o"):
    library = StrategyLibrary(folder)
    for method, attempts, successes in (("a", 4, 0), ("b", 4, 3), ("c", 0, 0), ("d", 4, 1), ("e", 2, 2)):
        for number in range(attempts):
            library.count("method", method, objective, target, success=number < successes)
    library.save()
    return StrategyLibrary(folder)


def test_the_order_is_reproducible_from_a_seed(tmp_path):
    library = library_with_record(tmp_path)
    listed = ["a", "b", "c", "d", "e"]

    def order(seed, explore=0.4, cap=None):
        return library.order("o", "t-1", listed, rng=random.Random(seed), explore=explore, cap=cap)

    # Scores: e 0.75, b 0.67, c 0.5, d 0.33, a 0.17. With no exploration the record decides alone.
    assert order(1, explore=0) == (["e", "b", "c", "d", "a"], [])
    assert order(1, explore=0, cap=2) == (["e", "b"], ["c", "d", "a"])
    for seed in range(20):
        assert order(seed) == order(seed)
        assert order(seed, cap=3) == order(seed, cap=3)
    drawn = {tuple(order(seed)[0]) for seed in range(20)}
    assert len(drawn) > 1  # the seed moves the explored places
    for chosen in drawn:
        assert list(chosen[:3]) == ["e", "b", "c"] and sorted(chosen[3:]) == ["a", "d"]


@pytest.mark.parametrize("cap, explore, kept, explored", [(3, 0.34, 2, 1), (4, 0.5, 2, 2), (2, 0.2, 2, 0)])
def test_a_share_of_the_places_goes_to_methods_the_record_does_not_place(tmp_path, cap, explore, kept, explored):
    library = library_with_record(tmp_path)
    listed = ["a", "b", "c", "d", "e"]
    for seed in range(10):
        chosen, left_out = library.order("o", "t-1", listed, rng=random.Random(seed), explore=explore, cap=cap)
        assert chosen[:kept] == ["e", "b", "c", "d"][:kept] and len(chosen) == cap
        assert set(chosen[kept:]).isdisjoint(["e", "b", "c", "d"][:kept]) and len(chosen[kept:]) == explored
        assert sorted(chosen + left_out) == sorted(listed)


async def test_the_same_seed_gives_the_same_plan_and_a_resumed_run_keeps_its_order(tmp_path):
    folder = tmp_path / "memory"
    attacks = [{"name": "Templates"}, {"name": "Refine"}, {"name": "Mutation"}, {"name": "TreeSearch"}]
    file = ordered_file(folder, attacks=attacks, explore=0.5)
    engine = Engine(file, service=Learner(), target=RefundDesk())
    assert methods(Engine(file, service=Learner(), target=RefundDesk())) == methods(engine)
    run = await engine.run()
    assert methods(Engine(file, service=Learner(), target=RefundDesk())) != methods(engine)  # the record moved
    resumed = Engine(file, service=Learner(), target=RefundDesk(), resume=run)
    assert methods(resumed) == methods(engine) and resumed.reused == {attempt.id for attempt in run.attempts}


def test_methods_per_objective_needs_order():
    with pytest.raises(ValueError, match="set order"):
        run_file(attacks=LISTED, vulnerabilities=[GOAL], memory={"methods_per_objective": 1})
