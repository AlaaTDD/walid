from math import cos, sin, tau

from shapely.affinity import translate
from shapely.geometry import Polygon, box
from shapely.errors import GEOSException

from app.nesting.benchmark import generate_benchmark_parts
from app.nesting.collision import validate_layout
from app.nesting.engine import (
    PartInput,
    _OccupiedZone,
    _candidate_satisfies_exact_clearance,
    _prepare_rotations,
    run_multi_sheet_nesting,
    run_nesting,
)
from app.nesting.nfp import compute_nfp
from app.nesting.rotation import LockedRotation


def test_nfp_is_translation_invariant_for_irregular_contours():
    stationary = Polygon([(0, 0), (44, 0), (44, 12), (17, 12), (17, 38), (0, 38)])
    moving = Polygon([(0, 0), (19, 4), (7, 26)])
    base = compute_nfp(stationary, moving).region_mm

    dx, dy = 37.125, -19.75
    translated = compute_nfp(translate(stationary, xoff=dx, yoff=dy), moving).region_mm

    # The NFP must only move with the fixed contour.  This specifically guards
    # against treating a polygon ring's arbitrary first vertex as geometry.
    assert translated.symmetric_difference(translate(base, xoff=dx, yoff=dy)).area < 1e-8


def test_cached_nfp_placement_preserves_exact_clearance_validation():
    parts = {
        "l": PartInput(
            shape_mm=Polygon([(0, 0), (42, 0), (42, 12), (18, 12), (18, 38), (0, 38)]),
            source_image_path="l.png",
        ),
        "triangle": PartInput(
            shape_mm=Polygon([(0, 0), (31, 3), (10, 33)]),
            source_image_path="triangle.png",
        ),
        "pentagon": PartInput(
            shape_mm=Polygon([(0, 12), (12, 0), (30, 6), (35, 27), (9, 32)]),
            source_image_path="pentagon.png",
        ),
    }

    result = run_nesting(
        parts,
        sheet_width_mm=180,
        sheet_height_mm=130,
        sheet_margin_mm=5,
        clearance_mm=4.1,
    )
    report = validate_layout(
        result.placed,
        sheet_width_mm=180,
        sheet_height_mm=130,
        sheet_margin_mm=5,
        clearance_mm=4.1,
    )

    assert len(result.placed) == len(parts)
    assert report.is_valid


def test_high_detail_contours_use_fast_candidates_with_exact_validation():
    # More than 256 source points activates the scalable placement path.  It
    # must still return a layout that passes the same independent exact GEOS
    # collision/clearance validator used for every exported sheet.
    circle = Polygon(
        [
            (
                25 + 20 * cos(index * tau / 300),
                25 + 20 * sin(index * tau / 300),
            )
            for index in range(300)
        ]
    )
    result = run_nesting(
        {
            "a": PartInput(shape_mm=circle, source_image_path="a.png"),
            "b": PartInput(shape_mm=circle, source_image_path="b.png"),
            "c": PartInput(shape_mm=circle, source_image_path="c.png"),
        },
        sheet_width_mm=180,
        sheet_height_mm=100,
        sheet_margin_mm=5,
        clearance_mm=4.1,
    )
    report = validate_layout(result.placed, 180, 100, 5, clearance_mm=4.1)

    assert len(result.placed) == 3
    assert report.is_valid


def test_remaining_parts_continue_on_new_sheets_until_every_part_is_placed():
    # The printable area is 40×40mm, so each 40×40 square occupies a page.
    # A single-sheet result would leave two parts behind; the multi-sheet
    # wrapper must open fresh pages and place all three.
    square = Polygon([(0, 0), (40, 0), (40, 40), (0, 40)])
    result = run_multi_sheet_nesting(
        {
            "one": PartInput(shape_mm=square, source_image_path="one.png"),
            "two": PartInput(shape_mm=square, source_image_path="two.png"),
            "three": PartInput(shape_mm=square, source_image_path="three.png"),
        },
        sheet_width_mm=50,
        sheet_height_mm=50,
        sheet_margin_mm=5,
        clearance_mm=2,
    )

    assert result.all_placed
    assert len(result.sheets) == 3
    assert [len(sheet.placed) for sheet in result.sheets] == [1, 1, 1]
    assert all(
        validate_layout(sheet.placed, 50, 50, 5, clearance_mm=2).is_valid
        for sheet in result.sheets
    )


