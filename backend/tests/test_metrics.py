from shapely.geometry import box

from app.nesting.engine import (
    PlacedPart,
    _sheet_polygon,
)
from app.nesting.rotation import LockedRotation
from app.nesting.metrics import (
    ObjectiveWeights,
    analyze_free_space,
    free_space_from_placed_parts,
    score_layout,
)


def _placed(part_id: str, shape) -> PlacedPart:
    return PlacedPart(
        part_id=part_id,
        source_image_path="x",
        placed_shape_mm=shape,
        rotation=LockedRotation.DEG_0,
    )


# ---------------------------------------------------------------------------
# Pocket classification correctness on hand-constructed geometry
# ---------------------------------------------------------------------------


def test_single_untouched_sheet_is_one_boundary_pocket():
    usable = _sheet_polygon(100, 100, 0)
    analysis = analyze_free_space(usable, None)

    assert analysis.pocket_count == 1
    assert analysis.enclosed_pocket_count == 0
    assert analysis.pockets[0].touches_boundary is True
    assert abs(analysis.total_free_area_mm2 - 10000.0) < 1e-6
    assert abs(analysis.fragmentation_index - 0.0) < 1e-9


def test_narrow_channel_gap_between_two_blocks_is_boundary_touching():
    # Two blocks with a 5mm gap between them, both reaching the sheet edge --
    # the gap is a narrow channel but is NOT fully enclosed (it opens to the
    # top and bottom edges of the sheet), so it must be classified as
    # touching the boundary despite being narrow.
    usable = _sheet_polygon(100, 100, 0)
    left_block = box(0, 0, 40, 100)
    right_block = box(45, 0, 100, 100)
    from shapely.ops import unary_union

    occupied = unary_union([left_block, right_block])
    analysis = analyze_free_space(usable, occupied)

    assert analysis.pocket_count == 1
    channel = analysis.pockets[0]
    assert channel.touches_boundary is True
    assert abs(channel.area_mm2 - 500.0) < 1e-6  # 5mm x 100mm
    # A 5mm-wide channel can only ever fit something <=5mm on its short axis.
    assert channel.max_inscribed_diameter_mm <= 5.0 + 1e-6


def test_fully_enclosed_hole_is_detected_as_not_touching_boundary():
    # A ring of four blocks with a hole in the middle that never reaches the
    # sheet boundary -- this must be classified as enclosed.
    usable = _sheet_polygon(100, 100, 0)
    top = box(0, 0, 100, 30)
    bottom = box(0, 70, 100, 100)
    left = box(0, 30, 30, 70)
    right = box(70, 30, 100, 70)
    from shapely.ops import unary_union

    occupied = unary_union([top, bottom, left, right])
    analysis = analyze_free_space(usable, occupied)

    assert analysis.pocket_count == 1
    hole = analysis.pockets[0]
    assert hole.touches_boundary is False
    assert abs(hole.area_mm2 - 1600.0) < 1e-6  # 40mm x 40mm interior
    assert analysis.enclosed_pocket_count == 1


def test_multiple_disconnected_pockets_are_each_reported():
    usable = _sheet_polygon(100, 100, 0)
    # A single block in the middle splits the free area into a left strip
    # and a right strip -- two disconnected pockets.
    middle = box(45, 0, 55, 100)
    analysis = analyze_free_space(usable, middle)

    assert analysis.pocket_count == 2
    areas = sorted(p.area_mm2 for p in analysis.pockets)
    assert abs(areas[0] - 4500.0) < 1e-6
    assert abs(areas[1] - 4500.0) < 1e-6
    # Two equal pockets: the largest is only half of total free area, so
    # fragmentation must reflect that (not 0.0, since it isn't one region).
    assert analysis.fragmentation_index > 0.4


