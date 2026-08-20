"""Exact irregular sheet nesting with early saturation detection."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import TypeAlias

import numpy as np

from shapely.affinity import scale, translate
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

from app.core_logging import get_logger
from app.geometry.clearance import CLEARANCE_MM, apply_clearance
from app.nesting.nfp import compute_nfp, prepare_nfp_triangles
from app.nesting.rotation import ALL_ROTATIONS, LockedRotation, fine_neighbors_of, rotate_shape

logger = get_logger(__name__)
# The requested clearance is authoritative. Geometry tolerances belong only
# in numerical comparisons, never as hidden physical spacing on the sheet.
_ENGINE_SAFETY_MARGIN_MM = 0.0
_FEASIBILITY_AREA_EPS_MM2 = 1e-9
_FAST_PATH_MAX_VERTICES = 100_000
_FAST_PATH_TOTAL_VERTICES = 1_000_000
_FAST_PATH_TOLERANCE_MM = 1e-6
# Spec section 5 ("محاولة واحدة ذكية وليس محاولات متعددة"): exactly ONE
# packing strategy for the initial greedy layout. The rejected alternative --
# 4-5 greedy attempts with different orderings, keeping whichever happened to
# score best -- produces gaps and picks the least-bad one instead of actually
# closing them; run_lns_optimization (destroy/repair) and compact_layout are
# what do the real optimization work afterward, on top of this single start.
# area_desc + bottom_left is that one strategy: largest-area-first ordering
# into the bottom-left placement policy, the same starting point test_lns.py's
# own fixtures already assume when they call run_best_single_sheet_nesting
# with packing_attempts=1 before feeding the result into LNS.
_PACKING_STRATEGIES: tuple[tuple[str, str], ...] = (
    ("area_desc", "bottom_left"),
)
NfpTriangles: TypeAlias = np.ndarray


class NestingError(Exception):
    pass


class NestingCancelledError(Exception):
    pass


@dataclass(slots=True)
class _PreparedRotation:
    """One exact, reusable orientation of a source contour.

    A part's contour never changes while a layout is being calculated.  The
    old hot path nevertheless rotated, centred and triangulated the same
    moving contour for every already-placed part.  Keep that immutable work
    here and reuse it.  Only the NFP against the *current* occupied area and
    the final translation remain per-placement operations.
    """

    angle: LockedRotation
    centered_shape_mm: BaseGeometry
    variant_key: bytes
    negated_triangles: NfpTriangles | None = None
    stationary_triangles: NfpTriangles | None = None


@dataclass(frozen=True, slots=True)
class _OccupiedZone:
    """A translated, reusable clearance silhouette already on the sheet."""

    rotation: _PreparedRotation
    center_x_mm: float
    center_y_mm: float


@dataclass(slots=True)
class _MergedBlockedCache:
    """Running union of every zone's translated blocked region, for ONE rotation.

    ``occupied_zones`` only ever grows (append-only) within a single
    ``run_nesting`` call.  Without this cache, every rotation of every part
    re-derives the union of ALL zones placed so far from scratch on every
    single call -- with up to 24 rotations tried per part, that means a part
    placed after N others pays roughly 24x the cost of unioning N zones,
    every single time, even though only the zones added since this exact
    rotation was last evaluated are actually new. This cache remembers how
    many zones it has already folded in and the resulting merged geometry,
    so a later call for the same rotation only unions in the zones appended
    since then -- turning repeated O(all zones) work into amortized
    O(newly added zones).
    """

    zones_merged_count: int = 0
    merged_blocked: BaseGeometry | None = None


@dataclass(frozen=True, slots=True)
class PartInput:
    shape_mm: BaseGeometry
    source_image_path: str
    source_centroid_px: tuple[float, float] | None = None
    alpha_bbox_px: tuple[int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class PlacedPart:
    part_id: str
    source_image_path: str
    placed_shape_mm: BaseGeometry
    rotation: LockedRotation
    source_centroid_px: tuple[float, float] | None = None
    alpha_bbox_px: tuple[int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class NestingResult:
    placed: list[PlacedPart] = field(default_factory=list)
    unplaced_part_ids: list[str] = field(default_factory=list)
    sheet_full: bool = False
    processed_count: int = 0
    total_count: int = 0

    @property
    def all_placed(self) -> bool:
        return not self.unplaced_part_ids

    @property
    def completed_with_capacity(self) -> bool:
        return self.sheet_full and bool(self.unplaced_part_ids)


@dataclass(frozen=True, slots=True)
class MultiSheetNestingResult:
    """A sequence of independent sheets plus pieces that fit on none of them."""

    sheets: list[NestingResult] = field(default_factory=list)
    unplaced_part_ids: list[str] = field(default_factory=list)
    total_count: int = 0

    @property
    def placed_count(self) -> int:
        return sum(len(sheet.placed) for sheet in self.sheets)

    @property
    def all_placed(self) -> bool:
        return not self.unplaced_part_ids and self.placed_count == self.total_count


def _sheet_polygon(width_mm: float, height_mm: float, margin_mm: float) -> Polygon:
    return box(margin_mm, margin_mm, width_mm - margin_mm, height_mm - margin_mm)


def _candidate_points_from_region(usable_area: BaseGeometry) -> list[tuple[float, float]]:
    if usable_area.is_empty:
        return []
    polys = list(usable_area.geoms) if isinstance(usable_area, MultiPolygon) else [usable_area]
    points: list[tuple[float, float]] = []
    for poly in polys:
        if not isinstance(poly, Polygon) or poly.is_empty:
            continue
        points.extend(poly.exterior.coords[:-1])
        for interior in poly.interiors:
            points.extend(interior.coords[:-1])
    return points


def _placement_score(
    shape_centered_mm: BaseGeometry,
    point: tuple[float, float],
    policy: str,
) -> tuple[float, float, float, float]:
    """Rank proven-feasible candidates without adding artificial padding."""
    minx, miny, maxx, maxy = shape_centered_mm.bounds
    left, bottom = point[0] + minx, point[1] + miny
    right, top = point[0] + maxx, point[1] + maxy
    if policy == "bottom_left":
        return bottom, left, top, right
    if policy == "bottom_right":
        return bottom, -right, top, -left
    if policy == "compact":
        # This third ordering explores a different, still deterministic, edge
        # contact preference. Candidate validity remains exact NFP/GEOS.
        return right, top, left, bottom
    if policy == "top_left":
        # Vertical mirror of bottom_left: prefers the highest reachable point
        # first, then leftmost. No existing policy searches downward from the
        # top of the sheet, so this reaches feasible corners the other three
        # policies rank last.
        return -top, left, -bottom, right
    raise NestingError(f"سياسة packing غير مدعومة: {policy}")


def _vertex_count(shape: BaseGeometry) -> int:
    """Count source contour vertices without triangulating the geometry."""
    polygons = list(shape.geoms) if isinstance(shape, MultiPolygon) else [shape]
    count = 0
    for polygon in polygons:
        if not isinstance(polygon, Polygon):
            continue
        count += len(polygon.exterior.coords) - 1
        count += sum(len(interior.coords) - 1 for interior in polygon.interiors)
    return count


def _should_use_fast_candidate_path(parts_mm: dict[str, PartInput]) -> bool:
    """Route to the fast candidate-generation path only when the exact path
    would genuinely be too slow to use.

    _FAST_PATH_MAX_VERTICES and _FAST_PATH_TOTAL_VERTICES exist specifically
    to gate this decision (see their definitions above), but were never
    actually consulted -- this function previously always returned True,
    which meant every job always used the fast path, regardless of actual
    complexity. That silently disabled two things on every call, not just
    performance: the exact-NFP-only placement logic in _place_one_part, and
    the early-exit saturation probe (_has_any_remaining_fit) that stops the
    main sequential loop once no remaining distinct shape can fit anywhere
    -- see run_nesting's `if not use_fast_candidate_path and loop_index <
    total - 1` guard, which was structurally unreachable while this always
    returned True.

    A single shape past _FAST_PATH_MAX_VERTICES, or the whole job's vertex
    total past _FAST_PATH_TOTAL_VERTICES, means the exact path's per-rotation
    unary_union/NFP cost across many parts would be impractically slow --
    that is what the fast path exists to avoid. Below both thresholds, the
    exact path is used, restoring both its own placement quality and the
    saturation probe's early-exit behaviour for the common case.
    """
    total_vertices = 0
    for part in parts_mm.values():
        vertices = _vertex_count(part.shape_mm)
        if vertices > _FAST_PATH_MAX_VERTICES:
            return True
        total_vertices += vertices
        if total_vertices > _FAST_PATH_TOTAL_VERTICES:
            return True
    return False


def _shrink_sheet_for_shape_center(
    usable_area: Polygon,
    moving_shape_centered_mm: BaseGeometry,
) -> Polygon | None:
    minx, miny, maxx, maxy = moving_shape_centered_mm.bounds
    sx0, sy0, sx1, sy1 = usable_area.bounds
    new_x0 = sx0 - minx
    new_x1 = sx1 - maxx
    new_y0 = sy0 - miny
    new_y1 = sy1 - maxy
    if new_x0 > new_x1 or new_y0 > new_y1:
        return None
    if new_x0 == new_x1 or new_y0 == new_y1:
        return Polygon.from_bounds(new_x0, new_y0, new_x1, new_y1)
    return Polygon.from_bounds(new_x0, new_y0, new_x1, new_y1)


def _moving_triangles(rotation: _PreparedRotation) -> NfpTriangles:
    """Return the exact reflected mesh, creating it only when it is needed."""
    if rotation.negated_triangles is None:
        negated = scale(
            rotation.centered_shape_mm,
            xfact=-1.0,
            yfact=-1.0,
            origin=(0, 0),
        )
        rotation.negated_triangles = prepare_nfp_triangles(negated)
    return rotation.negated_triangles


def _stationary_triangles(rotation: _PreparedRotation) -> NfpTriangles:
    """Triangulate an immutable placed silhouette only once."""
    if rotation.stationary_triangles is None:
        rotation.stationary_triangles = prepare_nfp_triangles(rotation.centered_shape_mm)
    return rotation.stationary_triangles


def _zone_bounds_mm(zone: _OccupiedZone) -> tuple[float, float, float, float]:
    """Cheap translated bounding box for a zone, no geometry construction.

    A zone's ``centered_shape_mm`` bounds are computed once when its rotation
    is prepared; offsetting them by the zone's stored centre is O(1) and
    avoids materialising the actual translated silhouette just to know its
    envelope.
    """
    minx, miny, maxx, maxy = zone.rotation.centered_shape_mm.bounds
    return (
        minx + zone.center_x_mm,
        miny + zone.center_y_mm,
        maxx + zone.center_x_mm,
        maxy + zone.center_y_mm,
    )


def _bounds_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    margin: float,
) -> bool:
    """Whether two envelopes can possibly interact within ``margin``."""
    return not (
        first[2] + margin < second[0]
        or second[2] + margin < first[0]
        or first[3] + margin < second[1]
        or second[3] + margin < first[1]
    )


def _build_allowed_center_region_from_zones(
    moving_rotation: _PreparedRotation,
    usable_area: Polygon,
    occupied_zones: list[_OccupiedZone],
    *,
    clearance_mm: float,
    nfp_cache: dict[tuple[bytes, bytes], BaseGeometry],
    merged_cache: dict[bytes, _MergedBlockedCache] | None = None,
    deadline: float | None = None,
) -> BaseGeometry | None:
    """Calculate the allowed centres without rebuilding a growing global NFP.

    Minkowski addition distributes exactly over a union:

    ``(A ∪ B) ⊕ -M == (A ⊕ -M) ∪ (B ⊕ -M)``.

    Therefore subtracting each placed zone's NFP produces the same feasible
    centre set as calculating an NFP for the union of all occupied zones.  It
    has two important operational advantages: individual zones have a stable
    precomputed triangulation, and an NFP between two repeated silhouettes is
    computed once then only translated for every copy on the sheet.  The
    clearance offset is applied to the raw NFP afterwards, using
    ``(A ⊕ D) ⊕ -M == (A ⊕ -M) ⊕ D``.  This preserves the same geometric
    clearance while avoiding triangulating the many arc segments a buffer
    adds before every NFP operation.

    A zone whose translated bounding box cannot possibly reach ``valid_centers``
    (the moving shape's own feasible-centre envelope, fixed for the whole run
    since it depends only on ``usable_area`` and this rotation's shape -- not
    on which zones exist yet) is skipped entirely: no NFP lookup, no
    translate, no union. This is an exact filter, not an approximation -- a
    zone's NFP-blocked area against the moving shape is necessarily contained
    within (zone envelope + moving envelope diagonal), so if the zone's own
    envelope, padded generously by that diagonal plus clearance, cannot reach
    ``valid_centers``' envelope, including it can never change the result.

    ``merged_cache``, when given, remembers -- per rotation -- how many
    leading zones of ``occupied_zones`` have already been folded into a
    running merged-blocked geometry. Since ``occupied_zones`` only ever grows
    (append-only) within one ``run_nesting`` call, and ``valid_centers``
    depends only on this rotation (stable across the whole call), a zone
    proven out of reach on one call stays out of reach for every later call
    of the SAME rotation too. This lets each call process only the zones
    appended since this rotation's cache entry was last updated, instead of
    re-deriving the union of every zone placed so far from scratch every
    single time -- which is what made placing part N cost roughly (rotations
    tried) times the cost of unioning N zones, repeated for every part.

    ``deadline``, when given, is checked BEFORE each zone's ``compute_nfp``
    call inside the loop below -- not just once before this function is
    entered. A part whose rotation has never been evaluated against a large
    ``occupied_zones`` (e.g. the first backfill part after a long fast-path
    main pass) can otherwise spend minutes inside a SINGLE call processing
    every new zone one Minkowski sum at a time, with the caller's own
    per-rotation deadline check never getting a chance to run again until
    that one call finally returns. If the deadline is hit partway through
    ``new_zones``, this returns None (a conservative, always-safe answer --
    the caller treats it exactly like "no room found this rotation", never
    like a placement was accepted). ``merged_cache`` is intentionally NOT
    updated in that case: recording partial progress would need the cache to
    track exactly which zones were folded in (not just how many, since a
    fixed count assumes a contiguous prefix), which the existing
    ``zones_merged_count`` design does not support. Skipping the cache write
    entirely means the next call simply reprocesses the same ``new_zones``
    from scratch -- correct, just not optimal -- which is the same trade-off
    ``_place_one_part`` already makes when its own deadline stops a rotation
    partway through: correctness first, and only a best-effort cache hit.
    """
    valid_centers = _shrink_sheet_for_shape_center(
        usable_area,
        moving_rotation.centered_shape_mm,
    )
    if valid_centers is None or valid_centers.is_empty:
        return None

    if not occupied_zones:
        return valid_centers

    # The moving shape's own footprint plus clearance bounds how far its NFP
    # against any zone can reach beyond that zone's envelope. Padding every
    # zone-vs-allowed-region bounds check by this fixed amount keeps the
    # skip test exact regardless of which zone or rotation is being tested.
    mminx, mminy, mmaxx, mmaxy = moving_rotation.centered_shape_mm.bounds
    reach_margin = max(mmaxx - mminx, mmaxy - mminy) + clearance_mm

    # outer_bounds is valid_centers' own (unshrunk) envelope -- fixed for the
    # whole run given this rotation and usable_area, which is exactly what
    # makes a per-zone pruning decision safe to remember across calls: it
    # never changes underneath the cache between one call and the next.
    outer_bounds = valid_centers.bounds

    cache_entry = merged_cache.get(moving_rotation.variant_key) if merged_cache is not None else None
    already_merged_count = cache_entry.zones_merged_count if cache_entry is not None else 0
    running_merged = cache_entry.merged_blocked if cache_entry is not None else None

    new_zones = occupied_zones[already_merged_count:]
    if new_zones:
        moving_triangles = _moving_triangles(moving_rotation)
        surviving_blocked: list[BaseGeometry] = []
        deadline_hit = False
        for zone in new_zones:
            if not _bounds_overlap(outer_bounds, _zone_bounds_mm(zone), reach_margin):
                continue

            cache_key = (zone.rotation.variant_key, moving_rotation.variant_key)
            blocked_local = nfp_cache.get(cache_key)
            if blocked_local is None:
                # Checked right before the expensive Minkowski-sum call, not
                # just once before this whole function runs: a rotation with
                # many genuinely NEW zones (e.g. the first backfill part
                # after a long fast-path main pass, where merged_cache has
                # no entry yet) can otherwise spend the ENTIRE remaining
                # deadline inside this one loop, one compute_nfp() at a time,
                # with no opportunity for the caller's own per-rotation check
                # to run again until this whole call finally returns.
                if deadline is not None and time.monotonic() >= deadline:
                    deadline_hit = True
                    break
                raw_nfp = compute_nfp(
                    zone.rotation.centered_shape_mm,
                    moving_rotation.centered_shape_mm,
                    stationary_triangles=_stationary_triangles(zone.rotation),
                    moving_triangles=moving_triangles,
                ).region_mm
                blocked_local = apply_clearance(raw_nfp, clearance_mm)
                nfp_cache[cache_key] = blocked_local

            translated_blocked = translate(
                blocked_local,
                xoff=zone.center_x_mm,
                yoff=zone.center_y_mm,
            )
            if not translated_blocked.is_valid:
                translated_blocked = translated_blocked.buffer(0)
            
            # The cheap envelope bounds check above can only prove
            # separation, not overlap (blocked_local can be smaller than the
            # zone's own envelope). A second, still free, envelope check
            # against the exact translated NFP region catches the remaining
            # no-op zones before they are folded into the running merge.
            if not _bounds_overlap(outer_bounds, translated_blocked.bounds, 0.0):
                continue

            surviving_blocked.append(translated_blocked)

        if deadline_hit:
            # A partial pass over new_zones proves nothing about the zones
            # never reached -- returning None here ("no room found this
            # rotation") is the only answer that stays correct regardless of
            # what those unreached zones would have blocked. merged_cache is
            # deliberately left untouched: its zones_merged_count assumes a
            # CONTIGUOUS prefix of occupied_zones was folded in, which does
            # not hold for a partial pass, so recording it would make a
            # later call wrongly skip zones that were never actually
            # processed. The next call simply reprocesses new_zones from
            # scratch -- more work, but never an incorrect placement.
            return None

        if surviving_blocked:
            # Fold only the NEW survivors into whatever was already merged.
            # unary_union of (running_merged, *new pieces) is algebraically
            # identical to re-unioning every zone from scratch -- union is
            # associative and commutative -- but its cost is proportional to
            # the new pieces plus one simplification pass over the existing
            # running geometry, not to re-deriving that geometry every call.
            try:
                running_merged = unary_union(
                    [running_merged, *surviving_blocked] if running_merged is not None else surviving_blocked
                )
            except Exception:
                safe_pieces = [p.buffer(0) for p in ([running_merged, *surviving_blocked] if running_merged is not None else surviving_blocked)]
                running_merged = unary_union(safe_pieces)

        if merged_cache is not None:
            merged_cache[moving_rotation.variant_key] = _MergedBlockedCache(
                zones_merged_count=len(occupied_zones),
                merged_blocked=running_merged,
            )

    if running_merged is None:
        return valid_centers

    try:
        allowed = valid_centers.difference(running_merged)
    except Exception:
        allowed = valid_centers.buffer(0).difference(running_merged.buffer(0))
        
    if allowed.is_empty:
        return None
    return allowed


def _candidate_satisfies_exact_clearance(
    moving_shape_centered_mm: BaseGeometry,
    center_x: float,
    center_y: float,
    occupied_zones: list[_OccupiedZone],
    clearance_mm: float,
) -> bool:
    """Independently re-validate one chosen candidate against real placed shapes.

    ``_candidate_points_from_region`` draws candidates from the boundary
    vertices of ``valid_centers.difference(running_merged)``. Shapely proves
    that boundary is part of the allowed set (a vertex can legitimately
    satisfy ``allowed.touches(point)`` at exactly zero distance, which is by
    design -- clearance is already baked into the NFP, so touching the
    boundary is a valid placement, not a violation; see nfp.py's
    ``point_is_valid_placement`` docstring). That proof is about ``allowed``
    as GEOS represents it, not about the point's true distance to each
    occupied zone's own actual placed geometry. ``unary_union``/``difference``
    can, at floating-point resolution, produce a boundary vertex that is
    simultaneously "on ``allowed``'s boundary" and a hair inside one
    constituent zone's own translated NFP+clearance region when that region
    is reconstructed and tested independently -- the two representations of
    the same geometric fact can diverge by a GEOS floating-point sliver.

    This mirrors the fast candidate path's own proven two-phase pattern
    (``_resolve_ambiguous_candidate``): a cheap bounds-distance check first
    resolves the common case (genuinely far zones) with pure arithmetic, and
    only zones whose envelopes are ambiguously close pay for an exact GEOS
    ``.distance()`` call against the real translated shape. The exact-NFP
    path already trusts its own NFP-derived region for candidate GENERATION;
    this adds the same independent acceptance gate the fast path already has
    before a candidate is actually committed to, closing the one place in
    this path that lacked it.
    """
    minx, miny, maxx, maxy = moving_shape_centered_mm.bounds
    candidate_bounds = (
        minx + center_x,
        miny + center_y,
        maxx + center_x,
        maxy + center_y,
    )
    ambiguous_zones: list[_OccupiedZone] = []
    for zone in occupied_zones:
        if _bounds_distance(candidate_bounds, _zone_bounds_mm(zone)) >= clearance_mm - _FAST_PATH_TOLERANCE_MM:
            continue
        ambiguous_zones.append(zone)
    if not ambiguous_zones:
        return True

    candidate_shape = translate(moving_shape_centered_mm, xoff=center_x, yoff=center_y)
    for zone in ambiguous_zones:
        stationary = translate(
            zone.rotation.centered_shape_mm,
            xoff=zone.center_x_mm,
            yoff=zone.center_y_mm,
        )
        if candidate_shape.distance(stationary) < clearance_mm - _FAST_PATH_TOLERANCE_MM:
            return False
    return True


def _find_best_placement_from_zones(
    moving_rotation: _PreparedRotation,
    usable_area: Polygon,
    occupied_zones: list[_OccupiedZone],
    *,
    clearance_mm: float,
    nfp_cache: dict[tuple[bytes, bytes], BaseGeometry],
    merged_cache: dict[bytes, _MergedBlockedCache] | None = None,
    require_positive_area: bool = False,
    placement_policy: str = "bottom_left",
    deadline: float | None = None,
) -> tuple[float, float] | None:
    # A cheap monotonic-clock check before the expensive NFP/union work below.
    # ``deadline`` is a soft, best-effort budget: it never aborts a candidate
    # mid-computation, so a caller past deadline still gets a clean None
    # instead of a half-built result -- correctness of any ACCEPTED placement
    # is never affected, only whether one more rotation is attempted at all.
    if deadline is not None and time.monotonic() >= deadline:
        return None
    candidate_source = _build_allowed_center_region_from_zones(
        moving_rotation,
        usable_area,
        occupied_zones,
        clearance_mm=clearance_mm,
        nfp_cache=nfp_cache,
        merged_cache=merged_cache,
        deadline=deadline,
    )
    if candidate_source is None:
        return None
    if require_positive_area and candidate_source.area <= _FEASIBILITY_AREA_EPS_MM2:
        return None

    candidates = _candidate_points_from_region(candidate_source)
    if not candidates:
        if candidate_source.area > 0 and not require_positive_area:
            point = candidate_source.representative_point()
            candidates = [(point.x, point.y)]
        else:
            return None

    # Candidates are derived from the exact feasible region above, which is
    # sufficient to PROPOSE a point but not, on its own, sufficient to COMMIT
    # to one: a boundary vertex of a difference()-computed region can, at
    # floating-point resolution, diverge from an independent exact distance
    # check against one of that region's own constituent zones (see
    # _candidate_satisfies_exact_clearance). Sorting once by score and then
    # validating in score order means the common case -- the best-scoring
    # candidate is genuinely fine -- costs exactly one extra distance-check
    # pass over occupied_zones, not a second NFP computation; only a
    # candidate that fails falls through to the next-best by score, bounded
    # by the candidate count.
    ordered_candidates = sorted(
        candidates,
        key=lambda point: _placement_score(
            moving_rotation.centered_shape_mm, point, placement_policy
        ),
    )
    for point in ordered_candidates:
        if _candidate_satisfies_exact_clearance(
            moving_rotation.centered_shape_mm,
            point[0],
            point[1],
            occupied_zones,
            clearance_mm,
        ):
            return point
    return None


def _fast_candidate_centers(
    moving_shape_centered_mm: BaseGeometry,
    usable_area: Polygon,
    placed_bounds: list[tuple[float, float, float, float]],
    clearance_mm: float,
    placement_policy: str,
) -> list[tuple[float, float]]:
    """Generate candidate placement centres from already placed envelopes.

    These positions are only *proposals*. A proposal is accepted only when
    its envelope mathematically proves the requested clearance, or after an
    exact check against the original irregular contours where envelopes alone
    are inconclusive.

    Hybrid strategy:
    1) Edge-only candidates around each placed part's bounding box (fast,
       O(placed_count) with a small constant).
    2) A **global sheet grid** that covers the entire usable area at a fixed
       step size.  This catches gaps between irregular shapes that no single
       placed part's bounding-box edge can reach.  The grid cost is fixed
       (depends only on sheet size, not on how many parts are placed) so it
       does not slow down as the sheet fills up.
    """
    minx, miny, maxx, maxy = moving_shape_centered_mm.bounds
    sheet_x0, sheet_y0, sheet_x1, sheet_y1 = usable_area.bounds
    candidates: set[tuple[float, float]] = {
        (sheet_x0 - minx, sheet_y0 - miny),
    }

    # Sheet corners
    candidates.add((sheet_x1 - maxx, sheet_y0 - miny))
    candidates.add((sheet_x0 - minx, sheet_y1 - maxy))
    candidates.add((sheet_x1 - maxx, sheet_y1 - maxy))

    # --- Per-placed-part edge candidates (original fast strategy) ---
    for px0, py0, px1, py1 in placed_bounds:
        right_x = px1 + clearance_mm - minx
        above_y = py1 + clearance_mm - miny
        start_x = px0 - maxx - clearance_mm
        start_y = py0 - maxy - clearance_mm

        grid_step = 5.0
        steps_x = min(max(1, int((right_x - start_x) / grid_step)), 10)
        steps_y = min(max(1, int((above_y - start_y) / grid_step)), 10)
        dx = (right_x - start_x) / steps_x
        dy = (above_y - start_y) / steps_y

        # Bottom and Top edges
        for i in range(steps_x + 1):
            cx = start_x + i * dx
            candidates.add((cx, start_y))
            candidates.add((cx, above_y))
        # Left and Right edges
        for j in range(steps_y + 1):
            cy = start_y + j * dy
            candidates.add((start_x, cy))
            candidates.add((right_x, cy))

    return sorted(
        candidates,
        key=lambda point: _placement_score(
            moving_shape_centered_mm, point, placement_policy
        ),
    )


def _candidate_sheet_bounds(
    moving_shape_centered_mm: BaseGeometry,
    center_x: float,
    center_y: float,
) -> tuple[float, float, float, float]:
    moving_minx, moving_miny, moving_maxx, moving_maxy = moving_shape_centered_mm.bounds
    return (
        moving_minx + center_x,
        moving_miny + center_y,
        moving_maxx + center_x,
        moving_maxy + center_y,
    )


def _candidate_fits_sheet(
    candidate_bounds: tuple[float, float, float, float],
    usable_area: Polygon,
) -> bool:
    usable_minx, usable_miny, usable_maxx, usable_maxy = usable_area.bounds
    # The usable sheet is a rectangle.  A contour is a subset of its bounding
    # box, therefore this inexpensive bounds test proves containment without
    # translating the full (often thousand-point) contour first.
    return not (
        candidate_bounds[0] < usable_minx - _FAST_PATH_TOLERANCE_MM
        or candidate_bounds[1] < usable_miny - _FAST_PATH_TOLERANCE_MM
        or candidate_bounds[2] > usable_maxx + _FAST_PATH_TOLERANCE_MM
        or candidate_bounds[3] > usable_maxy + _FAST_PATH_TOLERANCE_MM
    )


def _bounds_distance(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    """Cheap lower-bound on the true distance between two envelopes.

    Pure arithmetic, no GEOS call. If this lower bound already proves
    separation of at least ``clearance_mm``, the enclosed contours are
    necessarily that far apart too, so an expensive exact check can be
    skipped entirely.
    """
    dx = max(second[0] - first[2], first[0] - second[2], 0.0)
    dy = max(second[1] - first[3], first[1] - second[3], 0.0)
    return math.hypot(dx, dy)


def _resolve_ambiguous_candidate(
    moving_shape_centered_mm: BaseGeometry,
    center_x: float,
    center_y: float,
    placed_shapes: list[BaseGeometry],
    placed_bounds: list[tuple[float, float, float, float]],
    placed_spatial_index: STRtree,
    clearance_mm: float,
) -> bool:
    """Exact accept/reject for one candidate whose sheet-bounds check already passed.

    Two-phase check, restored after benchmarking against a single-call
    ``dwithin`` alternative: at realistic scale (153 high-vertex sticker
    contours) ``dwithin`` measured SLOWER (141.36s) than this two-phase
    approach (114.91s) -- ``dwithin`` always pays a full GEOS/C++ round-trip
    per candidate, while this version resolves the common case (envelopes
    already at least ``clearance_mm`` apart) with pure Python arithmetic and
    never touches GEOS at all for those candidates. Only genuinely ambiguous
    envelope overlaps fall through to an exact GEOS ``.distance()`` call
    against the real translated contour -- no approximation, since the final
    accept/reject for an ambiguous pair still uses true geometric distance,
    not a bounding-box proxy.
    """
    moving_minx, moving_miny, moving_maxx, moving_maxy = moving_shape_centered_mm.bounds
    candidate_bounds = (
        moving_minx + center_x,
        moving_miny + center_y,
        moving_maxx + center_x,
        moving_maxy + center_y,
    )

    nearby_indices = placed_spatial_index.query(
        box(
            candidate_bounds[0] - clearance_mm,
            candidate_bounds[1] - clearance_mm,
            candidate_bounds[2] + clearance_mm,
            candidate_bounds[3] + clearance_mm,
        )
    )
    ambiguous_indices: list[int] = []
    for raw_index in nearby_indices:
        index = int(raw_index)
        # If envelopes are at least the requested clearance apart, their
        # enclosed contours are necessarily at least that far apart too. This
        # is a mathematical early accept, not a contour approximation.
        if _bounds_distance(candidate_bounds, placed_bounds[index]) >= clearance_mm - _FAST_PATH_TOLERANCE_MM:
            continue
        ambiguous_indices.append(index)

    if not ambiguous_indices:
        return True

    # Only genuinely ambiguous envelope overlaps need a translated full
    # contour and a GEOS distance check. Creating that expensive geometry for
    # every proposal (ambiguous or not) is what made dwithin slower overall.
    candidate_shape = translate(moving_shape_centered_mm, xoff=center_x, yoff=center_y)
    
    import shapely
    ambiguous_geoms = [placed_shapes[i] for i in ambiguous_indices]
    if shapely.dwithin(candidate_shape, ambiguous_geoms, clearance_mm - _FAST_PATH_TOLERANCE_MM).any():
        return False
    return True


def _find_fast_placement(
    moving_rotation: _PreparedRotation,
    usable_area: Polygon,
    placed_shapes: list[BaseGeometry],
    placed_bounds: list[tuple[float, float, float, float]],
    spatial_index: STRtree | None,
    clearance_mm: float,
    placement_policy: str,
    deadline: float | None = None,
) -> tuple[float, float] | None:
    # Mirrors _find_best_placement_from_zones' soft, best-effort deadline: a
    # cheap monotonic-clock check before the expensive candidate generation +
    # per-candidate STRtree/GEOS validation below (measured at up to ~6,000
    # candidates per rotation at realistic placed-part counts). Never aborts a
    # candidate mid-check, so a caller past deadline still gets a clean None
    # instead of a half-evaluated result -- correctness of any ACCEPTED
    # placement is never affected, only whether one more rotation is tried.
    if deadline is not None and time.monotonic() >= deadline:
        return None
    centered = moving_rotation.centered_shape_mm
    candidates = _fast_candidate_centers(
        centered,
        usable_area,
        placed_bounds,
        clearance_mm,
        placement_policy,
    )
    if not candidates:
        return None

    # The sheet-containment bounds check is pure arithmetic (no GEOS object
    # construction at all) -- a candidate that does not even fit the usable
    # sheet is rejected before it ever reaches the STRtree/exact check.
    for center_x, center_y in candidates:
        sheet_bounds = _candidate_sheet_bounds(centered, center_x, center_y)
        if not _candidate_fits_sheet(sheet_bounds, usable_area):
            continue
        if spatial_index is None or _resolve_ambiguous_candidate(
            centered, center_x, center_y, placed_shapes, placed_bounds, spatial_index, clearance_mm
        ):
            return center_x, center_y
    return None


def _place_one_part_fast(
    rotations: tuple[_PreparedRotation, ...],
    usable_area: Polygon,
    placed_parts: list[PlacedPart],
    *,
    clearance_mm: float,
    placement_policy: str,
    deadline: float | None = None,
) -> tuple[LockedRotation, BaseGeometry, tuple[float, float]] | None:
    # The stationary geometry does not change while the four rotations of this
    # part are evaluated.  Constructing its STRtree once avoids rebuilding an
    # O(n) spatial index for every rotation.
    placed_shapes = [part.placed_shape_mm for part in placed_parts]
    placed_bounds = [tuple(shape.bounds) for shape in placed_shapes]
    spatial_index = STRtree(placed_shapes) if placed_shapes else None
    best: tuple[tuple[float, float], LockedRotation, BaseGeometry, tuple[float, float]] | None = None
    for rotation in rotations:
        # Checked once per rotation (not just once per part), mirroring
        # _place_one_part's identical guard: with up to 24 rotations tried
        # here, a per-part-only check would still let a single part consume a
        # full 24x deadline overrun in the worst case. Measured cost at
        # realistic scale (150 already-placed parts): up to ~8.4s for one
        # call to this function, almost entirely inside the per-rotation
        # candidate validation _find_fast_placement performs below -- without
        # this guard a caller's own time_budget_seconds (e.g. LNS's per-
        # iteration budget) has no way to actually bound wall-clock time once
        # execution enters this loop.
        if deadline is not None and time.monotonic() >= deadline:
            break
        center = _find_fast_placement(
            rotation,
            usable_area,
            placed_shapes,
            placed_bounds,
            spatial_index,
            clearance_mm,
            placement_policy,
            deadline=deadline,
        )
        if center is None:
            continue
        final_shape = translate(rotation.centered_shape_mm, xoff=center[0], yoff=center[1])
        score = _placement_score(rotation.centered_shape_mm, center, placement_policy)
        if best is None or score < best[0]:
            best = (score, rotation.angle, final_shape, center)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _prepared_rotation_for_angle(shape_mm: BaseGeometry, angle: LockedRotation) -> _PreparedRotation | None:
    """Build one _PreparedRotation for an arbitrary LockedRotation angle,
    outside the fixed ALL_ROTATIONS (coarse-24) set _prepare_rotations builds.

    Used only by fine-rotation refinement (spec section 7) to materialise a
    FINE_* neighbour angle on demand, after a coarse search has already found
    a good placement worth refining around -- never called for every
    candidate, only for the small fine_neighbors_of() set around one winning
    coarse angle. Mirrors _prepare_rotations' own rotate-then-center
    construction exactly, so a fine rotation is triangulated/scored through
    the identical code path a coarse one uses, not a shortcut.
    """
    rotated = rotate_shape(shape_mm, angle)
    if rotated.is_empty or rotated.area <= 0:
        return None
    centered = _centered_shape(rotated)
    return _PreparedRotation(
        angle=angle,
        centered_shape_mm=centered,
        variant_key=centered.wkb,
    )


def _centered_shape(shape: BaseGeometry) -> BaseGeometry:
    centroid = shape.centroid
    return translate(shape, xoff=-centroid.x, yoff=-centroid.y)


def _ordered_part_ids(parts_mm: dict[str, PartInput], strategy: str) -> list[str]:
    """Return one deterministic, dimension-aware ordering for exact nesting."""
    def key(part_id: str) -> tuple[float, float, float, str]:
        shape = parts_mm[part_id].shape_mm
        minx, miny, maxx, maxy = shape.bounds
        width, height = maxx - minx, maxy - miny
        area = float(shape.area)
        max_side, min_side = max(width, height), min(width, height)
        bbox_area = width * height
        aspect = max_side / max(min_side, _FEASIBILITY_AREA_EPS_MM2)
        if strategy == "area_desc":
            return -area, -max_side, -bbox_area, part_id
        if strategy == "max_side_desc":
            return -max_side, -area, -bbox_area, part_id
        if strategy == "bbox_area_desc":
            return -bbox_area, -area, -max_side, part_id
        if strategy == "aspect_desc":
            return -aspect, -max_side, -area, part_id
        if strategy == "perimeter_desc":
            # bbox-based perimeter approximation, consistent with how every
            # other strategy here ranks by bounding-box metrics rather than
            # the exact polygon perimeter. A distinct axis from area/side/
            # aspect: a long thin part can rank low by area yet high here.
            perimeter = 2.0 * (width + height)
            return -perimeter, -area, -max_side, part_id
        raise NestingError(f"استراتيجية packing غير مدعومة: {strategy}")

    return sorted(parts_mm, key=key)


def _prepare_rotations(shape_mm: BaseGeometry) -> tuple[_PreparedRotation, ...]:
    """Build the four lossless rotations, with meshes prepared lazily.

    ``compute_nfp`` needs triangles of the reflected moving shape.  Those
    triangles only depend on the input contour and the locked rotation; they
    do not depend on the sheet or on any part that has already been placed.
    Caching them is therefore an algebraically identical calculation, not an
    approximation or a layout heuristic.
    """
    prepared: list[_PreparedRotation] = []
    for angle in ALL_ROTATIONS:
        rotated = rotate_shape(shape_mm, angle)
        if rotated.is_empty or rotated.area <= 0:
            continue
        centered = _centered_shape(rotated)
        prepared.append(
            _PreparedRotation(
                angle=angle,
                centered_shape_mm=centered,
                # WKB keeps distinct source contours/rotations separate while
                # letting duplicate uploaded silhouettes share their cache.
                variant_key=centered.wkb,
            )
        )
    return tuple(prepared)


def _refine_rotation_around_winner(
    source_shape_mm: BaseGeometry,
    coarse_best: tuple[tuple[float, float], _PreparedRotation, BaseGeometry, tuple[float, float]],
    usable_area: Polygon,
    occupied_zones: list[_OccupiedZone],
    *,
    clearance_mm: float,
    nfp_cache: dict[tuple[bytes, bytes], BaseGeometry],
    merged_cache: dict[bytes, _MergedBlockedCache] | None,
    placement_policy: str,
    deadline: float | None,
) -> tuple[tuple[float, float], _PreparedRotation, BaseGeometry, tuple[float, float]]:
    """Fine Rotation Refinement (spec section 7): after the coarse 24-angle
    search already found ``coarse_best``, try its small +/-3deg/+/-6deg
    LockedRotation neighbours (fine_neighbors_of) against the SAME
    occupied_zones, and keep whichever placement scores best.

    This never runs on every candidate -- only once, around one already-good
    coarse winner, exactly matching rotation.py's own documented contract for
    fine_neighbors_of ("only ever consulted ... after a coarse placement is
    already good, not on every candidate"). Strictly refining: coarse_best is
    always a valid, already-scored floor, so the returned tuple can only match
    or beat it, never regress it -- a fine neighbour is adopted only when its
    score is strictly lower (better) than the current best.

    Every fine candidate goes through the identical feasibility test a coarse
    one uses (_find_best_placement_from_zones against occupied_zones, with the
    same clearance/cache/deadline handling) -- this introduces no new
    feasibility test, only additional candidate ANGLES to score.

    Returns the winning ``_PreparedRotation`` object itself (not just its
    ``.angle``), same as ``coarse_best`` already carries -- this is what lets
    every caller build an ``_OccupiedZone`` directly from the result, whether
    the winner ended up coarse or fine, without needing to look it back up in
    a caller's ``prepared_rotations`` table (which only ever holds the 24
    coarse rotations -- see _place_one_part's own docstring for why a lookup
    by angle there would fail for a fine winner).
    """
    best = coarse_best
    try:
        neighbor_angles = fine_neighbors_of(best[1].angle)
    except ValueError:
        # best[1].angle can be a FINE_* angle here only if a caller ever
        # passes already-fine rotations into the coarse loop, which none of
        # this module's three call sites do (ALL_ROTATIONS is exactly the 24
        # coarse multiples) -- checked, not assumed, so this function never
        # raises out to a caller that only expects a placement result.
        return best
    for fine_angle in neighbor_angles:
        if deadline is not None and time.monotonic() >= deadline:
            break
        prepared = _prepared_rotation_for_angle(source_shape_mm, fine_angle)
        if prepared is None:
            continue
        position = _find_best_placement_from_zones(
            prepared,
            usable_area,
            occupied_zones,
            clearance_mm=clearance_mm,
            nfp_cache=nfp_cache,
            merged_cache=merged_cache,
            placement_policy=placement_policy,
            deadline=deadline,
        )
        if position is None:
            continue
        px, py = position
        final_shape = translate(prepared.centered_shape_mm, xoff=px, yoff=py)
        score = _placement_score(prepared.centered_shape_mm, position, placement_policy)
        if score < best[0]:
            best = (score, prepared, final_shape, position)
    return best


def _place_one_part(
    rotations: tuple[_PreparedRotation, ...],
    usable_area: Polygon,
    occupied_zones: list[_OccupiedZone],
    *,
    clearance_mm: float,
    nfp_cache: dict[tuple[bytes, bytes], BaseGeometry],
    placement_policy: str,
    merged_cache: dict[bytes, _MergedBlockedCache] | None = None,
    deadline: float | None = None,
    source_shape_mm: BaseGeometry | None = None,
) -> tuple[LockedRotation, BaseGeometry, tuple[float, float], _PreparedRotation] | None:
    """Find the best-scoring coarse rotation, then refine around it.

    ``source_shape_mm``, when given, enables Fine Rotation Refinement (spec
    section 7): once the coarse loop below finds its best-scoring one of the
    24 locked 15deg-multiple rotations, a small bounded set of +/-3deg/+/-6deg
    neighbours around THAT winning angle are also tried (never brute-forced
    over every candidate), and whichever placement scores best overall is
    returned. Omitting ``source_shape_mm`` (the default) skips refinement
    entirely and preserves the exact prior coarse-only behaviour -- every
    existing caller that does not yet pass it is unaffected.

    The 4th element of the returned tuple is the winning ``_PreparedRotation``
    object itself -- callers MUST use this (not a ``prepared_rotations[part_id]``
    lookup keyed by the returned angle) to build an ``_OccupiedZone``, because a
    refined winner's angle can be a FINE_* member that a caller's own
    ``prepared_rotations`` table (built only from the 24 coarse ALL_ROTATIONS)
    does not contain -- a lookup-by-angle there would raise StopIteration.
    """
    best_for_part: tuple[tuple[float, float], _PreparedRotation, BaseGeometry, tuple[float, float]] | None = None
    for rotation in rotations:
        # Checked once per rotation (not just once per part): with up to 24
        # rotations tried here, a per-part-only check would still let a
        # single part consume a full 24x deadline overrun in the worst case.
        if deadline is not None and time.monotonic() >= deadline:
            break
        centered = rotation.centered_shape_mm
        position = _find_best_placement_from_zones(
            rotation,
            usable_area,
            occupied_zones,
            clearance_mm=clearance_mm,
            nfp_cache=nfp_cache,
            merged_cache=merged_cache,
            placement_policy=placement_policy,
            deadline=deadline,
        )
        if position is None:
            continue
        px, py = position
        final_shape = translate(centered, xoff=px, yoff=py)
        score = _placement_score(centered, position, placement_policy)
        if best_for_part is None or score < best_for_part[0]:
            best_for_part = (score, rotation, final_shape, position)
    if best_for_part is None:
        return None
    if source_shape_mm is not None and (deadline is None or time.monotonic() < deadline):
        best_for_part = _refine_rotation_around_winner(
            source_shape_mm,
            best_for_part,
            usable_area,
            occupied_zones,
            clearance_mm=clearance_mm,
            nfp_cache=nfp_cache,
            merged_cache=merged_cache,
            placement_policy=placement_policy,
            deadline=deadline,
        )
    winning_rotation = best_for_part[1]
    return winning_rotation.angle, best_for_part[2], best_for_part[3], winning_rotation


def _has_any_remaining_fit(
    remaining_ids: list[str],
    prepared_rotations: dict[str, tuple[_PreparedRotation, ...]],
    usable_area: Polygon,
    occupied_zones: list[_OccupiedZone],
    *,
    clearance_mm: float,
    nfp_cache: dict[tuple[bytes, bytes], BaseGeometry],
    merged_cache: dict[bytes, _MergedBlockedCache] | None = None,
    check_cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Exact saturation probe.

    It checks whether at least one *remaining* distinct shape still has a
    positive-area feasible center region.  It deliberately avoids generating
    candidate points, so it is substantially cheaper than a normal placement.
    """
    seen_signatures: set[bytes] = set()
    for part_id in remaining_ids:
        if check_cancelled and check_cancelled():
            raise NestingCancelledError("تم إلغاء عملية الترتيب من قبل المستخدم.")
        rotations = prepared_rotations[part_id]
        try:
            signature = rotations[0].centered_shape_mm.wkb if rotations else b""
        except Exception:
            signature = repr(rotations).encode()
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        for rotation in rotations:
            allowed = _build_allowed_center_region_from_zones(
                rotation,
                usable_area,
                occupied_zones,
                clearance_mm=clearance_mm,
                nfp_cache=nfp_cache,
                merged_cache=merged_cache,
            )
            if allowed is not None and allowed.area > _FEASIBILITY_AREA_EPS_MM2:
                return True
    return False


def _ascending_unplaced_ids(
    unplaced_ids: list[str],
    parts_mm: dict[str, PartInput],
) -> list[str]:
    """Order unplaced parts smallest-first, the mirror of the main pass.

    The main loop always walks largest-first, which is why a single big
    remaining part failing to fit triggers ``_has_any_remaining_fit`` to give
    up on everything still queued behind it -- including small parts that
    were never actually tried against the exact free region a large-first
    pass leaves fragmented. Ascending area (ties broken by max side, then
    bbox area, matching the descending key's tie-break order) gives the
    smallest, most gap-friendly shapes first crack at whatever room remains.
    """
    def key(part_id: str) -> tuple[float, float, float, str]:
        shape = parts_mm[part_id].shape_mm
        minx, miny, maxx, maxy = shape.bounds
        width, height = maxx - minx, maxy - miny
        area = float(shape.area)
        max_side = max(width, height)
        bbox_area = width * height
        return area, max_side, bbox_area, part_id

    return sorted(unplaced_ids, key=key)


def _backfill_gaps(
    unplaced_ids: list[str],
    prepared_rotations: dict[str, tuple[_PreparedRotation, ...]],
    parts_mm: dict[str, PartInput],
    usable_area: Polygon,
    occupied_zones: list[_OccupiedZone],
    placed: list[PlacedPart],
    *,
    clearance_mm: float,
    nfp_cache: dict[tuple[bytes, bytes], BaseGeometry],
    placement_policy: str,
    merged_cache: dict[bytes, _MergedBlockedCache] | None = None,
    check_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    max_sweeps: int = 1,
    time_budget_seconds: float | None = None,
) -> list[str]:
    """Retry abandoned parts, smallest first, against the live free region.

    ``occupied_zones`` already carries the exact geometry the main loop used
    to prove a large part could not fit; that same geometry is reused here
    unchanged, so this is additive placement, not a fresh layout. Every
    placement can only shrink the remaining free area, so a part that fails
    a sweep cannot spuriously succeed on the next one without another part
    having freed nothing new -- the loop is bounded by ``max_sweeps`` for
    safety, but in practice stops in one or two passes once a sweep places
    zero parts. Reuses ``nfp_cache`` and ``_place_one_part`` so shape-pair
    Minkowski sums already computed in the main pass are cache hits here,
    keeping the added cost proportional to the (typically small) unplaced
    count rather than a re-analysis of the whole job.

    ``time_budget_seconds``, when given, bounds the WHOLE sweep loop by wall
    clock rather than by part count. Unlike the main pass, which never uses
    the exact-NFP/``merged_cache`` machinery at all (it runs on the cheap
    ``STRtree``-based fast path), backfill always calls the exact path
    unconditionally -- so the very first backfill part can be the first time
    in the whole run that a per-rotation ``unary_union`` over every occupied
    zone is paid for at all, for as many as 24 rotations. That one-time
    warm-up cost, multiplied by however many distinct remaining shapes exist,
    is what previously made a sweep look permanently stuck: no bug, just an
    unbounded amount of legitimate, expensive geometry work with no ceiling.
    The deadline is soft and checked between rotations/parts, never inside a
    single GEOS call, so it can only shorten how many more attempts are made
    -- it can never affect the correctness or clearance of an already-
    accepted placement.
    """
    still_unplaced = list(unplaced_ids)
    deadline = (
        time.monotonic() + time_budget_seconds if time_budget_seconds is not None else None
    )
    for sweep in range(max_sweeps):
        if not still_unplaced:
            break
        logger.info(
            "backfill sweep=%d/%d unplaced=%d placed=%d",
            sweep + 1, max_sweeps, len(still_unplaced), len(placed),
        )
        ordered = _ascending_unplaced_ids(still_unplaced, parts_mm)
        next_round: list[str] = []
        placed_this_sweep = 0
        timed_out = False
        for idx, part_id in enumerate(ordered):
            if check_cancelled and check_cancelled():
                raise NestingCancelledError("تم إلغاء عملية الترتيب من قبل المستخدم.")
            if deadline is not None and time.monotonic() >= deadline:
                # Budget exhausted: stop trying more parts THIS sweep rather
                # than burning more wall clock on work that already looked
                # slow. Every part from here on stays unplaced, same as if
                # this sweep genuinely could not fit them.
                logger.info(
                    "backfill time_budget_exceeded sweep=%d tried=%d/%d placed_this_sweep=%d",
                    sweep + 1, idx, len(ordered), placed_this_sweep,
                )
                next_round.extend(ordered[idx:])
                timed_out = True
                break
            if on_progress and idx % 5 == 0:
                on_progress(idx, len(ordered), len(placed))
            result = _place_one_part(
                prepared_rotations[part_id],
                usable_area,
                occupied_zones,
                clearance_mm=clearance_mm,
                nfp_cache=nfp_cache,
                placement_policy=placement_policy,
                merged_cache=merged_cache,
                deadline=deadline,
                source_shape_mm=parts_mm[part_id].shape_mm,
            )
            if result is None:
                next_round.append(part_id)
                continue
            # _place_one_part is always called above with source_shape_mm set,
            # so a non-None result here is always the 4-tuple form whose 4th
            # element is the winning _PreparedRotation itself -- see the
            # matching fix (and its full rationale) in run_nesting above.
            # Using it directly avoids the StopIteration a by-angle lookup
            # into prepared_rotations[part_id] would raise whenever refinement
            # picks a FINE_* winning angle, since prepared_rotations only ever
            # holds the 24 coarse ALL_ROTATIONS members.
            angle, final_shape, center, winning_rotation = result
            part_input = parts_mm[part_id]
            placed.append(
                PlacedPart(
                    part_id=part_id,
                    source_image_path=part_input.source_image_path,
                    placed_shape_mm=final_shape,
                    rotation=angle,
                    source_centroid_px=part_input.source_centroid_px,
                    alpha_bbox_px=part_input.alpha_bbox_px,
                )
            )
            occupied_zones.append(
                _OccupiedZone(
                    rotation=winning_rotation,
                    center_x_mm=center[0],
                    center_y_mm=center[1],
                )
            )
            placed_this_sweep += 1
        still_unplaced = next_round
        logger.info(
            "backfill sweep=%d done placed_this_sweep=%d still_unplaced=%d",
            sweep + 1, placed_this_sweep, len(still_unplaced),
        )
        if timed_out or placed_this_sweep == 0:
            break
    return still_unplaced


def run_nesting(
    parts_mm: dict[str, PartInput],
    sheet_width_mm: float,
    sheet_height_mm: float,
    *,
    sheet_margin_mm: float = 5.0,
    clearance_mm: float = CLEARANCE_MM,
    packing_strategy: str = "area_desc",
    placement_policy: str = "bottom_left",
    check_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    backfill_time_budget_seconds: float | None = None,
) -> NestingResult:
    if not parts_mm:
        raise NestingError("لا يوجد أي أشكال للترتيب.")
    if sheet_width_mm <= 0 or sheet_height_mm <= 0:
        raise NestingError("أبعاد الشيت يجب أن تكون أكبر من صفر.")
    if sheet_margin_mm < 0:
        raise NestingError("sheet_margin_mm لا يمكن أن يكون سالباً.")
    if clearance_mm <= 0:
        raise NestingError("clearance_mm يجب أن يكون أكبر من صفر.")
    usable_area = _sheet_polygon(sheet_width_mm, sheet_height_mm, sheet_margin_mm)
    if usable_area.is_empty or usable_area.area <= 0:
        raise NestingError("الهامش كبير جداً مقارنة بأبعاد الشيت.")

    ordered_ids = _ordered_part_ids(parts_mm, packing_strategy)
    use_fast_candidate_path = _should_use_fast_candidate_path(parts_mm)
    # Identical uploaded silhouettes are common in print jobs.  Keying by WKB
    # lets all their copies share the exact four prepared rotation meshes.
    # The value contains only immutable Shapely/Numpy data and is local to this
    # compute call, so no job can leak geometry or memory into another job.
    rotations_by_shape: dict[bytes, tuple[_PreparedRotation, ...]] = {}
    prepared_rotations: dict[str, tuple[_PreparedRotation, ...]] = {}
    import shapely
    for part_id in ordered_ids:
        shape = parts_mm[part_id].shape_mm
        try:
            # Cluster identical shapes that differ by microscopic floating point drift
            # (e.g. from 151 individual contour extractions of the same image)
            # by rounding coordinates to 0.01 mm before generating the cache key.
            shape_key = shapely.set_precision(shape, 0.01).wkb
        except Exception:
            shape_key = repr(shape).encode()
        rotations = rotations_by_shape.get(shape_key)
        if rotations is None:
            rotations = _prepare_rotations(shape)
            rotations_by_shape[shape_key] = rotations
        prepared_rotations[part_id] = rotations

    placed: list[PlacedPart] = []
    unplaced: list[str] = []
    occupied_zones: list[_OccupiedZone] = []
    nfp_cache: dict[tuple[bytes, bytes], BaseGeometry] = {}
    # Remembers, per rotation, how many leading zones of occupied_zones (or
    # of the fast-path's rebuilt backfill_zones, later) have already been
    # folded into a running merged-blocked geometry -- see
    # _build_allowed_center_region_from_zones for why this is safe and what
    # it eliminates. One instance for the whole call, exactly like nfp_cache.
    merged_cache: dict[bytes, _MergedBlockedCache] = {}
    total = len(ordered_ids)
    processed = 0
    sheet_full = False

    logger.info(
        "run_nesting started total_parts=%d strategy=%s",
        total,
        "exact_nfp" if not use_fast_candidate_path else "exactly_validated_candidates",
    )

    for loop_index, part_id in enumerate(ordered_ids):
        if check_cancelled and check_cancelled():
            raise NestingCancelledError("تم إلغاء عملية الترتيب من قبل المستخدم.")

        part_input = parts_mm[part_id]
        if use_fast_candidate_path:
            result = _place_one_part_fast(
                prepared_rotations[part_id],
                usable_area,
                placed,
                clearance_mm=clearance_mm + _ENGINE_SAFETY_MARGIN_MM,
                placement_policy=placement_policy,
            )
        else:
            result = _place_one_part(
                prepared_rotations[part_id],
                usable_area,
                occupied_zones,
                clearance_mm=clearance_mm + _ENGINE_SAFETY_MARGIN_MM,
                nfp_cache=nfp_cache,
                placement_policy=placement_policy,
                merged_cache=merged_cache,
                source_shape_mm=part_input.shape_mm,
            )
        processed = loop_index + 1

        if result is None:
            unplaced.append(part_id)
            if not use_fast_candidate_path and loop_index < total - 1:
                remaining_ids = ordered_ids[loop_index + 1 :]
                # Do not stop merely because this one piece failed. Stop only
                # when a positive-area fit for *every* remaining shape is gone.
                if not _has_any_remaining_fit(
                    remaining_ids,
                    prepared_rotations,
                    usable_area,
                    occupied_zones,
                    clearance_mm=clearance_mm + _ENGINE_SAFETY_MARGIN_MM,
                    nfp_cache=nfp_cache,
                    merged_cache=merged_cache,
                    check_cancelled=check_cancelled,
                ):
                    unplaced.extend(remaining_ids)
                    processed = loop_index + 1
                    sheet_full = True
                    if on_progress:
                        on_progress(processed, total, len(placed))
                    break
        else:
            # BUG (found and fixed here): the branch above (result is not
            # None) previously ALWAYS unpacked `result` as the 4-tuple
            # (angle, final_shape, center, winning_rotation) that only
            # _place_one_part returns (see its docstring: called with
            # source_shape_mm set, enabling Fine Rotation Refinement, whose
            # 4th element -- the winning _PreparedRotation itself -- must be
            # used directly rather than looked up from prepared_rotations by
            # angle, since a refined winner's angle can be a FINE_* member
            # prepared_rotations' coarse-only table does not contain).
            #
            # But `result` here can ALSO come from _place_one_part_fast (line
            # ~1412, taken whenever use_fast_candidate_path is True), whose
            # own declared return type is the plain 3-tuple
            # tuple[LockedRotation, BaseGeometry, tuple[float, float]] | None
            # -- it has no Fine Rotation Refinement and never returns a 4th
            # element. Unpacking that 3-tuple into 4 names raised
            # `ValueError: not enough values to unpack (expected 4, got 3)`
            # on the FIRST successful fast-path placement of every job whose
            # geometry crosses _FAST_PATH_MAX_VERTICES/_FAST_PATH_TOTAL_VERTICES
            # (confirmed by direct reproduction: a 2-part job with ~600k/500k
            # vertices raised this exact ValueError before this fix, and ran
            # to a clean SUCCESS after it). Every real stored production job
            # under backend/jobs/ stays under that vertex threshold today, so
            # this was latent rather than user-visible yet -- but any upload
            # complex/detailed enough to cross it (e.g. a highly detailed
            # traced image or complex SVG that geometry/contour.py's
            # simplify() does not reduce enough) would 500 on first compute.
            #
            # Fix: branch on use_fast_candidate_path (already in scope, the
            # same flag that chose which function to call above) to unpack
            # the arity that function actually returns, matching the
            # existing correct pattern already used lower in this same file
            # for the fast-path backfill sweep (`angle, final_shape, center =
            # result` there, vs. lns.py's `_repair` which applies the
            # identical use_fast_candidate_path-gated unpack for the same
            # reason). occupied_zones is only ever appended to in the exact
            # (non-fast) branch -- unchanged -- since the fast path tracks
            # occupancy via `placed`/STRtree instead (see _place_one_part_fast
            # and _find_fast_placement above), so winning_rotation is simply
            # never produced or needed on that path.
            if use_fast_candidate_path:
                angle, final_shape, center = result
            else:
                angle, final_shape, center, winning_rotation = result
            placed.append(
                PlacedPart(
                    part_id=part_id,
                    source_image_path=part_input.source_image_path,
                    placed_shape_mm=final_shape,
                    rotation=angle,
                    source_centroid_px=part_input.source_centroid_px,
                    alpha_bbox_px=part_input.alpha_bbox_px,
                )
            )
            if not use_fast_candidate_path:
                occupied_zones.append(
                    _OccupiedZone(
                        rotation=winning_rotation,
                        center_x_mm=center[0],
                        center_y_mm=center[1],
                    )
                )

        if on_progress:
            on_progress(processed, total, len(placed))
        # The client already receives a throttled live stream. Logging every
        # one of hundreds of parts forces needless UI log updates and retains
        # long file-related diagnostics; checkpoints are enough for support.
        if processed == 1 or processed == total or processed % 25 == 0:
            logger.info(
                "run_nesting progress=%d/%d placed=%d unplaced=%d",
                processed, total, len(placed), len(unplaced),
            )

    if unplaced:
        # The main pass abandons every remaining part the instant the
        # largest one can no longer fit, which is provably correct for that
        # part but throws away smaller remaining parts that were never
        # individually tried against the exact free region. Retry them here,
        # smallest first, directly against the same occupied geometry.
        #
        # The fast candidate path never populates occupied_zones (it derives
        # its own bounding-box spatial index per call instead), so rebuild it
        # from what is actually on the sheet before reusing the exact-NFP
        # backfill primitives -- otherwise the backfill would not know a fast
        # path placement is there and could place on top of it.
        backfill_zones = occupied_zones
        if use_fast_candidate_path and placed:
            backfill_zones = []
            for placed_part in placed:
                rotation = next(
                    item
                    for item in prepared_rotations[placed_part.part_id]
                    if item.angle == placed_part.rotation
                )
                bounds = placed_part.placed_shape_mm.bounds
                centered_bounds = rotation.centered_shape_mm.bounds
                backfill_zones.append(
                    _OccupiedZone(
                        rotation=rotation,
                        center_x_mm=bounds[0] - centered_bounds[0],
                        center_y_mm=bounds[1] - centered_bounds[1],
                    )
                )

        logger.info(
            "run_nesting starting backfill unplaced=%d placed=%d",
            len(unplaced), len(placed),
        )
        if on_progress:
            on_progress(processed, total, len(placed))

        def _backfill_progress(bf_done: int, bf_total: int, bf_placed: int) -> None:
            """Report backfill progress to the UI via the same on_progress callback."""
            if on_progress:
                on_progress(processed, total, bf_placed)

        if use_fast_candidate_path:
            # Use the fast path for backfill too -- the exact NFP path rebuilds
            # unary_union over all occupied zones for every rotation of every
            # part, which with 90+ placed parts is extremely slow (only 3 parts
            # tried in 60 seconds). The fast path with our dense interior grid
            # candidate generation tries all remaining parts in seconds.
            backfill_ordered = _ascending_unplaced_ids(unplaced, parts_mm)
            backfill_deadline = (
                time.monotonic() + backfill_time_budget_seconds
                if backfill_time_budget_seconds is not None
                else None
            )
            still_unplaced: list[str] = []
            for sweep in range(3):  # up to 3 sweeps
                placed_this_sweep = 0
                next_round: list[str] = []
                for order_index, part_id in enumerate(backfill_ordered):
                    if check_cancelled and check_cancelled():
                        raise NestingCancelledError("تم إلغاء عملية الترتيب من قبل المستخدم.")
                    if backfill_deadline is not None and time.monotonic() >= backfill_deadline:
                        next_round.extend(backfill_ordered[order_index:])
                        break
                    result = _place_one_part_fast(
                        prepared_rotations[part_id],
                        usable_area,
                        placed,
                        clearance_mm=clearance_mm + _ENGINE_SAFETY_MARGIN_MM,
                        placement_policy=placement_policy,
                        deadline=backfill_deadline,
                    )
                    if result is None:
                        next_round.append(part_id)
                        continue
                    angle, final_shape, center = result
                    part_input = parts_mm[part_id]
                    placed.append(
                        PlacedPart(
                            part_id=part_id,
                            source_image_path=part_input.source_image_path,
                            placed_shape_mm=final_shape,
                            rotation=angle,
                            source_centroid_px=part_input.source_centroid_px,
                            alpha_bbox_px=part_input.alpha_bbox_px,
                        )
                    )
                    placed_this_sweep += 1
                logger.info(
                    "backfill sweep=%d done placed_this_sweep=%d still_unplaced=%d",
                    sweep + 1, placed_this_sweep, len(next_round),
                )
                if placed_this_sweep == 0 or not next_round:
                    still_unplaced = next_round
                    break
                backfill_ordered = next_round
                still_unplaced = next_round
            unplaced = still_unplaced
        else:
            unplaced = _backfill_gaps(
                unplaced,
                prepared_rotations,
                parts_mm,
                usable_area,
                backfill_zones,
                placed,
                clearance_mm=clearance_mm + _ENGINE_SAFETY_MARGIN_MM,
                nfp_cache=nfp_cache,
                placement_policy=placement_policy,
                merged_cache=merged_cache,
                check_cancelled=check_cancelled,
                on_progress=_backfill_progress,
                time_budget_seconds=backfill_time_budget_seconds,
            )
        # processed_count keeps its original meaning -- where the main
        # sequential pass stopped -- untouched by the backfill sweep, which is
        # an additional retry pass rather than a continuation of that count.
        if on_progress:
            on_progress(processed, total, len(placed))
        logger.info(
            "run_nesting backfill placed=%d unplaced=%d",
            len(placed), len(unplaced),
        )

    sheet_full = bool(unplaced)

    return NestingResult(
        placed=placed,
        unplaced_part_ids=unplaced,
        sheet_full=sheet_full,
        processed_count=processed,
        total_count=total,
    )


