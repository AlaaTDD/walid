"""Fast raster compositor using metadata captured during upload."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

from app.geometry.units import Resolution, mm_to_px, sheet_canvas_size_px
from app.image_safety import MAX_INPUT_IMAGE_PIXELS, open_image_with_limit
from app.nesting.engine import PlacedPart
from app.nesting.rotation import LockedRotation
from app.validation.alpha_check import normalize_open_image_to_rgba


class CompositingError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CompositedSheet:
    canvas_rgba: Image.Image
    sheet_width_mm: float
    sheet_height_mm: float
    resolution: Resolution
    background_rgba: tuple[int, int, int, int]
    # Kept only while a layered TIFF is being written.  The flattened canvas
    # remains the standard TIFF preview; these rasters become Photoshop layers
    # in tag 37724 and are never flattened into the editable representation.
    layers: tuple["RasterLayer", ...] = ()


@dataclass(slots=True)
class RasterLayer:
    """One unflattened part raster and its full-sheet pixel origin."""

    name: str
    image_rgba: Image.Image
    left_px: int
    top_px: int

    def close(self) -> None:
        self.image_rgba.close()


def _rotate_image_locked(image: Image.Image, angle: LockedRotation) -> Image.Image:
    """Rotate a raster the same amount rotate_shape() rotated its contour.

    LockedRotation now covers 24 values (every 15deg from 0-345), not just the
    original 4 axis-aligned angles.  The four original angles remain lossless
    transpose operations; every other angle uses PIL's general affine rotate
    with expand=True (grows the canvas to fit the rotated content, matching
    how rotate_shape() rotates the polygon about its own centroid without
    cropping it) and NEAREST resampling, which keeps the alpha mask a hard
    edge instead of introducing semi-transparent anti-aliased pixels that
    would not correspond to the exact contour already computed in Shapely.
    """
    if angle == LockedRotation.DEG_0:
        return image
    if angle == LockedRotation.DEG_90:
        return image.transpose(Image.Transpose.ROTATE_90)
    if angle == LockedRotation.DEG_180:
        return image.transpose(Image.Transpose.ROTATE_180)
    if angle == LockedRotation.DEG_270:
        return image.transpose(Image.Transpose.ROTATE_270)
    # PIL rotates counter-clockwise for a positive angle in image coordinates
    # (Y grows downward), which is the same convention rotate_shape() already
    # documents and relies on for its own counter-clockwise image rotation.
    return image.rotate(
        angle.value,
        resample=Image.Resampling.NEAREST,
        expand=True,
        fillcolor=(0, 0, 0, 0),
    )


def _rotate_point_in_image(
    point_px: tuple[float, float],
    width_px: int,
    height_px: int,
    angle: LockedRotation,
) -> tuple[float, float]:
    """Map a point from the pre-rotation image into the rotated (expanded) canvas.

    The four original transpose-based angles keep their exact integer
    formulas.  Every other angle is derived from the same general rotation
    used by PIL's expand=True: rotate the point about the original image's
    centre by the angle (PIL/Shapely's counter-clockwise convention with a
    downward Y axis), then re-origin it to the expanded canvas, whose size
    PIL computes as the rotated bounding box of the source rectangle.
    """
    x, y = point_px
    w, h = float(width_px), float(height_px)
    if angle == LockedRotation.DEG_0:
        return x, y
    if angle == LockedRotation.DEG_90:
        return y, w - x
    if angle == LockedRotation.DEG_180:
        return w - x, h - y
    if angle == LockedRotation.DEG_270:
        return h - y, x

    theta = math.radians(angle.value)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    cx, cy = w / 2.0, h / 2.0
    dx, dy = x - cx, y - cy
    # Counter-clockwise rotation in a downward-Y image coordinate system.
    rx = dx * cos_t + dy * sin_t
    ry = -dx * sin_t + dy * cos_t
    new_w = abs(w * cos_t) + abs(h * sin_t)
    new_h = abs(w * sin_t) + abs(h * cos_t)
    return rx + new_w / 2.0, ry + new_h / 2.0


def _crop_to_alpha_bbox(image: Image.Image, bbox: tuple[int, int, int, int] | None) -> tuple[Image.Image, tuple[float, float]]:
    """Crop transparent padding and return the cropped image plus source offset."""
    if bbox is None:
        return image, (0.0, 0.0)
    x0, y0, x1, y1 = bbox
    x0 = max(0, min(x0, image.width - 1))
    y0 = max(0, min(y0, image.height - 1))
    x1 = max(x0 + 1, min(x1, image.width))
    y1 = max(y0 + 1, min(y1, image.height))
    return image.crop((x0, y0, x1, y1)), (float(x0), float(y0))


def rasterize_part_layer(part: PlacedPart, resolution: Resolution, *, layer_name: str) -> RasterLayer:
    """Return the source pixels as a positioned RGBA layer, without scaling.

    Geometry is calculated in millimetres and the only mm→px conversion is
    the same final conversion used by the flattened preview.  The returned
    image owns its buffer; callers must close it once the TIFF tag is written.
    """
    source: Image.Image | None = None
    cropped: Image.Image | None = None
    rotated: Image.Image | None = None
    try:
        with open_image_with_limit(part.source_image_path, max_pixels=MAX_INPUT_IMAGE_PIXELS) as raw:
            raw.load()
            # Keep source files untouched. RGB/JPEG images are normalized to
            # opaque RGBA and any EXIF rotation is applied exactly as it was
            # during contour extraction.
            normalized = normalize_open_image_to_rgba(raw)
            # Always take ownership of a standalone raster.  This keeps a
            # Pillow context manager from closing a layer retained for export.
            source = normalized.copy()
            if normalized is not raw:
                normalized.close()
    except FileNotFoundError as exc:
        raise CompositingError(f"الصورة الأصلية غير موجودة للقطعة '{part.part_id}'.") from exc
    except Exception as exc:
        raise CompositingError(f"فشل فتح الصورة للقطعة '{part.part_id}': {exc}") from exc

    if part.source_centroid_px is None:
        # Backward-compatible fallback.  Current jobs always have metadata.
        from app.geometry.contour import extract_contour_from_image
        extracted = extract_contour_from_image(source, resolution)
        source_centroid = extracted.source_centroid_px
        bbox = extracted.alpha_bbox_px
    else:
        source_centroid = part.source_centroid_px
        bbox = part.alpha_bbox_px

    try:
        cropped, (off_x, off_y) = _crop_to_alpha_bbox(source, bbox)
        cropped_centroid = (source_centroid[0] - off_x, source_centroid[1] - off_y)
        rotated = _rotate_image_locked(cropped, part.rotation)
        rotated_centroid = _rotate_point_in_image(cropped_centroid, cropped.width, cropped.height, part.rotation)

        target = part.placed_shape_mm.centroid
        target_x_px = mm_to_px(float(target.x), resolution)
        target_y_px = mm_to_px(float(target.y), resolution)
        paste_x = round(target_x_px - rotated_centroid[0])
        paste_y = round(target_y_px - rotated_centroid[1])

        assert rotated is not None
        # Transfer the rotated buffer to the caller and release only buffers
        # which are not it.  Rotation is strictly 90° based and therefore
        # lossless — no resize/resample occurs here.
        if cropped is not None and cropped is not rotated and cropped is not source:
            cropped.close()
        if source is not None and source is not rotated:
            source.close()
        return RasterLayer(layer_name, rotated, paste_x, paste_y)
    except Exception:
        for image in (rotated, cropped, source):
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass
        raise


def composite_sheet(
    placed_parts: list[PlacedPart],
    sheet_width_mm: float,
    sheet_height_mm: float,
    resolution: Resolution,
    *,
    background_rgba: tuple[int, int, int, int] = (255, 255, 255, 255),
    retain_layers: bool = True,
    on_part_composited: Callable[[int, int], None] | None = None,
) -> CompositedSheet:
    """Composite one sheet's parts onto a canvas.

    on_part_composited, when given, is called after each part is pasted with
    (parts_done, parts_total) — 1-indexed progress within this single sheet.
    It lets a caller (e.g. the TIFF exporter) surface live export progress for
    sheets with many parts instead of the whole call staying silent until it
    returns.
    """
    if sheet_width_mm <= 0 or sheet_height_mm <= 0:
        raise CompositingError("أبعاد الشيت يجب أن تكون أكبر من صفر.")
    canvas_size = sheet_canvas_size_px(sheet_width_mm, sheet_height_mm, resolution)
    canvas = Image.new("RGBA", canvas_size, background_rgba)
    layers: list[RasterLayer] = []
    total_parts = len(placed_parts)
    try:
        for index, part in enumerate(placed_parts, start=1):
            layer = rasterize_part_layer(
                part,
                resolution,
                # PSD Pascal names are legacy MacRoman.  Keep names portable;
                # the original filename remains available in job metadata.
                layer_name=f"Image {index:04d} ({part.part_id})",
            )
            mask = layer.image_rgba.getchannel("A")
            try:
                canvas.paste(layer.image_rgba, (layer.left_px, layer.top_px), mask)
            finally:
                mask.close()
            if retain_layers:
                layers.append(layer)
            else:
                layer.close()
            if on_part_composited is not None:
                on_part_composited(index, total_parts)
        return CompositedSheet(
            canvas,
            sheet_width_mm,
            sheet_height_mm,
            resolution,
            background_rgba,
            tuple(layers),
        )
    except Exception:
        for layer in layers:
            layer.close()
        canvas.close()
        raise
