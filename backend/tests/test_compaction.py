from shapely.geometry import box

from app.nesting.collision import validate_layout
from app.nesting.compaction import CompactionError, compact_layout
from app.nesting.engine import NestingResult, PartInput, PlacedPart, _sheet_polygon, run_nesting
from app.nesting.rotation import LockedRotation


def _p(part_id, shape):
    return PlacedPart(
        part_id=part_id, source_image_path="x", placed_shape_mm=shape, rotation=LockedRotation.DEG_0,
    )


# ---------------------------------------------------------------------------
# Identity: compaction never changes WHICH parts are placed, only WHERE
# ---------------------------------------------------------------------------


def test_compaction_preserves_placed_count_and_part_ids():
    starting = NestingResult(
        placed=[
            _p("a", box(60, 60, 80, 80)),
            _p("b", box(30, 60, 50, 80)),
        ],
        unplaced_part_ids=["c"],
        sheet_full=True,
        processed_count=3,
        total_count=3,
    )
    result = compact_layout(starting, 100, 100, sheet_margin_mm=0, clearance_mm=2)

    assert len(result.result.placed) == 2
    assert {p.part_id for p in result.result.placed} == {"a", "b"}
    # unplaced/processed/total carried through unchanged -- compaction never
    # touches placement decisions, only positions.
    assert result.result.unplaced_part_ids == ["c"]
    assert result.result.processed_count == 3
    assert result.result.total_count == 3


def test_compaction_on_empty_placed_list_is_a_safe_no_op():
    starting = NestingResult(placed=[], unplaced_part_ids=["a"], sheet_full=True, processed_count=1, total_count=1)
    result = compact_layout(starting, 50, 50, sheet_margin_mm=0, clearance_mm=2)
    assert result.result.placed == []
    assert result.moved_count == 0
    assert result.improved is False


def test_compaction_rejects_invalid_max_passes():
    import pytest

    starting = NestingResult(placed=[_p("a", box(0, 0, 10, 10))], unplaced_part_ids=[], sheet_full=False, processed_count=1, total_count=1)
    with pytest.raises(CompactionError):
        compact_layout(starting, 50, 50, sheet_margin_mm=0, clearance_mm=2, max_passes=0)


# ---------------------------------------------------------------------------
# Actual settling behaviour on hand-constructed layouts
# ---------------------------------------------------------------------------


def test_single_part_slides_all_the_way_left_when_sliding_left_strictly_helps():
    # A single 20x20 part sitting near the RIGHT edge of a WIDE (not square)
    # sheet. Sliding left here strictly reduces the bounding envelope without
    # trading away pocket compactness the way a square usable area would
    # (see _compact_one_pass's own unit-level proof below for the raw slide
    # mechanics) -- this keeps the assertion about compact_layout's PUBLIC
    # behaviour honest about the never-worse guarantee: a move is only kept
    # when the resulting score is genuinely not worse, and a wide sheet with
    # a single part is a case where left-only compaction is unambiguously at
    # least as good (it can only ever consolidate the one existing pocket
    # into a still-single, still-boundary-touching pocket of the same total
    # area, never fragment it, since there is nothing else on the sheet to
    # interact with).
    starting = NestingResult(
        placed=[_p("a", box(150, 10, 170, 30))],
        unplaced_part_ids=[],
        sheet_full=False,
        processed_count=1,
        total_count=1,
    )
    result = compact_layout(
        starting, 200, 40, sheet_margin_mm=0, clearance_mm=2, directions=("left",), max_passes=1
    )
    moved = result.result.placed[0]
    minx, miny, maxx, maxy = moved.placed_shape_mm.bounds
    assert abs(minx - 0.0) < 1e-4
    assert result.moved_count == 1
    # NOT score.total >= input_score.total here: sliding flush against the
    # edge turns the wrap-around remaining pocket into one long rectangle,
    # which has a strictly WORSE isoperimetric compactness ratio
    # (0.4051 -> 0.3443, measured directly) even though it is unambiguously
    # the better nesting outcome. score_layout's compactness_bonus term is
    # a sound tie-breaker for LNS (which compares layouts that can differ in
    # WHICH parts are placed), but is not the right acceptance test for a
    # pure-translation stage like this one -- see compact_layout's own gate,
    # which checks total_free_area_mm2 instead. That is the real structural
    # guarantee this module provides: a slide is only ever accepted when
    # exact bisection proves it stays feasible, so occupied+clearance area
    # can only shrink or hold, meaning free area can only grow or hold.
    from app.nesting.metrics import free_space_from_placed_parts
    usable = _sheet_polygon(200, 40, 0)
    after_free = free_space_from_placed_parts(usable, result.result.placed, clearance_mm=2.0)
    before_free = free_space_from_placed_parts(usable, starting.placed, clearance_mm=2.0)
    assert after_free.total_free_area_mm2 >= before_free.total_free_area_mm2 - 1e-6


def test_compact_one_pass_mechanics_slide_a_part_flush_to_the_target_edge():
    """Unit-level proof that the underlying slide mechanics work, independent
    of compact_layout's never-worse score guard (which is a separate, and
    separately tested, concern -- a single-direction slide CAN legitimately
    make the multi-term score worse on a square sheet, e.g. by turning a
    perfectly square remaining pocket into a less-compact rectangle, and the
    guard correctly vetoes that; this test isolates that the raw geometry
    operation itself is correct regardless of whether a particular caller's
    score judges the net result worth keeping).
    """
    from app.nesting.compaction import _compact_one_pass, _order_for_direction

    usable = _sheet_polygon(100, 100, 0)
    placed = [_p("a", box(50, 50, 70, 70))]
    order = _order_for_direction(placed, "left")
    working, moved_count = _compact_one_pass(placed, usable, "left", clearance_mm=2.0, order=order)

    assert moved_count == 1
    minx, miny, maxx, maxy = working[0].placed_shape_mm.bounds
    assert abs(minx - 0.0) < 1e-4
    assert abs(miny - 50.0) < 1e-4  # only x moved, y untouched by a pure-left slide