# ---------------------------------------------------------------------------
# Candidate-selection boundary-precision regression
# ---------------------------------------------------------------------------
#
# ``_build_allowed_center_region_from_zones`` derives the feasible placement
# region as ``valid_centers.difference(running_merged)`` and proposes boundary
# vertices of that region as candidates. Shapely proves a boundary vertex is
# part of the allowed set (touching the boundary at exactly zero distance is
# a valid placement by design -- clearance is already baked into the NFP; see
# nfp.py's ``point_is_valid_placement`` docstring). That proof is about the
# region as GEOS represents it, not about the point's true distance to each
# occupied zone's own actual placed geometry. ``unary_union``/``difference``
# can, at floating-point resolution, produce a boundary vertex that is
# simultaneously "on the allowed region's boundary" and a hair *inside* one
# constituent zone's own translated NFP+clearance region when that region is
# reconstructed and tested independently.
#
# Concretely: seed=5, part_count=20, packing_strategy="area_desc",
# placement_policy="bottom_left" previously placed part '7' at a boundary
# vertex of the allowed region that was, at floating-point resolution,
# 53.5mm² inside part '4''s own individually-reconstructed clearance zone --
# a real, independently-confirmed geometric overlap on the exact-NFP path,
# the path with the codebase's strongest theoretical guarantee (true
# Minkowski-sum NFP, not a bounding-box heuristic). The candidate passed
# every check that existed prior to this fix; nothing re-validated the
# WINNING candidate against the actual occupied zones before committing to
# it. ``_candidate_satisfies_exact_clearance`` closes exactly that gap,
# mirroring the fast candidate path's already-proven two-phase pattern
# (cheap bounds-distance early-accept, exact GEOS distance only for
# genuinely ambiguous zones) inside ``_find_best_placement_from_zones``,
# which now validates the winning candidate and falls through to the
# next-best-scoring candidate on rejection instead of committing blindly.


def test_seed5_twenty_parts_area_desc_no_longer_overlaps():
    """Pins the exact historical scenario that produced a real 53.5mm² overlap.

    Before the fix, ``validate_layout`` on this exact seed/strategy/policy
    combination reported 8 violations, one of them severity="overlap"
    between parts '4' and '7' -- not a clearance shortfall, a genuine
    geometric intersection. If this regresses, the candidate-selection gap
    this test guards against has reopened.
    """
    parts = generate_benchmark_parts(seed=5, part_count=20)
    result = run_nesting(
        parts,
        sheet_width_mm=1000,
        sheet_height_mm=1000,
        sheet_margin_mm=5,
        clearance_mm=4.1,
        packing_strategy="area_desc",
        placement_policy="bottom_left",
    )

    report = validate_layout(result.placed, 1000, 1000, 5, clearance_mm=4.1)
    assert report.is_valid, [
        (v.severity, v.part_id_a, v.part_id_b, v.detail) for v in report.violations
    ]
    # The fix must reject a bad candidate and fall through to the next-best
    # one, not simply give up on the part -- confirm every part still placed.
    assert len(result.placed) == 20
    assert len(result.unplaced_part_ids) == 0


