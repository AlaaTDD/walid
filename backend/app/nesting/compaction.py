"""Compaction: settle an already-decided set of placed parts toward one edge.

Spec's remaining core algorithmic ask alongside LNS (destroy/repair changes
WHICH parts are placed; compaction changes WHERE an already-fixed set of
placed parts sits, without adding or removing any of them). Distinct from
both existing mechanisms in engine.py: _backfill_gaps only ever ADDS
never-placed parts against a fixed occupied set, and never moves anything
already placed; run_lns_optimization's destroy/repair changes membership
(which parts are placed) via removal and re-placement. Compaction here does
neither -- the placed set going in is exactly the placed set coming out,
only translated.

Why this matters on top of LNS/backfill alone: a greedy or LNS-repaired
layout can place every part correctly and even score well, yet still leave
parts sitting with more clearance than strictly required between them and
an edge/each other in one direction, because the search that placed them
never tried sliding a part further once ANY feasible position was found
first on a given axis. Compaction's job is purely to squeeze that slack out
after the fact, consolidating scattered small gaps into fewer, larger,
more boundary-accessible ones that a future job (or another LNS pass) is
more likely to be able to use.

Correctness guarantee: identical to lns.py -- every candidate position is
verified through the same exact GEOS distance/NFP-consistent check used
elsewhere (via _resolve_ambiguous_candidate-equivalent exact distance
checks against an STRtree of the OTHER already-compacted parts, plus a
sheet-bounds check), and the final result is independently re-validated
with validate_layout before being accepted; any failure falls back to the
uncompacted input unchanged.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from shapely.affinity import translate
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from app.nesting.collision import validate_layout
from app.nesting.engine import NestingCancelledError, NestingResult, PlacedPart, _sheet_polygon
from app.nesting.metrics import (
    DEFAULT_OBJECTIVE_WEIGHTS,
    LayoutScore,
    ObjectiveWeights,
    free_space_from_placed_parts,
    score_layout,
)

_SETTLE_STEP_TOLERANCE_MM = 1e-6
_MAX_BISECTION_ITERATIONS = 40


class CompactionError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Compacted layout plus a score for reporting/comparison against other stages.

    ``score``/``input_score`` use the same score_layout as LNS, so a caller can
    compare compaction's output on the same footing as an LNS candidate. They
    are NOT what gates whether compaction accepts its own result, though --
    see the free-area-based acceptance check in compact_layout for why.

    ``moved_count`` reports how many parts actually shifted (beyond a
    floating-point-noise threshold), so a caller/report can distinguish
    "compaction ran and found nothing to squeeze" from "compaction ran and
    meaningfully rearranged the sheet".
    """

    result: NestingResult
    score: LayoutScore
    input_score: LayoutScore
    moved_count: int
    improved: bool


def _direction_vector(direction: str) -> tuple[float, float]:
    if direction == "left":
        return -1.0, 0.0
    if direction == "right":
        return 1.0, 0.0
    if direction == "down":
        return 0.0, -1.0
    if direction == "up":
        return 0.0, 1.0
    raise CompactionError(f"اتجاه انضغاط غير مدعوم: {direction!r}")


def _sheet_travel_distance(usable_area: Polygon, direction: str) -> float:
    minx, miny, maxx, maxy = usable_area.bounds
    if direction in ("left", "right"):
        return maxx - minx
    return maxy - miny


def _max_feasible_slide(
    shape: BaseGeometry,
    dx: float,
    dy: float,
    max_distance: float,
    usable_area: Polygon,
    other_shapes: list[BaseGeometry],
    other_bounds: list[tuple[float, float, float, float]],
    spatial_index: STRtree | None,
    clearance_mm: float,
) -> float:
    """Exact bisection search for the furthest feasible slide along (dx, dy).

    Feasibility at distance ``t`` means: the shape translated by t*(dx, dy)
    stays within usable_area, and remains at least clearance_mm from every
    other shape. Bisection is exact here (not an approximation) because the
    feasible-t region for a fixed direction and a fixed obstacle set is
    provably a single contiguous interval [0, t_max] -- moving further in a
    straight line can only ever approach an obstacle monotonically along
    that line for a convex clearance buffer, so there is exactly one
    boundary to find, which is precisely what bisection finds to floating
    point precision (bounded by _MAX_BISECTION_ITERATIONS, well below the
    _SETTLE_STEP_TOLERANCE_MM target).
    """
    def feasible_at(t: float) -> bool:
        if t <= 0.0:
            return True
        moved = translate(shape, xoff=dx * t, yoff=dy * t)
        if not usable_area.covers(moved):
            return False
        if spatial_index is None:
            return True
        moved_bounds = moved.bounds
        nearby = spatial_index.query(
            box(
                moved_bounds[0] - clearance_mm,
                moved_bounds[1] - clearance_mm,
                moved_bounds[2] + clearance_mm,
                moved_bounds[3] + clearance_mm,
            )
        )
        for raw_index in nearby:
            index = int(raw_index)
            other = other_shapes[index]
            if moved.distance(other) < clearance_mm - _SETTLE_STEP_TOLERANCE_MM:
                return False
        return True

    if not feasible_at(max_distance):
        # Bisect only when the endpoint itself is infeasible; the whole
        # range is feasible in the common case (nothing in the way at all),
        # which this short-circuits without paying for iterations.
        low, high = 0.0, max_distance
        for _ in range(_MAX_BISECTION_ITERATIONS):
            mid = (low + high) / 2.0
            if feasible_at(mid):
                low = mid
            else:
                high = mid
            if high - low < _SETTLE_STEP_TOLERANCE_MM:
                break
        return low
    return max_distance


