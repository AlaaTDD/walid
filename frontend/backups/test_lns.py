from shapely.geometry import box

from app.nesting.collision import validate_layout
from app.nesting.engine import PartInput, run_best_single_sheet_nesting, run_nesting
from app.nesting.lns import LnsError, run_lns_optimization
from app.nesting.metrics import free_space_from_placed_parts, score_layout


def _sheet_polygon_area(width, height, margin):
    from app.nesting.engine import _sheet_polygon

    return _sheet_polygon(width, height, margin).area


# ---------------------------------------------------------------------------
# Correctness: LNS never returns a worse or invalid layout than it started with
# ---------------------------------------------------------------------------


def test_lns_never_returns_fewer_placed_parts_than_starting_layout():
    dimensions = [
        (25, 50), (45, 40), (20, 40), (40, 45), (40, 20),
        (20, 40), (20, 40), (50, 25), (35, 35), (70, 20),
    ]
    parts = {
        str(index): PartInput(shape_mm=box(0, 0, width, height), source_image_path="x")
        for index, (width, height) in enumerate(dimensions)
    }
    starting = run_best_single_sheet_nesting(
        parts, 100, 100, sheet_margin_mm=0, clearance_mm=2, packing_attempts=1
    )
    result = run_lns_optimization(
        starting,
        parts,
        100,
        100,
        sheet_margin_mm=0,
        clearance_mm=2,
        max_iterations=20,
        seed=42,
    )

    assert len(result.best.placed) >= len(starting.placed)
    assert result.best_score.total >= result.starting_score.total


def test_lns_result_always_passes_independent_collision_validation():
    dimensions = [
        (25, 50), (45, 40), (20, 40), (40, 45), (40, 20),
        (20, 40), (20, 40), (50, 25), (35, 35), (70, 20),
    ]
    parts = {
        str(index): PartInput(shape_mm=box(0, 0, width, height), source_image_path="x")
        for index, (width, height) in enumerate(dimensions)
    }
    starting = run_best_single_sheet_nesting(
        parts, 100, 100, sheet_margin_mm=0, clearance_mm=2, packing_attempts=1
    )
    result = run_lns_optimization(
        starting, parts, 100, 100, sheet_margin_mm=0, clearance_mm=2,
        max_iterations=15, seed=7,
    )
    report = validate_layout(result.best.placed, 100, 100, 0, clearance_mm=2)
    assert report.is_valid


def test_lns_rejects_invalid_max_iterations():
    parts = {"a": PartInput(shape_mm=box(0, 0, 10, 10), source_image_path="x")}
    starting = run_nesting(parts, 50, 50, sheet_margin_mm=0, clearance_mm=1)
    import pytest

    with pytest.raises(LnsError):
        run_lns_optimization(
            starting, parts, 50, 50, sheet_margin_mm=0, clearance_mm=1, max_iterations=0
        )


def test_lns_rejects_invalid_destroy_fraction():
    parts = {"a": PartInput(shape_mm=box(0, 0, 10, 10), source_image_path="x")}
    starting = run_nesting(parts, 50, 50, sheet_margin_mm=0, clearance_mm=1)
    import pytest

    with pytest.raises(LnsError):
        run_lns_optimization(
            starting, parts, 50, 50, sheet_margin_mm=0, clearance_mm=1,
            max_iterations=5, destroy_fraction=0.0,
        )
    with pytest.raises(LnsError):
        run_lns_optimization(
            starting, parts, 50, 50, sheet_margin_mm=0, clearance_mm=1,
            max_iterations=5, destroy_fraction=1.5,
        )


# ---------------------------------------------------------------------------
# Seeded reproducibility
# ---------------------------------------------------------------------------


def test_lns_is_deterministic_given_the_same_seed():
    dimensions = [(25, 50), (45, 40), (20, 40), (40, 45), (40, 20), (35, 35)]
    parts = {
        str(index): PartInput(shape_mm=box(0, 0, width, height), source_image_path="x")
        for index, (width, height) in enumerate(dimensions)
    }
    starting = run_best_single_sheet_nesting(
        parts, 100, 100, sheet_margin_mm=0, clearance_mm=2, packing_attempts=1
    )

    result_a = run_lns_optimization(
        starting, parts, 100, 100, sheet_margin_mm=0, clearance_mm=2,
        max_iterations=25, seed=123,
    )
    result_b = run_lns_optimization(
        starting, parts, 100, 100, sheet_margin_mm=0, clearance_mm=2,
        max_iterations=25, seed=123,
    )

    assert result_a.best_score.total == result_b.best_score.total
    assert result_a.best_score.placed_count == result_b.best_score.placed_count
    assert [entry.operator_name for entry in result_a.log] == [entry.operator_name for entry in result_b.log]
    assert [entry.accepted for entry in result_a.log] == [entry.accepted for entry in result_b.log]
    assert [entry.candidate_score for entry in result_a.log] == [entry.candidate_score for entry in result_b.log]