def test_compactness_prefers_round_over_thin_pocket_of_equal_area():
    # Two pockets of identical area (400mm^2) but very different shape:
    # a compact 20x20 square vs a long thin 4x100 sliver.
    square_usable = _sheet_polygon(120, 120, 0)
    # Occupy everything except a 20x20 square in one corner.
    square_free_region = box(0, 0, 20, 20)
    occupied_for_square = square_usable.difference(square_free_region)
    square_analysis = analyze_free_space(square_usable, occupied_for_square)

    sliver_usable = _sheet_polygon(120, 120, 0)
    sliver_free_region = box(0, 0, 4, 100)
    occupied_for_sliver = sliver_usable.difference(sliver_free_region)
    sliver_analysis = analyze_free_space(sliver_usable, occupied_for_sliver)

    assert square_analysis.pocket_count == 1
    assert sliver_analysis.pocket_count == 1
    assert abs(square_analysis.pockets[0].area_mm2 - 400.0) < 1e-6
    assert abs(sliver_analysis.pockets[0].area_mm2 - 400.0) < 1e-6
    # Same area, but the square pocket must score meaningfully more compact
    # than the thin sliver.
    assert square_analysis.pockets[0].compactness > sliver_analysis.pockets[0].compactness


# ---------------------------------------------------------------------------
# score_layout monotonicity under a strictly-better synthetic layout
# ---------------------------------------------------------------------------


def test_score_layout_prefers_more_placed_parts_all_else_similar():
    usable = _sheet_polygon(100, 100, 0)
    usable_area_mm2 = usable.area

    fewer = [_placed("a", box(0, 0, 20, 20))]
    more = [_placed("a", box(0, 0, 20, 20)), _placed("b", box(25, 0, 45, 20))]

    fewer_free = free_space_from_placed_parts(usable, fewer, clearance_mm=1.0)
    more_free = free_space_from_placed_parts(usable, more, clearance_mm=1.0)

    fewer_score = score_layout(fewer, usable_area_mm2, fewer_free)
    more_score = score_layout(more, usable_area_mm2, more_free)

    assert more_score.placed_count == 2
    assert fewer_score.placed_count == 1
    assert more_score.total > fewer_score.total


def test_score_layout_prefers_less_fragmented_remaining_space_at_equal_placed_count():
    # Same placed_count (1 part, same area), but positioned so remaining
    # free space is either one contiguous region or split into two -- a
    # strictly better layout (single big pocket) should score higher.
    usable = _sheet_polygon(100, 40, 0)
    usable_area_mm2 = usable.area

    # Corner placement: leaves one large contiguous L-shaped-ish region.
    corner_layout = [_placed("a", box(0, 0, 20, 40))]
    # Middle placement: splits remaining space into a left and right strip.
    middle_layout = [_placed("a", box(40, 0, 60, 40))]

    corner_free = free_space_from_placed_parts(usable, corner_layout, clearance_mm=1.0)
    middle_free = free_space_from_placed_parts(usable, middle_layout, clearance_mm=1.0)

    corner_score = score_layout(corner_layout, usable_area_mm2, corner_free)
    middle_score = score_layout(middle_layout, usable_area_mm2, middle_free)

    assert corner_score.placed_count == middle_score.placed_count == 1
    assert corner_free.pocket_count == 1
    assert middle_free.pocket_count == 2
    assert corner_score.total > middle_score.total


def test_score_layout_saturated_sheet_has_perfect_compactness_and_no_fragmentation():
    usable = _sheet_polygon(20, 20, 0)
    usable_area_mm2 = usable.area
    full_layout = [_placed("a", box(0, 0, 20, 20))]
    free = free_space_from_placed_parts(usable, full_layout, clearance_mm=0.5)
    score = score_layout(full_layout, usable_area_mm2, free)

    assert free.pocket_count == 0
    assert score.fragmentation_index == 0.0
    assert score.weighted_compactness == 1.0


