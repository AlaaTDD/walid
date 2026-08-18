"""Input-image normalization and visible-content validation.

The nesting engine uses an alpha mask internally, but customers should not
have to pre-convert ordinary photos to PNG/RGBA before uploading.  Decodable
raster formats are therefore normalized to RGBA in memory.  RGB images get an
opaque alpha channel, which correctly makes their complete rectangular canvas
their cut contour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from app.geometry.contour import ALPHA_THRESHOLD
from app.image_safety import MAX_INPUT_IMAGE_PIXELS, open_image_with_limit


@dataclass(frozen=True, slots=True)
class ValidationResult:
    file_path: str
    is_valid: bool
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class BatchValidationReport:
    accepted: list[str] = field(default_factory=list)
    rejected: list[ValidationResult] = field(default_factory=list)

    @property
    def all_valid(self) -> bool:
        return not self.rejected

    @property
    def total_count(self) -> int:
        return len(self.accepted) + len(self.rejected)


def normalize_open_image_to_rgba(image: Image.Image) -> Image.Image:
    """Return an oriented RGBA working image without touching the source file."""
    # JPEG photos frequently carry their intended orientation in EXIF rather
    # than their raw pixels. Apply it before extracting geometry, then make the
    # compositor apply this same normalizer so contour and printed pixels stay
    # exactly aligned.
    orientation = image.getexif().get(274, 1)
    working = ImageOps.exif_transpose(image) if orientation != 1 else image
    if working.mode == "RGBA":
        return working
    normalized = working.convert("RGBA")
    if working is not image:
        working.close()
    return normalized


def validate_open_rgba_image(image: Image.Image, file_path: str | Path) -> ValidationResult:
    """Validate a normalized RGBA image has printable pixels.

    The name remains for compatibility with the upload pipeline.  Callers
    normalize first; opaque alpha is intentionally valid.
    """
    path = str(file_path)
    if image.mode != "RGBA":
        return ValidationResult(
            path,
            False,
            f"تعذر تجهيز الصورة بصيغة RGBA (الصيغة الحالية: {image.mode}).",
        )
    alpha = np.asarray(image.getchannel("A"), dtype=np.uint8)
    if bool(np.all(alpha == 0)):
        return ValidationResult(path, False, "الصورة شفافة بالكامل — مفيش أي شكل مرئي.")
    if not bool(np.any(alpha >= ALPHA_THRESHOLD)):
        return ValidationResult(path, False, "مفيش أي بكسل غير شفاف قابل لاستخراج contour منه.")
    return ValidationResult(path, True)


def validate_single_image(file_path: str | Path) -> ValidationResult:
    path = Path(file_path)
    if not path.exists():
        return ValidationResult(str(path), False, "الملف غير موجود")
    try:
        with open_image_with_limit(path, max_pixels=MAX_INPUT_IMAGE_PIXELS) as image:
            image.load()
            normalized = normalize_open_image_to_rgba(image)
            try:
                return validate_open_rgba_image(normalized, path)
            finally:
                if normalized is not image:
                    normalized.close()
    except Exception as exc:
        return ValidationResult(str(path), False, f"فشل قراءة الصورة: {exc}")


def validate_batch(file_paths: list[str | Path]) -> BatchValidationReport:
    report = BatchValidationReport()
    for file_path in file_paths:
        result = validate_single_image(file_path)
        if result.is_valid:
            report.accepted.append(result.file_path)
        else:
            report.rejected.append(result)
    return report