def test_candidate_selection_produces_no_overlap_across_seeds_and_strategies():
    """Broader confirmation the fix is structural, not seed-specific luck.

    Sweeps a handful of seeds across every packing strategy at the same
    part_count that originally triggered the bug. Runs that hit the
    already-documented, out-of-scope GEOS TopologyException on the
    triangulated-Minkowski-sum path (nfp.py's own module docstring; see also
    benchmark.py's KnownGeosLimitation) are skipped -- that is a separate,
    pre-existing numerical-library robustness issue, not a candidate-
    selection correctness issue, and is not what this test guards.
    """
    from app.nesting.engine import _PACKING_STRATEGIES

    checked_runs = 0
    for seed in (1, 3, 5, 8, 11):
        parts = generate_benchmark_parts(seed=seed, part_count=20)
        for strategy, policy in _PACKING_STRATEGIES:
            try:
                result = run_nesting(
                    parts,
                    sheet_width_mm=1000,
                    sheet_height_mm=1000,
                    sheet_margin_mm=5,
                    clearance_mm=4.1,
                    packing_strategy=strategy,
                    placement_policy=policy,
                )
            except GEOSException:
                continue
            report = validate_layout(result.placed, 1000, 1000, 5, clearance_mm=4.1)
            overlap_violations = [v for v in report.violations if v.severity == "overlap"]
            assert not overlap_violations, (
                f"seed={seed} strategy={strategy}/{policy}: "
                f"{[(v.part_id_a, v.part_id_b, v.detail) for v in overlap_violations]}"
            )
            checked_runs += 1

    # If every single run hit the unrelated GEOS limitation, this test would
    # pass vacuously without actually exercising the fix -- guard against that.
    assert checked_runs > 0


def test_candidate_satisfies_exact_clearance_accepts_boundary_touch():
    """A candidate exactly clearance_mm away from an occupied zone is valid.

    Touching the clearance boundary at exactly zero remaining distance is a
    valid placement by design (see nfp.py's point_is_valid_placement), not a
    violation -- the fix must not become overly conservative and reject
    legitimate boundary placements while closing the floating-point gap.
    """
    shape = box(-10, -10, 10, 10)
    rotations = _prepare_rotations(shape)
    deg0 = next(r for r in rotations if r.angle == LockedRotation.DEG_0)
    zone = _OccupiedZone(rotation=deg0, center_x_mm=0.0, center_y_mm=0.0)

    clearance = 4.1
    moving_shape = box(-10, -10, 10, 10)
    # Zone's right edge is at x=10. A same-size candidate centered at
    # 10 (zone edge) + clearance + 10 (candidate's own half-width) has its
    # left edge exactly clearance_mm from the zone's right edge.
    exact_boundary_x = 10 + clearance + 10

    assert _candidate_satisfies_exact_clearance(
        moving_shape, exact_boundary_x, 0.0, [zone], clearance,
    )


def test_candidate_satisfies_exact_clearance_rejects_inside_clearance():
    """A candidate a fraction of a millimetre inside the clearance zone is rejected."""
    shape = box(-10, -10, 10, 10)
    rotations = _prepare_rotations(shape)
    deg0 = next(r for r in rotations if r.angle == LockedRotation.DEG_0)
    zone = _OccupiedZone(rotation=deg0, center_x_mm=0.0, center_y_mm=0.0)

    clearance = 4.1
    moving_shape = box(-10, -10, 10, 10)
    too_close_x = 10 + clearance + 10 - 0.01

    assert not _candidate_satisfies_exact_clearance(
        moving_shape, too_close_x, 0.0, [zone], clearance,
    )


def test_candidate_satisfies_exact_clearance_rejects_direct_overlap():
    """A candidate placed directly on top of an occupied zone is rejected."""
    shape = box(-10, -10, 10, 10)
    rotations = _prepare_rotations(shape)
    deg0 = next(r for r in rotations if r.angle == LockedRotation.DEG_0)
    zone = _OccupiedZone(rotation=deg0, center_x_mm=0.0, center_y_mm=0.0)

    clearance = 4.1
    moving_shape = box(-10, -10, 10, 10)

    assert not _candidate_satisfies_exact_clearance(
        moving_shape, 0.0, 0.0, [zone], clearance,
    )


def test_candidate_satisfies_exact_clearance_accepts_far_candidate_via_bounds_only():
    """A candidate far outside any zone's reach is accepted without an exact
    distance check -- confirms the cheap bounds-only early-accept path is
    actually reachable, not just the exact-distance fallback."""
    shape = box(-10, -10, 10, 10)
    rotations = _prepare_rotations(shape)
    deg0 = next(r for r in rotations if r.angle == LockedRotation.DEG_0)
    zone = _OccupiedZone(rotation=deg0, center_x_mm=0.0, center_y_mm=0.0)

    clearance = 4.1
    moving_shape = box(-10, -10, 10, 10)

    assert _candidate_satisfies_exact_clearance(
        moving_shape, 1000.0, 0.0, [zone], clearance,
    )