def test_score_layout_rejects_non_positive_usable_area():
    import pytest
    from app.nesting.metrics import MetricsError

    usable = _sheet_polygon(20, 20, 0)
    free = analyze_free_space(usable, None)
    with pytest.raises(MetricsError):
        score_layout([], 0.0, free)


# ---------------------------------------------------------------------------
# Weight-sensitivity check (spec section 9's explicit requirement)
# ---------------------------------------------------------------------------


def test_weight_sensitivity_fragmentation_penalty_changes_ranking():
    """Confirms the weights are not inert: changing fragmentation_penalty
    can flip which of two equal-placed-count layouts scores higher, proving
    the term actually participates in the total rather than being a
    dead/no-op coefficient.
    """
    usable = _sheet_polygon(100, 40, 0)
    usable_area_mm2 = usable.area

    corner_layout = [_placed("a", box(0, 0, 20, 40))]
    middle_layout = [_placed("a", box(40, 0, 60, 40))]

    corner_free = free_space_from_placed_parts(usable, corner_layout, clearance_mm=1.0)
    middle_free = free_space_from_placed_parts(usable, middle_layout, clearance_mm=1.0)

    zero_frag_weights = ObjectiveWeights(fragmentation_penalty=0.0, compactness_bonus=0.0)
    high_frag_weights = ObjectiveWeights(fragmentation_penalty=500.0, compactness_bonus=0.0)

    corner_zero = score_layout(corner_layout, usable_area_mm2, corner_free, weights=zero_frag_weights)
    middle_zero = score_layout(middle_layout, usable_area_mm2, middle_free, weights=zero_frag_weights)
    corner_high = score_layout(corner_layout, usable_area_mm2, corner_free, weights=high_frag_weights)
    middle_high = score_layout(middle_layout, usable_area_mm2, middle_free, weights=high_frag_weights)

    # With the fragmentation penalty zeroed out, the two equal-placed-count,
    # equal-utilization layouts must score identically (nothing else differs
    # in placed geometry between them).
    assert abs(corner_zero.total - middle_zero.total) < 1e-9
    # With a heavy fragmentation penalty, the split-space layout must now
    # score strictly lower than the single-pocket layout.
    assert corner_high.total > middle_high.total
    # The gap between them must be larger under the high-weight setting.
    gap_zero = corner_zero.total - middle_zero.total
    gap_high = corner_high.total - middle_high.total
    assert gap_high > gap_zero


def test_weight_sensitivity_placed_count_dominance_is_tunable():
    """placed_count is documented as the dominant term by default -- verify
    that reducing its weight relative to the others is at least possible
    (i.e. the weights are genuinely independent knobs, not coupled).
    """
    usable = _sheet_polygon(100, 100, 0)
    usable_area_mm2 = usable.area

    fewer = [_placed("a", box(0, 0, 20, 20))]
    more = [_placed("a", box(0, 0, 20, 20)), _placed("b", box(25, 0, 45, 20))]

    fewer_free = free_space_from_placed_parts(usable, fewer, clearance_mm=1.0)
    more_free = free_space_from_placed_parts(usable, more, clearance_mm=1.0)

    default_fewer = score_layout(fewer, usable_area_mm2, fewer_free)
    default_more = score_layout(more, usable_area_mm2, more_free)
    assert default_more.total > default_fewer.total

    # Both configurations still change placed_count's contribution
    # independently of the other terms, confirming no hidden coupling.
    tiny_placed_weight = ObjectiveWeights(placed_count=0.001)
    tiny_fewer = score_layout(fewer, usable_area_mm2, fewer_free, weights=tiny_placed_weight)
    tiny_more = score_layout(more, usable_area_mm2, more_free, weights=tiny_placed_weight)
    # utilization term now dominates; "more" still has more placed area so
    # it should still win, but by a much smaller margin than under defaults.
    default_margin = default_more.total - default_fewer.total
    tiny_margin = tiny_more.total - tiny_fewer.total
    assert tiny_margin < default_margin
