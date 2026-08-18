"""Free-space classification and multi-term layout scoring.

Spec sections 3 (gap/pocket classification) and 9 (multi-term objective).
This module is purely additive: it reads PlacedPart/_OccupiedZone/_sheet_polygon
from engine.py but does not modify placement behaviour anywhere.  It exists so
a future LNS/compaction stage has a principled way to compare two candidate
layouts, instead of the current placed-count-then-bbox-tiebreak heuristic in
_single_sheet_quality.

No new dependency is introduced.  Connected-component and enclosed-hole
detection is done with Shapely's own MultiPolygon.geoms / Polygon.interiors,
which is exact vector topology, not a raster or graph-library approximation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.nesting.engine import PlacedPart, _OccupiedZone

_AREA_EPS_MM2 = 1e-9


class MetricsError(Exception):
    pass


# ---------------------------------------------------------------------------
# Gap / pocket classification (spec section 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PocketInfo:
    """One connected free-space region on the usable sheet area.

    ``touches_boundary`` distinguishes a pocket reachable from the sheet's
    own outer edge (still potentially fillable by a part approaching from
    outside the occupied cluster) from a pocket that is fully enclosed by
    placed parts (an interior hole -- reachable only if some part is later
    removed or was never placed to begin with, i.e. a strong LNS destroy
    target in a later phase).

    ``max_inscribed_diameter_mm`` is a cheap, exact lower/upper-bound proxy
    for "can anything of a given size fit here" without running a full NFP
    search: the minimum of the pocket's own bounding-box width and height.
    A part whose smallest bounding dimension exceeds this can never fit
    inside the pocket (necessary, not sufficient, condition -- an irregular
    pocket can still reject a part that passes this check, but nothing that
    fails this check can ever be placed there). This keeps analyze_free_space
    itself O(pockets), not O(pockets x candidate parts).
    """

    polygon_mm: BaseGeometry
    area_mm2: float
    touches_boundary: bool
    max_inscribed_diameter_mm: float
    perimeter_mm: float

    @property
    def compactness(self) -> float:
        """Isoperimetric ratio in [0, 1]; 1.0 is a perfect circle.

        A low value means a long, thin, jagged pocket (hard to fill with
        anything but a matching sliver) as opposed to a chunky, round one
        (easy to fill with almost any small part). Using area/perimeter^2
        (normalised by 4*pi so a circle scores 1.0) rather than raw area
        alone is what lets the objective function later distinguish "one big
        useless crack" from "one big usable pocket" of the same area.
        """
        if self.perimeter_mm <= 0:
            return 0.0
        import math

        return max(
            0.0,
            min(1.0, (4.0 * math.pi * self.area_mm2) / (self.perimeter_mm ** 2)),
        )


@dataclass(frozen=True, slots=True)
class FreeSpaceAnalysis:
    """Full decomposition of a sheet's unused area into distinct pockets."""

    pockets: tuple[PocketInfo, ...] = field(default_factory=tuple)
    total_free_area_mm2: float = 0.0

    @property
    def pocket_count(self) -> int:
        return len(self.pockets)

    @property
    def largest_pocket_area_mm2(self) -> float:
        if not self.pockets:
            return 0.0
        return max(pocket.area_mm2 for pocket in self.pockets)

    @property
    def enclosed_pocket_count(self) -> int:
        """Pockets with no path to the sheet boundary (fully surrounded)."""
        return sum(1 for pocket in self.pockets if not pocket.touches_boundary)

    @property
    def fragmentation_index(self) -> float:
        """How spread out the free area is across many small pockets vs one big one.

        0.0 means all free area is in a single pocket (best case: one large
        contiguous region a future part can still use). Approaches 1.0 as
        free area splits into many small, individually-useless slivers.
        Defined as 1 - (largest_pocket_area / total_free_area); this is scale
        -- and pocket-count -- independent by construction, so it stays
        comparable across sheets of different sizes or different total gap
        area.
        """
        if self.total_free_area_mm2 <= _AREA_EPS_MM2:
            return 0.0
        return max(
            0.0,
            min(1.0, 1.0 - (self.largest_pocket_area_mm2 / self.total_free_area_mm2)),
        )