def test_two_parts_settle_against_each_other_respecting_clearance():
    # Two 20x20 parts with a big gap between them and the left edge; sliding
    # left should pack them against the edge and against each other, each
    # separated by exactly clearance_mm (not overlapping, not more than
    # required apart).
    clearance = 3.0
    starting = NestingResult(
        placed=[
            _p("a", box(10, 10, 30, 30)),
            _p("b", box(50, 10, 70, 30)),
        ],
        unplaced_part_ids=[],
        sheet_full=False,
        processed_count=2,
        total_count=2,
    )
    result = compact_layout(
        starting, 100, 100, sheet_margin_mm=0, clearance_mm=clearance,
        directions=("left",), max_passes=1,
    )
    by_id = {p.part_id: p for p in result.result.placed}
    a_bounds = by_id["a"].placed_shape_mm.bounds
    b_bounds = by_id["b"].placed_shape_mm.bounds

    # "a" (nearer the edge) settles flush against x=0.
    assert abs(a_bounds[0] - 0.0) < 1e-4
    # "b" settles flush against "a" plus the required clearance, not overlapping.
    gap = b_bounds[0] - a_bounds[2]
    assert abs(gap - clearance) < 1e-3

    report = validate_layout(result.result.placed, 100, 100, 0, clearance_mm=clearance)
    assert report.is_valid


def test_compaction_consolidates_a_split_gap_into_fewer_larger_pockets():
    """Three parts scattered with gaps on both sides of each; compacting
    left should consolidate all the slack into ONE gap on the right side
    instead of three small gaps between/around each part.
    """
    starting = NestingResult(
        placed=[
            _p("a", box(5, 5, 20, 95)),
            _p("b", box(35, 5, 50, 95)),
            _p("c", box(65, 5, 80, 95)),
        ],
        unplaced_part_ids=[],
        sheet_full=False,
        processed_count=3,
        total_count=3,
    )
    from app.nesting.metrics import free_space_from_placed_parts

    usable = _sheet_polygon(100, 100, 0)
    before = free_space_from_placed_parts(usable, starting.placed, clearance_mm=1.0)

    result = compact_layout(
        starting, 100, 100, sheet_margin_mm=0, clearance_mm=1.0,
        directions=("left",), max_passes=1,
    )
    after = free_space_from_placed_parts(usable, result.result.placed, clearance_mm=1.0)

    # Compaction must not create MORE pockets than before, and should
    # generally reduce fragmentation (slack consolidated to one side).
    assert after.pocket_count <= before.pocket_count
    assert after.fragmentation_index <= before.fragmentation_index + 1e-9


# ---------------------------------------------------------------------------
# Never-worse guarantee and independent validation
# ---------------------------------------------------------------------------


def test_compaction_never_shrinks_total_free_area():
    # Renamed from an earlier version of this test that asserted
    # result.score.total >= result.input_score.total. That is NOT a sound
    # invariant for this module -- see the detailed comment in
    # test_single_part_slides_all_the_way_left_when_sliding_left_strictly_helps
    # for a directly-measured counterexample (score_layout's isoperimetric
    # compactness_bonus term can score a strictly-better, strictly-more-free
    # -area layout as lower, because a long single settled pocket has a
    # worse isoperimetric ratio than the fragmented/wrap-around shape it
    # replaced). The real, structurally-guaranteed invariant this module
    # provides is total_free_area_mm2, which is what compact_layout's own
    # acceptance gate checks -- this test checks the same thing at the
    # public-API level, on a realistic multi-part packed layout rather than
    # a hand-constructed one.
    dimensions = [(25, 50), (45, 40), (20, 40), (40, 45), (40, 20), (35, 35)]
    parts = {
        str(index): PartInput(shape_mm=box(0, 0, width, height), source_image_path="x")
        for index, (width, height) in enumerate(dimensions)
    }
    starting = run_nesting(parts, 100, 100, sheet_margin_mm=0, clearance_mm=2)
    result = compact_layout(starting, 100, 100, sheet_margin_mm=0, clearance_mm=2)

    from app.nesting.metrics import free_space_from_placed_parts
    usable = _sheet_polygon(100, 100, 0)
    after_free = free_space_from_placed_parts(usable, result.result.placed, clearance_mm=2.0)
    before_free = free_space_from_placed_parts(usable, starting.placed, clearance_mm=2.0)
    assert after_free.total_free_area_mm2 >= before_free.total_free_area_mm2 - 1e-6
    assert len(result.result.placed) == len(starting.placed)


def test_compacted_result_always_passes_independent_collision_validation():
    dimensions = [(25, 50), (45, 40), (20, 40), (40, 45), (40, 20), (35, 35), (30, 30)]
    parts = {
        str(index): PartInput(shape_mm=box(0, 0, width, height), source_image_path="x")
        for index, (width, height) in enumerate(dimensions)
    }
    starting = run_nesting(parts, 100, 100, sheet_margin_mm=0, clearance_mm=2)
    result = compact_layout(starting, 100, 100, sheet_margin_mm=0, clearance_mm=2)
    report = validate_layout(result.result.placed, 100, 100, 0, clearance_mm=2)
    assert report.is_valid