def _single_sheet_quality(page: NestingResult) -> tuple[int, float, float, float]:
    """Rank valid one-sheet candidates by capacity, then compactness."""
    if not page.placed:
        return 0, float("-inf"), float("-inf"), float("-inf")
    bounds = [part.placed_shape_mm.bounds for part in page.placed]
    max_y = max(bounds_item[3] for bounds_item in bounds)
    max_x = max(bounds_item[2] for bounds_item in bounds)
    min_y = min(bounds_item[1] for bounds_item in bounds)
    min_x = min(bounds_item[0] for bounds_item in bounds)
    # Capacity is the decisive criterion. Compactness only breaks a tie so
    # mixed-size jobs retain the largest possible contiguous empty region.
    return len(page.placed), -(max_y - min_y), -(max_x - min_x), -max_y


def run_best_single_sheet_nesting(
    parts_mm: dict[str, PartInput],
    sheet_width_mm: float,
    sheet_height_mm: float,
    *,
    sheet_margin_mm: float = 5.0,
    clearance_mm: float = CLEARANCE_MM,
    packing_attempts: int = 1,
    check_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_attempt_progress: Callable[[int, int, int, int, int], None] | None = None,
    backfill_time_budget_seconds: float | None = 45.0,
) -> NestingResult:
    """Find the largest valid subset that fits on exactly one physical sheet.

    The desktop workflow deliberately exports one TIFF page.  We therefore
    try several exact ordering strategies on that *same* sheet and retain the
    candidate with the most parts; anything else is explicitly returned as
    unplaced instead of being silently pushed onto another page.

    ``on_progress`` keeps its original 3-argument signature and is invoked
    only once per completed attempt, for any existing caller that does not
    care about live per-part progress.  ``on_attempt_progress``, when given,
    is forwarded straight into every inner ``run_nesting`` call so a caller
    can report live progress *within* the current attempt, plus which
    attempt (1-indexed) out of how many total attempts is currently running
    — the previous behaviour reported no progress at all until an entire
    attempt of every part finished, which is what made a long run of many
    parts look stalled after the first one completed.

    ``backfill_time_budget_seconds`` bounds each attempt's backfill sweep by
    wall clock (default 45s, matching the scale of the existing LNS stage's
    own ``time_budget_seconds`` further up the pipeline in main.py). Backfill
    always uses the exact-NFP path regardless of which path the main pass
    used, so with hundreds of already-occupied zones its very first part can
    be expensive; without a ceiling that cost has no bound at all. Pass
    ``None`` to disable the budget entirely (unbounded, previous behaviour).
    """
    if packing_attempts < 1:
        raise NestingError("packing_attempts يجب أن يكون أكبر من صفر.")

    candidates: list[NestingResult] = []
    total = len(parts_mm)
    attempt_total = min(packing_attempts, len(_PACKING_STRATEGIES))
    for attempt_index, (strategy, policy) in enumerate(_PACKING_STRATEGIES[:packing_attempts], start=1):
        if check_cancelled and check_cancelled():
            raise NestingCancelledError("تم إلغاء عملية الترتيب من قبل المستخدم.")

        inner_progress: Callable[[int, int, int], None] | None = None
        if on_attempt_progress is not None:
            def inner_progress(
                done: int,
                part_total: int,
                placed_count: int,
                _attempt_index: int = attempt_index,
                _attempt_total: int = attempt_total,
            ) -> None:
                on_attempt_progress(done, part_total, placed_count, _attempt_index, _attempt_total)

        candidate = run_nesting(
            parts_mm,
            sheet_width_mm,
            sheet_height_mm,
            sheet_margin_mm=sheet_margin_mm,
            clearance_mm=clearance_mm,
            packing_strategy=strategy,
            placement_policy=policy,
            check_cancelled=check_cancelled,
            on_progress=inner_progress,
            backfill_time_budget_seconds=backfill_time_budget_seconds,
        )
        candidates.append(candidate)
        if on_progress:
            best_so_far = max(len(page.placed) for page in candidates)
            on_progress(total, total, best_so_far)

    return max(candidates, key=_single_sheet_quality)