def _polygon_pieces(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return [poly for poly in geometry.geoms if isinstance(poly, Polygon) and poly.area > _AREA_EPS_MM2]
    return []


def _pocket_from_polygon(polygon: Polygon, usable_area: Polygon) -> PocketInfo:
    minx, miny, maxx, maxy = polygon.bounds
    max_inscribed_diameter = min(maxx - minx, maxy - miny)

    # A pocket "touches the boundary" of the usable area when its own
    # exterior ring shares any boundary with the usable area's exterior --
    # i.e. it is not a hole fully carved out of the interior. Comparing the
    # pocket's exterior to the usable_area's exterior (not to placed parts
    # directly) keeps this an exact topological test rather than a
    # tolerance-based distance check.
    touches_boundary = polygon.exterior.intersects(usable_area.exterior)

    return PocketInfo(
        polygon_mm=polygon,
        area_mm2=float(polygon.area),
        touches_boundary=touches_boundary,
        max_inscribed_diameter_mm=max(0.0, max_inscribed_diameter),
        perimeter_mm=float(polygon.exterior.length),
    )


def analyze_free_space(usable_area: Polygon, occupied_union: BaseGeometry | None) -> FreeSpaceAnalysis:
    """Decompose the unused portion of ``usable_area`` into connected pockets.

    ``occupied_union`` is the union of every placed part's clearance-expanded
    silhouette (i.e. the same blocked geometry the engine's own
    ``_build_allowed_center_region_from_zones`` already derives internally,
    passed in here rather than recomputed, so this function has no opinion
    about how occupancy was produced -- it works identically whether the
    caller used the exact NFP path's occupied_zones or the fast path's
    STRtree-based placed shapes).

    A ``MultiPolygon.geoms`` decomposition is exact connected-component
    labelling for planar regions -- two pockets are the same Shapely geometry
    element if and only if they are 4-connected (share an edge or vertex) in
    the continuous plane, which is the exact continuous-geometry analogue of
    raster connected-component labelling, without discretising to pixels.
    """
    if usable_area.is_empty or usable_area.area <= _AREA_EPS_MM2:
        return FreeSpaceAnalysis()

    free_region = usable_area if occupied_union is None else usable_area.difference(occupied_union)
    if free_region.is_empty or free_region.area <= _AREA_EPS_MM2:
        return FreeSpaceAnalysis()

    pieces = _polygon_pieces(free_region)
    pockets = tuple(_pocket_from_polygon(piece, usable_area) for piece in pieces)
    total_area = sum(pocket.area_mm2 for pocket in pockets)
    return FreeSpaceAnalysis(pockets=pockets, total_free_area_mm2=total_area)


def free_space_from_occupied_zones(
    usable_area: Polygon,
    occupied_zones: list[_OccupiedZone],
    *,
    clearance_mm: float,
) -> FreeSpaceAnalysis:
    """Convenience wrapper for the exact-NFP main loop's own zone bookkeeping.

    Rebuilds each zone's translated, clearance-expanded silhouette from its
    prepared rotation's centered shape (buffered by clearance_mm) the same
    way the engine's placement geometry treats "blocked" space, then unions
    them once. This intentionally recomputes rather than reuses the engine's
    internal merged_cache, since that cache is keyed per-rotation for a
    completely different purpose (candidate-region shrinking) and is not a
    general "all zones unioned" artifact; recomputation here is O(zones),
    called only when a caller wants a scoring snapshot, not on every
    placement attempt.
    """
    if not occupied_zones:
        return analyze_free_space(usable_area, None)

    from shapely.affinity import translate

    blocked_pieces: list[BaseGeometry] = []
    for zone in occupied_zones:
        translated = translate(
            zone.rotation.centered_shape_mm,
            xoff=zone.center_x_mm,
            yoff=zone.center_y_mm,
        )
        blocked_pieces.append(translated.buffer(clearance_mm, join_style="round"))
    occupied_union = unary_union(blocked_pieces)
    return analyze_free_space(usable_area, occupied_union)


def free_space_from_placed_parts(
    usable_area: Polygon,
    placed_parts: list[PlacedPart],
    *,
    clearance_mm: float,
) -> FreeSpaceAnalysis:
    """Convenience wrapper for the fast candidate path (no occupied_zones).

    Uses each part's already-placed final silhouette directly (placed_shape_mm
    is the real, exact rotated+translated contour, not an approximation),
    buffered by clearance_mm to match how the fast path's own
    _resolve_ambiguous_candidate enforces separation. This is the same
    treatment free_space_from_occupied_zones gives the exact path, so scores
    from either path are comparable on the same footing.
    """
    if not placed_parts:
        return analyze_free_space(usable_area, None)

    blocked_pieces = [
        part.placed_shape_mm.buffer(clearance_mm, join_style="round") for part in placed_parts
    ]
    occupied_union = unary_union(blocked_pieces)
    return analyze_free_space(usable_area, occupied_union)


# ---------------------------------------------------------------------------
# Multi-term objective function (spec section 9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObjectiveWeights:
    """Centralised, documented weights for score_layout.

    Every weight here is a deliberate design choice, not an arbitrary
    default, and is meant to be tuned via the weight-sensitivity test in
    tests/test_metrics.py (spec section 9's explicit requirement to test
    sensitivity) rather than hand-picked once and forgotten:

    - ``placed_count``: dominant term.  Capacity (parts actually placed) is
      the primary business objective per the original user complaint ("117
      out of 153, but there's room for more") -- a layout that places fewer
      parts should essentially never outscore one that places more, so this
      weight is large relative to the others by construction.
    - ``utilization``: rewards a high placed-area / usable-area ratio
      directly, independent of pocket shape. Two layouts with the same
      placed_count can still differ here if one wastes more usable area
      overall (e.g. a wider bounding envelope).
    - ``fragmentation_penalty``: penalises spreading free space across many
      small pockets instead of one large contiguous one, since a single
      large pocket is more likely to be usable by a future part (LNS repair,
      or simply the next job) than the same area split into slivers.
    - ``enclosed_penalty``: penalises fully-enclosed (boundary-unreachable)
      pockets specifically, since these are inherently *worse* than a
      boundary-touching gap of the same area/shape -- nothing new can enter
      them without an existing part being removed first, which is precisely
      what a future LNS destroy operator would need to target.
    - ``compactness_bonus``: rewards round/chunky remaining free space over
      long thin cracks, using each pocket's isoperimetric ratio weighted by
      its area share of total free space -- this is what lets the score
      distinguish "one big round unused corner" (good, easy to fill later)
      from "one big jagged crack" (bad, likely permanently wasted) even when
      their raw areas are identical.
    """

    placed_count: float = 1000.0
    utilization: float = 100.0
    fragmentation_penalty: float = 20.0
    enclosed_penalty: float = 15.0
    compactness_bonus: float = 10.0


DEFAULT_OBJECTIVE_WEIGHTS = ObjectiveWeights()


@dataclass(frozen=True, slots=True)
class LayoutScore:
    """Breakdown of a single layout's score, for reporting and debugging.

    ``total`` is what a comparison/optimisation step should sort or select
    by; the individual terms are retained so a benchmark report (spec
    section 9's weight-sensitivity requirement, and the eventual statistical
    benchmark harness in a later phase) can show *why* one layout beat
    another, not just that it did.
    """

    total: float
    placed_count: int
    utilization_ratio: float
    fragmentation_index: float
    enclosed_pocket_count: int
    weighted_compactness: float
    free_space: FreeSpaceAnalysis


def score_layout(
    placed_parts: list[PlacedPart],
    usable_area_mm2: float,
    free_space: FreeSpaceAnalysis,
    *,
    weights: ObjectiveWeights = DEFAULT_OBJECTIVE_WEIGHTS,
) -> LayoutScore:
    """Combine placement capacity and free-space quality into one score.

    Higher is always better. This does not replace _single_sheet_quality's
    role as the tie-break used inside the existing 5-strategy search (that
    function's placed-count-first behaviour is exactly what the hard
    regression tests in test_nesting_capacity.py pin), but supersedes it for
    any future LNS/compaction stage that needs to compare two full candidate
    layouts (including ones with the SAME placed_count but different
    remaining-space quality), which _single_sheet_quality's coarse bbox
    tiebreak cannot meaningfully distinguish.

    placed_area is derived from each part's own placed_shape_mm.area rather
    than an assumed/uniform part size, so this is exact for irregular
    contours, not a bounding-box approximation.
    """
    if usable_area_mm2 <= _AREA_EPS_MM2:
        raise MetricsError("usable_area_mm2 يجب أن تكون أكبر من صفر لحساب score_layout.")

    placed_area = sum(float(part.placed_shape_mm.area) for part in placed_parts)
    utilization_ratio = max(0.0, min(1.0, placed_area / usable_area_mm2))

    fragmentation_index = free_space.fragmentation_index
    enclosed_count = free_space.enclosed_pocket_count

    if free_space.total_free_area_mm2 > _AREA_EPS_MM2:
        weighted_compactness = sum(
            pocket.compactness * (pocket.area_mm2 / free_space.total_free_area_mm2)
            for pocket in free_space.pockets
        )
    else:
        # No free space left at all is the best possible outcome for every
        # free-space-quality term; a fully-saturated sheet should never be
        # penalised for having nothing left to classify.
        weighted_compactness = 1.0

    total = (
        weights.placed_count * len(placed_parts)
        + weights.utilization * utilization_ratio
        - weights.fragmentation_penalty * fragmentation_index
        - weights.enclosed_penalty * enclosed_count
        + weights.compactness_bonus * weighted_compactness
    )

    return LayoutScore(
        total=total,
        placed_count=len(placed_parts),
        utilization_ratio=utilization_ratio,
        fragmentation_index=fragmentation_index,
        enclosed_pocket_count=enclosed_count,
        weighted_compactness=weighted_compactness,
        free_space=free_space,
    )