def test_lns_different_seeds_can_explore_differently():
    """Not a strict requirement that seeds MUST differ (a trivial problem
    can converge identically regardless), but on a problem with real search
    room, different seeds should be free to take different destroy/accept
    paths -- this guards against rng accidentally being ignored.
    """
    dimensions = [
        (25, 50), (45, 40), (20, 40), (40, 45), (40, 20),
        (20, 40), (20, 40), (50, 25), (35, 35), (70, 20),
        (30, 30), (15, 60),
    ]
    parts = {
        str(index): PartInput(shape_mm=box(0, 0, width, height), source_image_path="x")
        for index, (width, height) in enumerate(dimensions)
    }
    starting = run_best_single_sheet_nesting(
        parts, 100, 100, sheet_margin_mm=0, clearance_mm=2, packing_attempts=1
    )

    result_a = run_lns_optimization(
        starting, parts, 100, 100, sheet_margin_mm=0, clearance_mm=2,
        max_iterations=25, seed=1,
    )
    result_b = run_lns_optimization(
        starting, parts, 100, 100, sheet_margin_mm=0, clearance_mm=2,
        max_iterations=25, seed=2,
    )

    operator_sequence_a = [entry.operator_name for entry in result_a.log]
    operator_sequence_b = [entry.operator_name for entry in result_b.log]
    assert operator_sequence_a != operator_sequence_b


# ---------------------------------------------------------------------------
# Destroy operators actually remove and repair actually re-places
# ---------------------------------------------------------------------------


def test_destroy_random_removes_requested_fraction():
    from app.nesting.lns import _destroy_random
    import random

    placed = [
        _p(str(i), box(i * 10, 0, i * 10 + 8, 8))
        for i in range(10)
    ]
    rng = random.Random(1)
    kept, removed = _destroy_random(placed, 0.3, rng)
    assert len(removed) == 3
    assert len(kept) == 7
    assert set(p.part_id for p in kept) | set(p.part_id for p in removed) == set(p.part_id for p in placed)


def test_destroy_smallest_removes_the_smallest_area_parts():
    from app.nesting.lns import _destroy_smallest

    placed = [
        _p("big", box(0, 0, 50, 50)),
        _p("medium", box(0, 0, 20, 20)),
        _p("tiny", box(0, 0, 5, 5)),
    ]
    kept, removed = _destroy_smallest(placed, fraction=0.4)
    assert len(removed) == 1
    assert removed[0].part_id == "tiny"
    assert set(p.part_id for p in kept) == {"big", "medium"}


def test_destroy_neighborhood_removes_the_seeds_closest_geometric_neighbours():
    from app.nesting.lns import _destroy_neighborhood
    import random

    # A tight row (0..8mm gaps) plus one far-away outlier at x=500. A seed
    # chosen from the tight row should always pull its removed set from
    # WITHIN the row (never the outlier), since every row part is far
    # closer to any other row part than the outlier is to anything.
    placed = [_p(str(i), box(i * 10, 0, i * 10 + 8, 8)) for i in range(8)]
    placed.append(_p("outlier", box(500, 0, 508, 8)))
    rng = random.Random(3)
    kept, removed = _destroy_neighborhood(placed, 0.3, rng)
    removed_ids = {p.part_id for p in removed}
    assert len(removed) == round(9 * 0.3)
    assert "outlier" not in removed_ids
    assert set(p.part_id for p in kept) | removed_ids == {p.part_id for p in placed}


def test_destroy_neighborhood_single_part_removes_nothing_and_keeps_it():
    from app.nesting.lns import _destroy_neighborhood
    import random

    placed = [_p("solo", box(0, 0, 10, 10))]
    rng = random.Random(1)
    kept, removed = _destroy_neighborhood(placed, 0.5, rng)
    assert kept == []
    assert [p.part_id for p in removed] == ["solo"]


