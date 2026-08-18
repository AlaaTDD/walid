"""Final independent QA after TIFF export."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path

from psdtags import TiffImageSourceData
from shapely.geometry import Polygon, box
from shapely.strtree import STRtree

from app.geometry.units import MM_PER_INCH, Resolution
from app.image_safety import open_image_with_limit
from app.nesting.engine import PlacedPart

_FLOATING_POINT_TOLERANCE_MM = 1e-6
_DPI_TOLERANCE = 1e-6
# Same GEOS numerical-boundary tolerance as collision.py's
# _GEOS_CLEARANCE_BOUNDARY_TOLERANCE_MM (see that module for the full
# rationale and benchmark.py's KnownGeosLimitation measurements). This final
# independent QA re-check must use the identical tolerance for the identical
# reason, or a layout /layout/compute already accepted could still be
# rejected here afterward for the same sub-2-micron noise, after the TIFF has
# already been exported to disk.
_GEOS_CLEARANCE_BOUNDARY_TOLERANCE_MM = 0.01


class QaCheckError(Exception):
    pass


class QaSeverity:
    DPI_MISMATCH = "dpi_mismatch"
    DIMENSION_MISMATCH = "dimension_mismatch"
    CLEARANCE_VIOLATION = "clearance_violation"
    OVERLAP = "overlap"
    MISSING_ICC_PROFILE = "missing_icc_profile"
    INVALID_MODE = "invalid_mode"
    INVALID_LAYERS = "invalid_layers"
    FILE_UNREADABLE = "file_unreadable"


@dataclass(frozen=True, slots=True)
class QaViolation:
    severity: str
    detail: str
    expected: str | None = None
    actual: str | None = None


@dataclass(frozen=True, slots=True)
class QaReport:
    file_path: str
    violations: list[QaViolation] = field(default_factory=list)
    checked_dimension: bool = False
    checked_dpi: bool = False
    checked_clearance_pairs: int = 0
    checked_icc_and_mode: bool = False
    checked_layers: bool = False
    page_count: int = 1

    @property
    def is_valid(self) -> bool:
        return (
            self.checked_dimension
            and self.checked_dpi
            and self.checked_icc_and_mode
            and self.checked_layers
            and not self.violations
        )


def _expected_canvas_size_px(width_mm: float, height_mm: float, dpi: float) -> tuple[int, int]:
    px_per_mm = dpi / MM_PER_INCH
    return ceil(width_mm * px_per_mm), ceil(height_mm * px_per_mm)


def _check_dimensions_and_dpi(
    tiff_path: Path,
    sheet_width_mm: float,
    sheet_height_mm: float,
    expected_dpi: float,
    violations: list[QaViolation],
) -> tuple[bool, bool]:
    try:
        expected_width, expected_height = _expected_canvas_size_px(
            sheet_width_mm, sheet_height_mm, expected_dpi
        )
        with open_image_with_limit(
            tiff_path,
            max_pixels=expected_width * expected_height,
        ) as image:
            frame_count = getattr(image, "n_frames", 1)
            frames = []
            for page_index in range(frame_count):
                image.seek(page_index)
                frames.append((page_index + 1, image.size, image.info.get("dpi", (None, None))))
    except Exception as exc:
        violations.append(QaViolation(QaSeverity.FILE_UNREADABLE, f"فشل فتح TIFF للتحقق: {exc}"))
        return False, False

    ew, eh = expected_width, expected_height
    dimension_ok = True
    dpi_ok = True
    for page_number, (width, height), dpi in frames:
        if (width, height) != (ew, eh):
            dimension_ok = False
            violations.append(
                QaViolation(
                    QaSeverity.DIMENSION_MISMATCH,
                    f"أبعاد صفحة TIFF رقم {page_number} ({width}×{height}px) لا تطابق المتوقع ({ew}×{eh}px).",
                    f"{ew}×{eh}px",
                    f"{width}×{height}px",
                )
            )
        dx, dy = dpi
        if (
            dx is None or dy is None
            or abs(float(dx) - expected_dpi) > _DPI_TOLERANCE
            or abs(float(dy) - expected_dpi) > _DPI_TOLERANCE
        ):
            dpi_ok = False
            violations.append(
                QaViolation(
                    QaSeverity.DPI_MISMATCH,
                    f"DPI صفحة TIFF رقم {page_number} ({dx}, {dy}) لا يطابق المتوقع ({expected_dpi}, {expected_dpi}).",
                    f"({expected_dpi}, {expected_dpi})",
                    f"({dx}, {dy})",
                )
            )
    return dimension_ok, dpi_ok


def _check_icc_and_mode(
    tiff_path: Path,
    expected_modes: tuple[str, ...],
    expected_pixels: int,
    violations: list[QaViolation],
) -> bool:
    try:
        with open_image_with_limit(tiff_path, max_pixels=expected_pixels) as image:
            frame_count = getattr(image, "n_frames", 1)
            frames = []
            for page_index in range(frame_count):
                image.seek(page_index)
                frames.append((page_index + 1, image.mode, image.info.get("icc_profile")))
    except Exception as exc:
        violations.append(QaViolation(QaSeverity.FILE_UNREADABLE, f"فشل فتح TIFF للتحقق من ICC/mode: {exc}"))
        return False
    for page_number, mode, icc in frames:
        if mode not in expected_modes:
            violations.append(QaViolation(QaSeverity.INVALID_MODE, f"وضع ألوان الصفحة {page_number}: {mode!r} غير مسموح.", str(expected_modes), mode))
        if icc is None:
            violations.append(QaViolation(QaSeverity.MISSING_ICC_PROFILE, f"صفحة TIFF رقم {page_number} لا تحتوي على ICC profile."))
    return True


def _check_editable_layers(
    tiff_path: Path,
    expected_pixels: int,
    expected_page_count: int,
    violations: list[QaViolation],
) -> bool:
    """Ensure every TIFF page has Photoshop-compatible editable layer data."""
    try:
        with open_image_with_limit(tiff_path, max_pixels=expected_pixels) as image:
            frame_count = getattr(image, "n_frames", 1)
            if frame_count != expected_page_count:
                violations.append(
                    QaViolation(
                        QaSeverity.INVALID_LAYERS,
                        f"عدد صفحات TIFF ({frame_count}) لا يطابق صفحات الـlayout ({expected_page_count}).",
                    )
                )
                return False
            for page_index in range(frame_count):
                image.seek(page_index)
                data = image.tag_v2.get(37724)
                if not isinstance(data, bytes):
                    violations.append(
                        QaViolation(
                            QaSeverity.INVALID_LAYERS,
                            f"صفحة TIFF رقم {page_index + 1} لا تحتوي على طبقات قابلة للتحرير.",
                        )
                    )
                    continue
                source_data = TiffImageSourceData.frombytes(data)
                if not source_data.layers or source_data.layers[-1].name != "Background":
                    violations.append(
                        QaViolation(
                            QaSeverity.INVALID_LAYERS,
                            f"صفحة TIFF رقم {page_index + 1} لا تحتوي على طبقة Background مستقلة.",
                        )
                    )
    except Exception as exc:
        violations.append(
            QaViolation(QaSeverity.INVALID_LAYERS, f"فشل فحص طبقات TIFF: {exc}")
        )
        return False
    return not any(v.severity == QaSeverity.INVALID_LAYERS for v in violations)


def _check_clearance_from_raw_geometry(
    placed_parts: list[PlacedPart],
    sheet_width_mm: float,
    sheet_height_mm: float,
    sheet_margin_mm: float,
    clearance_mm: float,
    violations: list[QaViolation],
) -> int:
    printable = Polygon.from_bounds(
        sheet_margin_mm,
        sheet_margin_mm,
        sheet_width_mm - sheet_margin_mm,
        sheet_height_mm - sheet_margin_mm,
    )
    printable_with_tolerance = printable.buffer(_FLOATING_POINT_TOLERANCE_MM)
    for part in placed_parts:
        if not printable_with_tolerance.covers(part.placed_shape_mm):
            outside_area = part.placed_shape_mm.difference(printable).area
            violations.append(
                QaViolation(
                    QaSeverity.CLEARANCE_VIOLATION,
                    f"القطعة '{part.part_id}' خارج منطقة الطباعة بمساحة {outside_area:.6f}mm².",
                )
            )

    if len(placed_parts) < 2:
        return 0
    shapes = [p.placed_shape_mm for p in placed_parts]
    tree = STRtree(shapes)
    threshold = clearance_mm + 0.01
    checked = 0

    for i, part_a in enumerate(placed_parts):
        minx, miny, maxx, maxy = shapes[i].bounds
        indices = tree.query(box(minx - threshold, miny - threshold, maxx + threshold, maxy + threshold))
        for raw_j in indices:
            j = int(raw_j)
            if j <= i:
                continue
            checked += 1
            a, b = shapes[i], shapes[j]
            if a.intersects(b):
                overlap = a.intersection(b)
                if not overlap.is_empty and overlap.area > _FLOATING_POINT_TOLERANCE_MM:
                    violations.append(
                        QaViolation(
                            QaSeverity.OVERLAP,
                            f"تداخل بين '{part_a.part_id}' و '{placed_parts[j].part_id}' بمساحة {overlap.area:.6f}mm².",
                            "0.0mm²",
                            f"{overlap.area:.6f}mm²",
                        )
                    )
                    continue
            distance = float(a.distance(b))
            if distance < clearance_mm - _GEOS_CLEARANCE_BOUNDARY_TOLERANCE_MM:
                violations.append(
                    QaViolation(
                        QaSeverity.CLEARANCE_VIOLATION,
                        f"المسافة بين '{part_a.part_id}' و '{placed_parts[j].part_id}' = {distance:.6f}mm، أقل من {clearance_mm}mm.",
                        f">={clearance_mm}mm",
                        f"{distance:.6f}mm",
                    )
                )
    return checked


def run_qa_check(
    tiff_path: str | Path,
    placed_sheets: list[list[PlacedPart]],
    sheet_width_mm: float,
    sheet_height_mm: float,
    sheet_margin_mm: float,
    resolution: Resolution,
    *,
    clearance_mm: float,
    allowed_modes: tuple[str, ...] = ("RGB", "RGBA"),
    on_check_progress: Callable[[int, int, str], None] | None = None,
) -> QaReport:
    """Run the four independent post-export checks in sequence.

    on_check_progress, when given, is called with (checks_done, checks_total,
    stage_label) before each of the four checks starts. Each check reopens
    the exported TIFF from disk and (for clearance) runs an STRtree query per
    part pair, so a large export can spend real time here with the caller
    otherwise unable to tell this ran at all.
    """
    tiff_path = Path(tiff_path)
    if not placed_sheets or not any(placed_sheets):
        raise QaCheckError("لا يوجد أي أشكال مرتبة للتحقق منها.")
    if not tiff_path.exists():
        raise QaCheckError("ملف TIFF المطلوب فحصه غير موجود.")

    checks_total = 4

    def _report(checks_done: int, stage_label: str) -> None:
        if on_check_progress is not None:
            on_check_progress(checks_done, checks_total, stage_label)

    violations: list[QaViolation] = []
    _report(0, "جاري التحقق من الأبعاد وال‏DPI...")
    checked_dimension, checked_dpi = _check_dimensions_and_dpi(
        tiff_path, sheet_width_mm, sheet_height_mm, resolution.dpi, violations
    )
    expected_width, expected_height = _expected_canvas_size_px(
        sheet_width_mm, sheet_height_mm, resolution.dpi
    )
    _report(1, "جاري التحقق من ال‏ICC profile ووضع الألوان...")
    checked_icc = _check_icc_and_mode(
        tiff_path,
        allowed_modes,
        expected_width * expected_height,
        violations,
    )
    _report(2, "جاري التحقق من الطبقات القابلة للتحرير...")
    checked_layers = _check_editable_layers(
        tiff_path,
        expected_width * expected_height,
        len(placed_sheets),
        violations,
    )
    _report(3, "جاري التحقق من ال‏clearance بين القطع...")
    checked_pairs = sum(
        _check_clearance_from_raw_geometry(
            placed_parts,
            sheet_width_mm,
            sheet_height_mm,
            sheet_margin_mm,
            clearance_mm,
            violations,
        )
        for placed_parts in placed_sheets
    )
    _report(4, "اكتمل الفحص النهائي.")
    return QaReport(
        file_path=str(tiff_path),
        violations=violations,
        checked_dimension=checked_dimension,
        checked_dpi=checked_dpi,
        checked_clearance_pairs=checked_pairs,
        checked_icc_and_mode=checked_icc,
        checked_layers=checked_layers,
        page_count=len(placed_sheets),
    )