def _compact_one_pass(
    placed: list[PlacedPart],
    usable_area: Polygon,
    direction: str,
    clearance_mm: float,
    order: list[int],
) -> tuple[list[PlacedPart], int]:
    """One settle pass: try to slide every part as far as possible in ``direction``.

    Parts are processed in ``order`` (an index permutation into ``placed``)
    and each already-moved part immediately becomes an obstacle for the
    next -- this is what lets a pass consolidate a chain of parts against
    each other, not just against the sheet edge, in a single sweep.
    """
    dx, dy = _direction_vector(direction)
    max_travel = _sheet_travel_distance(usable_area, direction)
    working = list(placed)
    moved_count = 0

    for index in order:
        part = working[index]
        other_parts = [working[i] for i in range(len(working)) if i != index]
        other_shapes = [p.placed_shape_mm for p in other_parts]
        other_bounds = [tuple(s.bounds) for s in other_shapes]
        spatial_index = STRtree(other_shapes) if other_shapes else None

        slide = _max_feasible_slide(
            part.placed_shape_mm,
            dx,
            dy,
            max_travel,
            usable_area,
            other_shapes,
            other_bounds,
            spatial_index,
            clearance_mm,
        )
        if slide > _SETTLE_STEP_TOLERANCE_MM:
            new_shape = translate(part.placed_shape_mm, xoff=dx * slide, yoff=dy * slide)
            working[index] = PlacedPart(
                part_id=part.part_id,
                source_image_path=part.source_image_path,
                placed_shape_mm=new_shape,
                rotation=part.rotation,
                source_centroid_px=part.source_centroid_px,
                alpha_bbox_px=part.alpha_bbox_px,
            )
            moved_count += 1

    return working, moved_count


def _order_for_direction(placed: list[PlacedPart], direction: str) -> list[int]:
    """Process parts nearest the target edge first, so they settle before
    parts further away try to slide into the room they free up.

    Sliding left: parts with the smallest current minx settle first (they
    have the shortest path to the edge and cannot be blocked by anything
    processed after them in this same pass). This mirrors the natural
    physical order objects would settle in if slid simultaneously.
    """
    dx, dy = _direction_vector(direction)
    indices = list(range(len(placed)))

    def key(index: int) -> float:
        minx, miny, maxx, maxy = placed[index].placed_shape_mm.bounds
        if dx < 0:
            return minx
        if dx > 0:
            return -maxx
        if dy < 0:
            return miny
        return -maxy

    return sorted(indices, key=key)