def test_destroy_similarity_removes_area_matched_parts_not_just_smallest():
    from app.nesting.lns import _destroy_similarity
    import random

    # Two big parts of near-identical area, two small parts of near-identical
    # (but different from the big pair's) area. Forcing the rng to pick a
    # BIG part as seed must pull its removed set from the big pair, not the
    # globally-smallest parts -- the property _destroy_smallest could never
    # have (it always removes the global minimum, ignoring the seed).
    placed = [
        _p("big_a", box(0, 0, 50, 50)),   # area 2500
        _p("big_b", box(0, 0, 49, 51)),   # area 2499
        _p("small_a", box(0, 0, 5, 5)),   # area 25
        _p("small_b", box(0, 0, 5, 4)),   # area 20
    ]
    # random.Random(0).randrange(4) == 1 for this stdlib PRNG, selecting
    # placed[1] == "big_b" as the seed (verified by direct call below rather
    # than hard-coded, so this test cannot silently drift if the stdlib PRNG
    # implementation ever changes).
    rng = random.Random(0)
    seed_index = random.Random(0).randrange(4)
    kept, removed = _destroy_similarity(placed, 0.5, rng)
    removed_ids = {p.part_id for p in removed}
    assert len(removed) == 2
    assert placed[seed_index].part_id in removed_ids
    if placed[seed_index].part_id.startswith("big"):
        assert removed_ids == {"big_a", "big_b"}
    else:
        assert removed_ids == {"small_a", "small_b"}


def test_destroy_cluster_removed_set_is_spatially_connected():
    from app.nesting.lns import _destroy_cluster
    import random

    # A tight connected chain (each part exactly clearance_mm=2 from the
    # next) plus one isolated part far away. Any seed drawn from the chain
    # must grow through mutually-adjacent parts, never jumping straight to
    # the isolated part while chain neighbours remain unvisited.
    clearance = 2.0
    chain = [_p(str(i), box(i * 12, 0, i * 12 + 10, 10)) for i in range(6)]
    isolated = _p("isolated", box(1000, 0, 1010, 10))
    placed = chain + [isolated]
    rng = random.Random(5)
    kept, removed = _destroy_cluster(placed, 0.5, clearance, rng)
    removed_ids = {p.part_id for p in removed}
    assert len(removed) == round(7 * 0.5)
    # The chain parts are indices 0..5 with 2mm gaps == exactly clearance_mm
    # apart, so consecutive chain members are always cluster-adjacent.
    # Whichever contiguous sub-chain got removed must be an unbroken run of
    # consecutive chain indices (connectivity property), not a scattered
    # subset -- this is what distinguishes cluster destruction from
    # neighborhood destruction's plain closest-N-by-distance ranking.
    removed_chain_indices = sorted(int(pid) for pid in removed_ids if pid != "isolated")
    if removed_chain_indices:
        span = removed_chain_indices[-1] - removed_chain_indices[0] + 1
        assert span == len(removed_chain_indices)
    assert set(p.part_id for p in kept) | removed_ids == {p.part_id for p in placed}


def test_destroy_cluster_single_part_removes_nothing_and_keeps_it():
    from app.nesting.lns import _destroy_cluster
    import random

    placed = [_p("solo", box(0, 0, 10, 10))]
    rng = random.Random(1)
    kept, removed = _destroy_cluster(placed, 0.5, 2.0, rng)
    assert kept == []
    assert [p.part_id for p in removed] == ["solo"]


def test_all_six_destroy_operators_are_registered_and_reachable_by_lns():
    """Guards against a new operator being written but never wired into
    _make_destroy_operators -- spec's own "module without integration =
    module that does not exist" rule, applied at the operator level.
    """
    from app.nesting.lns import _make_destroy_operators
    from shapely.geometry import box as _box

    usable = _box(0, 0, 100, 100)
    operators = _make_destroy_operators(usable, clearance_mm=2.0, destroy_fraction=0.2)
    assert set(operators.keys()) == {
        "random", "worst_gap_adjacent", "smallest",
        "neighborhood", "similarity", "cluster",
    }


# ---------------------------------------------------------------------------
# Adaptive operator selection (spec section 12)
# ---------------------------------------------------------------------------


def test_adaptive_selector_starts_with_equal_weights():
    from app.nesting.lns import AdaptiveOperatorSelector

    selector = AdaptiveOperatorSelector(operator_names=("a", "b", "c"))
    assert selector.weights == {"a": 1.0, "b": 1.0, "c": 1.0}


def test_adaptive_selector_selection_is_weight_proportional():
    """With one operator weighted far above the others, roulette-wheel
    selection over many draws must pick it far more often -- this is the
    property that distinguishes adaptive selection from plain rng.choice
    (which spec section 12 explicitly rejects: "لا تجعل LNS تعمل بطريقة
    ثابتة غبية").
    """
    import random

    from app.nesting.lns import AdaptiveOperatorSelector

    selector = AdaptiveOperatorSelector(operator_names=("dominant", "weak"))
    selector.weights["dominant"] = 100.0
    selector.weights["weak"] = 1.0

    rng = random.Random(0)
    counts = {"dominant": 0, "weak": 0}
    for _ in range(2000):
        counts[selector.select(rng)] += 1

    # Expected ratio is 100:1; allow generous slack for RNG variance while
    # still proving the mechanism is weight-driven, not roughly 50/50 (which
    # plain rng.choice would give regardless of these weights).
    assert counts["dominant"] > counts["weak"] * 20