def run_multi_sheet_nesting(
    parts_mm: dict[str, PartInput],
    sheet_width_mm: float,
    sheet_height_mm: float,
    *,
    sheet_margin_mm: float = 5.0,
    clearance_mm: float = CLEARANCE_MM,
    packing_attempts: int = 1,
    check_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> MultiSheetNestingResult:
    """Fill as many sheets as necessary without ever mixing their geometry.

    A layout on every new sheet starts from a completely empty canvas.  This
    guarantees that a contour's coordinates and clearance are checked only
    against parts on the *same* physical sheet.  At least one part is removed
    from ``remaining`` on every successful pass, so the loop terminates even
    for a job containing a shape that cannot fit on an empty sheet.
    """
    if packing_attempts < 1:
        raise NestingError("packing_attempts يجب أن يكون أكبر من صفر.")
    remaining = dict(parts_mm)
    total = len(remaining)
    sheets: list[NestingResult] = []
    placed_total = 0

    while remaining:
        if check_cancelled and check_cancelled():
            raise NestingCancelledError("تم إلغاء عملية الترتيب من قبل المستخدم.")
        page_number = len(sheets) + 1
        logger.info(
            "run_multi_sheet_nesting started sheet=%d remaining=%d total=%d",
            page_number,
            len(remaining),
            total,
        )

        candidates: list[NestingResult] = []
        for strategy, policy in _PACKING_STRATEGIES[:packing_attempts]:
            if check_cancelled and check_cancelled():
                raise NestingCancelledError("تم إلغاء عملية الترتيب من قبل المستخدم.")
            candidates.append(
                run_nesting(
                    remaining,
                    sheet_width_mm,
                    sheet_height_mm,
                    sheet_margin_mm=sheet_margin_mm,
                    clearance_mm=clearance_mm,
                    packing_strategy=strategy,
                    placement_policy=policy,
                    check_cancelled=check_cancelled,
                )
            )
            if on_progress:
                best_so_far = max(len(candidate.placed) for candidate in candidates)
                on_progress(
                    placed_total + best_so_far,
                    total,
                    placed_total + best_so_far,
                )

        page = max(candidates, key=_single_sheet_quality)
        if on_progress:
            on_progress(placed_total + len(page.placed), total, placed_total + len(page.placed))

        if not page.placed:
            logger.info(
                "run_multi_sheet_nesting stopped sheet=%d unplaceable=%d",
                page_number,
                len(remaining),
            )
            return MultiSheetNestingResult(
                sheets=sheets,
                unplaced_part_ids=list(remaining),
                total_count=total,
            )

        sheets.append(page)
        for placed in page.placed:
            remaining.pop(placed.part_id, None)
        placed_total += len(page.placed)
        if on_progress:
            on_progress(placed_total, total, placed_total)
        logger.info(
            "run_multi_sheet_nesting completed sheet=%d placed=%d remaining=%d",
            page_number,
            len(page.placed),
            len(remaining),
        )

    return MultiSheetNestingResult(sheets=sheets, total_count=total)