def compact_layout(
    starting_result: NestingResult,
    sheet_width_mm: float,
    sheet_height_mm: float,
    *,
    sheet_margin_mm: float = 5.0,
    clearance_mm: float,
    directions: tuple[str, ...] = ("left", "down", "right", "up"),
    max_passes: int = 3,
    objective_weights: ObjectiveWeights = DEFAULT_OBJECTIVE_WEIGHTS,
    check_cancelled: Callable[[], bool] | None = None,
) -> CompactionResult:
    """Settle every placed part as far as possible along each direction in turn.

    ``directions`` is tried in order, each a full settle pass over every
    part; repeating the full cycle up to ``max_passes`` times lets a part
    that only had room to move after an EARLIER part in the cycle moved out
    of its way get a second chance, without looping unboundedly (a settle
    pass in one direction can only ever move parts toward that edge, so the
    total possible movement across the whole sheet is finite -- max_passes
    is a safety bound on convergence speed, not a correctness requirement,
    the loop also stops early once a full cycle moves nothing).

    The placed COUNT and WHICH parts are placed never changes -- this
    function only ever translates parts already in ``starting_result.placed``.
    unplaced_part_ids, processed_count and total_count are carried through
    unchanged from the input.
    """
    if max_passes < 1:
        raise CompactionError("max_passes يجب أن يكون أكبر من صفر.")
    if not starting_result.placed:
        free_space = free_space_from_placed_parts(
            _sheet_polygon(sheet_width_mm, sheet_height_mm, sheet_margin_mm), [], clearance_mm=clearance_mm
        )
        score = score_layout(
            [],
            _sheet_polygon(sheet_width_mm, sheet_height_mm, sheet_margin_mm).area,
            free_space,
            weights=objective_weights,
        )
        return CompactionResult(
            result=starting_result, score=score, input_score=score, moved_count=0, improved=False
        )

    usable_area = _sheet_polygon(sheet_width_mm, sheet_height_mm, sheet_margin_mm)
    usable_area_mm2 = usable_area.area

    input_free_space = free_space_from_placed_parts(
        usable_area, starting_result.placed, clearance_mm=clearance_mm
    )
    input_score = score_layout(
        starting_result.placed, usable_area_mm2, input_free_space, weights=objective_weights
    )

    working = list(starting_result.placed)
    total_moved = 0

    for _pass_index in range(max_passes):
        if check_cancelled and check_cancelled():
            raise NestingCancelledError("تم إلغاء عملية الترتيب من قبل المستخدم.")
        pass_moved = 0
        for direction in directions:
            order = _order_for_direction(working, direction)
            working, moved_this_direction = _compact_one_pass(
                working, usable_area, direction, clearance_mm, order
            )
            pass_moved += moved_this_direction
        total_moved += pass_moved
        if pass_moved == 0:
            break

    compacted_free_space = free_space_from_placed_parts(usable_area, working, clearance_mm=clearance_mm)
    compacted_score = score_layout(working, usable_area_mm2, compacted_free_space, weights=objective_weights)

    final_placed = working
    final_score = compacted_score
    # Acceptance is gated on total_free_area_mm2, not on score_layout.total.
    # Compaction only ever TRANSLATES an already-fixed set of parts (never
    # adds, removes, or resizes one), and every slide is accepted only when
    # exact bisection proves it stays within the usable area and at least
    # clearance_mm from every other part -- so the union of occupied+
    # clearance area can only shrink or stay the same, which means the total
    # unused area can only grow or stay the same. That is a structural
    # geometric fact of this algorithm, provable from the slide mechanism
    # itself, not a heuristic. score_layout.total is the right tool for LNS
    # (comparing candidate layouts that differ in WHICH parts are placed,
    # where a subjective free-space-quality term is the only way to break
    # ties on identical placed_count), but its compactness_bonus term can
    # score a pure translation as WORSE even when free area strictly grew --
    # e.g. sliding a single part flush against a sheet edge turns a
    # wrap-around pocket into one long rectangle, which is a strictly better
    # nesting outcome (larger, boundary-accessible, unambiguously reachable)
    # but has a lower isoperimetric ratio than the chunkier wrap-around
    # shape it replaced. Using that term to gate compaction's own output
    # would make compaction reject its own correct, monotonic result on
    # exactly the layouts it exists to improve. score_layout is still
    # computed above and returned in CompactionResult for reporting/
    # comparison, matching the existing field contract -- only the
    # accept/reject decision changes.
    if compacted_free_space.total_free_area_mm2 < input_free_space.total_free_area_mm2 - _SETTLE_STEP_TOLERANCE_MM:
        # Should be structurally unreachable given the slide mechanism, but
        # checked rather than assumed, exactly like lns.py's own
        # never-worse guarantee for its own different (membership-based)
        # correctness contract.
        final_placed = starting_result.placed
        final_score = input_score
        total_moved = 0
    else:
        report = validate_layout(
            final_placed, sheet_width_mm, sheet_height_mm, sheet_margin_mm, clearance_mm=clearance_mm
        )
        if not report.is_valid:
            final_placed = starting_result.placed
            final_score = input_score
            total_moved = 0

    final_result = NestingResult(
        placed=final_placed,
        unplaced_part_ids=starting_result.unplaced_part_ids,
        sheet_full=starting_result.sheet_full,
        processed_count=starting_result.processed_count,
        total_count=starting_result.total_count,
    )

    return CompactionResult(
        result=final_result,
        score=final_score,
        input_score=input_score,
        moved_count=total_moved,
        # total_moved is reset to 0 on both fallback paths above (free-area
        # regression or failed re-validation), so total_moved > 0 already
        # means exactly "the accepted layout is the compacted one, and it
        # differs from the input" -- consistent with the free-area-based
        # gate this result came from, unlike score_layout.total which can
        # decrease on a compaction that strictly grew free area (see the
        # gate's own comment above).
        improved=total_moved > 0,
    )