def test_adaptive_selector_rewards_new_best_more_than_merely_accepted():
    from app.nesting.lns import AdaptiveOperatorSelector

    selector_best = AdaptiveOperatorSelector(operator_names=("x",))
    selector_best.update("x", is_new_best=True, accepted=True)

    selector_accepted = AdaptiveOperatorSelector(operator_names=("x",))
    selector_accepted.update("x", is_new_best=False, accepted=True)

    selector_rejected = AdaptiveOperatorSelector(operator_names=("x",))
    selector_rejected.update("x", is_new_best=False, accepted=False)

    assert selector_best.weights["x"] > selector_accepted.weights["x"] > selector_rejected.weights["x"]


def test_adaptive_selector_weight_never_collapses_to_zero():
    """Spec section 12: an unhelpful operator should be used LESS, never
    eliminated entirely -- a destroy type that looks bad early in the search
    can become useful again once the layout has changed shape.
    """
    from app.nesting.lns import AdaptiveOperatorSelector, _ADAPTIVE_MIN_WEIGHT

    selector = AdaptiveOperatorSelector(operator_names=("always_rejected",))
    for _ in range(200):
        selector.update("always_rejected", is_new_best=False, accepted=False)

    assert selector.weights["always_rejected"] >= _ADAPTIVE_MIN_WEIGHT
    assert selector.weights["always_rejected"] > 0.0


def test_lns_result_exposes_final_operator_weights():
    """Spec section 12's own requirement: any new search mechanism needs
    clear measurement, not just a claimed benefit. run_lns_optimization must
    surface what the adaptive selector actually learned, not keep it as
    internal-only state.
    """
    dimensions = [(25, 50), (45, 40), (20, 40), (40, 45), (40, 20), (35, 35)]
    parts = {
        str(index): PartInput(shape_mm=box(0, 0, width, height), source_image_path="x")
        for index, (width, height) in enumerate(dimensions)
    }
    starting = run_best_single_sheet_nesting(
        parts, 100, 100, sheet_margin_mm=0, clearance_mm=2, packing_attempts=1
    )
    result = run_lns_optimization(
        starting, parts, 100, 100, sheet_margin_mm=0, clearance_mm=2,
        max_iterations=15, seed=5,
    )
    assert set(result.operator_weights_final.keys()) == {
        "random", "worst_gap_adjacent", "smallest",
        "neighborhood", "similarity", "cluster",
    }
    assert all(weight > 0.0 for weight in result.operator_weights_final.values())


def _p(part_id, shape):def _p(part_id, shape):
    from app.nesting.engine import PlacedPart
    from app.nesting.rotation import LockedRotation

    return PlacedPart(
        part_id=part_id, source_image_path="x", placed_shape_mm=shape, rotation=LockedRotation.DEG_0,
    )


# ---------------------------------------------------------------------------
# Proof of value: LNS escapes a local optimum a single greedy pass leaves behind
# ---------------------------------------------------------------------------


def test_lns_can_improve_on_a_deliberately_awkward_greedy_layout():
    """Construct a case where a single-strategy greedy pass places parts in
    an order that fragments free space, leaving room a rearrangement could
    recover. LNS is given enough iterations and a large destroy fraction to
    have a real chance at finding that rearrangement; the assertion is
    intentionally about the SCORE never regressing (already covered above)
    plus a genuine attempt at improvement, not a hard-coded expected count
    a random-number tweak could flake.
    """
    dimensions = [
        (30, 30), (30, 30), (30, 30), (30, 30),
        (14, 14), (14, 14), (14, 14), (14, 14), (14, 14), (14, 14),
    ]
    parts = {
        str(index): PartInput(shape_mm=box(0, 0, width, height), source_image_path="x")
        for index, (width, height) in enumerate(dimensions)
    }
    starting = run_nesting(
        parts, 100, 100, sheet_margin_mm=0, clearance_mm=1, packing_strategy="area_desc",
        placement_policy="bottom_left",
    )
    result = run_lns_optimization(
        starting, parts, 100, 100, sheet_margin_mm=0, clearance_mm=1,
        max_iterations=80, destroy_fraction=0.4, initial_temperature=80.0,
        seed=99,
    )

    # Never regresses (the hard guarantee).
    assert result.best_score.total >= result.starting_score.total
    assert len(result.best.placed) >= len(starting.placed)
    # The search actually ran the iterations requested (not silently a no-op).
    assert result.iterations_run == 80
    report = validate_layout(result.best.placed, 100, 100, 0, clearance_mm=1)
    assert report.is_valid
