"""Independent exact collision validation with spatial-index acceleration."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from app.core_logging import get_logger
from app.geometry.clearance import CLEARANCE_MM, minimum_distance_mm
from app.nesting.engine import PlacedPart

logger = get_logger(__name__)
_FLOATING_POINT_TOLERANCE_MM = 1e-6
_SPATIAL_FILTER_SAFETY_MARGIN_MM = 0.01
# GEOS's exact-NFP triangulated-Minkowski-sum path (see nfp.py's own module
# docstring) can report a clearance distance a few microns short of the true
# geometric requirement from floating-point accumulation across many small
# triangle-pair unions -- not a real physical overlap. benchmark.py's
# KnownGeosLimitation independently measured this exact shortfall across many
# seeds (consistently under ~1.3 microns) and validated 10 microns as a
# tolerance wide enough to absorb every measured instance with margin, while
# remaining far too small to misclassify a real, visible clearance violation.
# Reusing that same validated value here -- rather than
# _FLOATING_POINT_TOLERANCE_MM above, which stays reserved for true
# zero-area/zero-distance noise in the overlap check below -- is what lets
# production /layout/compute accept a layout that is geometrically sound in
# every practical sense, instead of rejecting it outright for numerical noise
# smaller than any achievable physical cutting/printing tolerance.
_GEOS_CLEARANCE_BOUNDARY_TOLERANCE_MM = 0.01


class ValidationSeverity:
    OVERLAP = "overlap"
    CLEARANCE_VIOLATION = "clearance_violation"
    OUT_OF_BOUNDS = "out_of_bounds"


@dataclass(frozen=True, slots=True)
class ValidationViolation:
    severity: str
    part_id_a: str
    part_id_b: str | None
    detail: str
    measured_distance_mm: float | None = None


@dataclass(frozen=True, slots=True)
class CollisionReport:
    violations: list[ValidationViolation] = field(default_factory=list)
    checked_pairs_count: int = 0

    @property
    def is_valid(self) -> bool:
        return not self.violations


def _candidate_query_box(shape: BaseGeometry, threshold: float) -> Polygon:
    minx, miny, maxx, maxy = shape.bounds
    return box(minx - threshold, miny - threshold, maxx + threshold, maxy + threshold)


def validate_layout(
    placed_parts: list[PlacedPart],
    sheet_width_mm: float,
    sheet_height_mm: float,
    sheet_margin_mm: float,
    *,
    clearance_mm: float = CLEARANCE_MM,
) -> CollisionReport:
    if sheet_width_mm <= 0 or sheet_height_mm <= 0:
        raise ValueError("أبعاد الشيت يجب أن تكون أكبر من صفر.")
    if sheet_margin_mm < 0:
        raise ValueError("sheet_margin_mm لا يمكن أن يكون سالباً.")
    if clearance_mm <= 0:
        raise ValueError("clearance_mm يجب أن يكون أكبر من صفر.")

    started = time.perf_counter()
    violations: list[ValidationViolation] = []
    printable_area = Polygon.from_bounds(
        sheet_margin_mm,
        sheet_margin_mm,
        sheet_width_mm - sheet_margin_mm,
        sheet_height_mm - sheet_margin_mm,
    )

    # A placement can sit exactly on the printable edge.  GEOS coordinates are
    # doubles, so an algebraically exact translation may read as 2.0 - 9e-16.
    # Allow only the existing numerical tolerance at the boundary; this does
    # not enlarge the physical sheet or relax a measurable out-of-bounds case.
    printable_area_with_tolerance = printable_area.buffer(_FLOATING_POINT_TOLERANCE_MM)
    for part in placed_parts:
        if not printable_area_with_tolerance.covers(part.placed_shape_mm):
            outside_area = part.placed_shape_mm.difference(printable_area).area
            violations.append(
                ValidationViolation(
                    ValidationSeverity.OUT_OF_BOUNDS,
                    part.part_id,
                    None,
                    f"القطعة '{part.part_id}' تتجاوز حدود الشيت القابلة للطباعة بمساحة {outside_area:.4f}mm².",
                )
            )

    if len(placed_parts) < 2:
        return CollisionReport(violations=violations, checked_pairs_count=0)

    shapes = [p.placed_shape_mm for p in placed_parts]
    tree = STRtree(shapes)
    threshold = clearance_mm + _SPATIAL_FILTER_SAFETY_MARGIN_MM
    seen: set[tuple[int, int]] = set()
    checked = 0

    for i, part_a in enumerate(placed_parts):
        query_indices = tree.query(_candidate_query_box(shapes[i], threshold))
        for raw_j in query_indices:
            j = int(raw_j)
            if j <= i:
                continue
            pair = (i, j)
            if pair in seen:
                continue
            seen.add(pair)
            shape_a = shapes[i]
            shape_b = shapes[j]

            # The index is only a broad spatial prefilter.  All final decisions
            # below are exact GEOS geometry predicates/distance calculations.
            checked += 1
            if shape_a.intersects(shape_b):
                overlap = shape_a.intersection(shape_b)
                if not overlap.is_empty and overlap.area > _FLOATING_POINT_TOLERANCE_MM:
                    violations.append(
                        ValidationViolation(
                            ValidationSeverity.OVERLAP,
                            part_a.part_id,
                            placed_parts[j].part_id,
                            f"تداخل هندسي حقيقي بين '{part_a.part_id}' و '{placed_parts[j].part_id}' بمساحة {overlap.area:.4f}mm².",
                            0.0,
                        )
                    )
                    continue

            distance = minimum_distance_mm(shape_a, shape_b)
            if distance < clearance_mm - _GEOS_CLEARANCE_BOUNDARY_TOLERANCE_MM:
                violations.append(
                    ValidationViolation(
                        ValidationSeverity.CLEARANCE_VIOLATION,
                        part_a.part_id,
                        placed_parts[j].part_id,
                        f"المسافة بين '{part_a.part_id}' و '{placed_parts[j].part_id}' = {distance:.6f}mm، أقل من {clearance_mm}mm.",
                        distance,
                    )
                )

    logger.info(
        "validate_layout total_parts=%d precise_pairs=%d violations=%d elapsed_ms=%.1f",
        len(placed_parts), checked, len(violations), (time.perf_counter() - started) * 1000,
    )
    return CollisionReport(violations=violations, checked_pairs_count=checked)
