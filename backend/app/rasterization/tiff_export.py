"""TIFF export with cached ICC profile generation."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms
from PIL import TiffImagePlugin
from PIL.TiffImagePlugin import AppendingTiffWriter
from psdtags import (
    PsdChannel,
    PsdChannelId,
    PsdCompressionType,
    PsdFormat,
    PsdKey,
    PsdLayer,
    PsdLayers,
    PsdRectangle,
    PsdUserMask,
    TiffImageSourceData,
)

from app.image_safety import open_image_with_limit
from app.rasterization.compositor import CompositedSheet, composite_sheet
from app.geometry.units import Resolution
from app.nesting.engine import PlacedPart


class TiffExportError(Exception):
    pass


class _SafeAppendingTiffWriter(AppendingTiffWriter):
    """Pillow's writer inherits BytesIO, whose finalizer may close twice."""

    _closed_once = False

    def close(self) -> None:
        if self._closed_once:
            return
        self._closed_once = True
        super().close()


@dataclass(frozen=True, slots=True)
class TiffExportResult:
    file_path: str
    width_px: int
    height_px: int
    dpi_x: float
    dpi_y: float
    mode: str
    icc_profile_embedded: bool
    page_count: int = 1
    layer_count: int = 0


@lru_cache(maxsize=1)
def _build_srgb_icc_bytes() -> bytes:
    srgb_profile = ImageCms.createProfile("sRGB")
    return ImageCms.ImageCmsProfile(srgb_profile).tobytes()


def _psd_layer_from_rgba(name: str, image_rgba: Image.Image, left: int, top: int) -> PsdLayer:
    if image_rgba.mode != "RGBA":
        raise TiffExportError(f"طبقة الصورة يجب أن تكون RGBA، وليست {image_rgba.mode!r}.")
    rgba = np.asarray(image_rgba, dtype=np.uint8)
    height, width = rgba.shape[:2]
    channels = [
        PsdChannel(PsdChannelId.CHANNEL0, data=rgba[..., 0]),
        PsdChannel(PsdChannelId.CHANNEL1, data=rgba[..., 1]),
        PsdChannel(PsdChannelId.CHANNEL2, data=rgba[..., 2]),
        # The alpha channel is essential: it preserves transparent holes and
        # anti-aliased edges as editable layer data rather than baking them.
        PsdChannel(PsdChannelId.TRANSPARENCY_MASK, data=rgba[..., 3]),
    ]
    return PsdLayer(
        name=name,
        channels=channels,
        rectangle=PsdRectangle(top, left, top + height, left + width),
    )


def _layered_tiff_info(composited: CompositedSheet) -> tuple[TiffImagePlugin.ImageFileDirectory_v2, int]:
    """Create Photoshop's editable TIFF ImageSourceData tag (37724).

    Standard TIFF stores the merged preview. Photoshop-compatible TIFF layers
    are held separately in tag 37724; this is the documented representation
    used by Photoshop, Affinity Photo, and Krita. Layer channels are ZIP
    compressed losslessly inside the tag, independent of TIFF page LZW.
    """
    width, height = composited.canvas_rgba.size
    # A broadcast view avoids allocating a second full-sheet RGB buffer for
    # a solid layer. psdtags compresses one channel at a time, so only that
    # temporary channel buffer exists while writing large print sheets.
    def solid_channel(value: int) -> np.ndarray:
        return np.broadcast_to(np.uint8(value), (height, width))

    background_layer = PsdLayer(
        name="Background",
        channels=[
            PsdChannel(PsdChannelId.CHANNEL0, data=solid_channel(composited.background_rgba[0])),
            PsdChannel(PsdChannelId.CHANNEL1, data=solid_channel(composited.background_rgba[1])),
            PsdChannel(PsdChannelId.CHANNEL2, data=solid_channel(composited.background_rgba[2])),
        ],
        rectangle=PsdRectangle(0, 0, height, width),
    )

    part_layers: list[PsdLayer] = []
    try:
        # PSD lists the top-most layer first, therefore reverse the drawing
        # order. The Background layer is necessarily last/bottom-most. Unlike
        # the flattened preview, layer bounds are not cropped to the canvas:
        # every original alpha pixel is retained for later editing.
        for layer in reversed(composited.layers):
            part_layers.append(
                _psd_layer_from_rgba(
                    layer.name, layer.image_rgba, layer.left_px, layer.top_px
                )
            )
        source_data = TiffImageSourceData(
            psdformat=PsdFormat.BE32BIT,
            layers=PsdLayers(PsdKey.LAYER, [*part_layers, background_layer]),
            usermask=PsdUserMask(),
        )
        value = source_data.tobytes(compression=PsdCompressionType.ZIP)
    except Exception as exc:
        raise TiffExportError(f"فشل إنشاء TIFF متعدد الطبقات: {exc}") from exc

    info = TiffImagePlugin.ImageFileDirectory_v2()
    # TIFF type 7 = UNDEFINED, as required by Adobe's ImageSourceData tag.
    info[37724] = value
    info.tagtype[37724] = 7
    return info, len(part_layers) + 1


def _verify_editable_layers(image: Image.Image, *, expected_layers: int | None = None) -> int:
    """Validate the actual Photoshop layer tag Pillow just wrote to TIFF."""
    data = image.tag_v2.get(37724)
    if not isinstance(data, bytes):
        raise TiffExportError("ملف TIFF المحفوظ لا يحتوي على بيانات الطبقات القابلة للتحرير.")
    try:
        source_data = TiffImageSourceData.frombytes(data)
    except Exception as exc:
        raise TiffExportError(f"بيانات طبقات TIFF غير قابلة للقراءة: {exc}") from exc
    count = len(source_data.layers)
    if count < 1 or source_data.layers[-1].name != "Background":
        raise TiffExportError("ملف TIFF لا يحتوي على طبقة Background مستقلة صالحة.")
    if expected_layers is not None and count != expected_layers:
        raise TiffExportError(
            f"عدد طبقات TIFF ({count}) لا يطابق المتوقع ({expected_layers})."
        )
    return count


def export_tiff(
    composited: CompositedSheet,
    output_path: str | Path,
    *,
    mode: str = "RGB",
    icc_profile_bytes: bytes | None = None,
    compression: str | None = "tiff_lzw",
) -> TiffExportResult:
    if mode not in ("RGB", "RGBA"):
        raise TiffExportError("mode غير مدعوم — استخدم RGB أو RGBA.")
    canvas = composited.canvas_rgba
    if canvas.mode != "RGBA":
        raise TiffExportError(f"canvas_rgba غير متوقع: {canvas.mode!r}")

    export_image = canvas.convert("RGB") if mode == "RGB" else canvas
    profile_bytes = icc_profile_bytes if icc_profile_bytes is not None else _build_srgb_icc_bytes()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tiffinfo, layer_count = _layered_tiff_info(composited)
    kwargs = {
        "format": "TIFF",
        "dpi": (composited.resolution.dpi, composited.resolution.dpi),
        "icc_profile": profile_bytes,
        "tiffinfo": tiffinfo,
    }
    if compression is not None:
        kwargs["compression"] = compression
    try:
        export_image.save(output_path, **kwargs)
    except Exception as exc:
        raise TiffExportError(f"فشل حفظ TIFF: {exc}") from exc

    # This file was just written from our in-memory canvas. Permit precisely
    # that known canvas size for metadata verification; uploaded source images
    # still use the much smaller untrusted-image safety limit.
    with open_image_with_limit(output_path, max_pixels=canvas.width * canvas.height) as verify:
        dpi = verify.info.get("dpi", (None, None))
        if dpi[0] is None or dpi[1] is None:
            raise TiffExportError("الملف المحفوظ لا يحتوي على DPI metadata قابلة للقراءة.")
        verified_layer_count = _verify_editable_layers(verify, expected_layers=layer_count)
        return TiffExportResult(
            file_path=str(output_path),
            width_px=verify.width,
            height_px=verify.height,
            dpi_x=float(dpi[0]),
            dpi_y=float(dpi[1]),
            mode=verify.mode,
            icc_profile_embedded=verify.info.get("icc_profile") is not None,
            layer_count=verified_layer_count,
        )


def export_multi_sheet_tiff(
    sheets: list[list[PlacedPart]],
    sheet_width_mm: float,
    sheet_height_mm: float,
    resolution: Resolution,
    output_path: str | Path,
    *,
    mode: str = "RGB",
    background_rgba: tuple[int, int, int, int] = (255, 255, 255, 255),
    icc_profile_bytes: bytes | None = None,
    compression: str | None = "tiff_lzw",
    on_sheet_progress: Callable[[int, int, str], None] | None = None,
) -> TiffExportResult:
    """Write a multi-page TIFF one page at a time, keeping only one canvas alive.

    on_sheet_progress, when given, is called with (sheets_done, sheets_total,
    stage_label) at three points per sheet: before compositing starts, once
    compositing finishes (parts pasted, ready to encode), and once the page is
    written to the TIFF writer. This keeps a caller able to report meaningful
    live progress through what is otherwise one long, silent per-sheet loop —
    compositing pastes every part's raster and PSD-layer encoding ZIP-compresses
    each one, both of which can take real time on a sheet with many parts.
    """
    if not sheets:
        raise TiffExportError("لا توجد أوراق مرتبة للتصدير.")
    if mode not in ("RGB", "RGBA"):
        raise TiffExportError("mode غير مدعوم — استخدم RGB أو RGBA.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    profile_bytes = icc_profile_bytes if icc_profile_bytes is not None else _build_srgb_icc_bytes()
    expected_size: tuple[int, int] | None = None
    layer_count_total = 0
    sheet_total = len(sheets)

    def _report(sheet_index: int, stage_label: str) -> None:
        if on_sheet_progress is not None:
            on_sheet_progress(sheet_index, sheet_total, stage_label)

    try:
        # AppendingTiffWriter fixes each frame's offsets before the next page
        # is composed. Unlike Pillow's append_images list, this never retains
        # N full-resolution RGBA canvases in RAM.
        with _SafeAppendingTiffWriter(output_path, new=True) as writer:
            for sheet_number, placed_parts in enumerate(sheets, start=1):
                _report(sheet_number - 1, f"جاري تركيب الصورة {sheet_number} من {sheet_total}...")
                part_total = len(placed_parts)

                def _on_part(part_done: int, part_total_inner: int, *, _sheet_number: int = sheet_number) -> None:
                    _report(
                        _sheet_number - 1,
                        f"جاري تركيب الصورة {_sheet_number} من {sheet_total} — قطعة {part_done} من {part_total_inner}...",
                    )

                composited = composite_sheet(
                    placed_parts,
                    sheet_width_mm,
                    sheet_height_mm,
                    resolution,
                    background_rgba=background_rgba,
                    retain_layers=True,
                    on_part_composited=_on_part if part_total > 0 and on_sheet_progress is not None else None,
                )
                canvas = composited.canvas_rgba
                expected_size = canvas.size
                export_image = canvas.convert("RGB") if mode == "RGB" else canvas
                _report(sheet_number - 1, f"جاري ترميز طبقات TIFF للصورة {sheet_number} من {sheet_total}...")
                tiffinfo, layer_count = _layered_tiff_info(composited)
                layer_count_total += layer_count
                kwargs = {
                    "format": "TIFF",
                    "dpi": (resolution.dpi, resolution.dpi),
                    "icc_profile": profile_bytes,
                    "tiffinfo": tiffinfo,
                }
                if compression is not None:
                    kwargs["compression"] = compression
                try:
                    export_image.save(writer, **kwargs)
                    writer.newFrame()
                finally:
                    if export_image is not canvas:
                        export_image.close()
                    for layer in composited.layers:
                        layer.close()
                    canvas.close()
                _report(sheet_number, f"تم حفظ الصورة {sheet_number} من {sheet_total}.")
    except Exception as exc:
        raise TiffExportError(f"فشل حفظ TIFF متعدد الصفحات: {exc}") from exc

    assert expected_size is not None
    with open_image_with_limit(output_path, max_pixels=expected_size[0] * expected_size[1]) as verify:
        dpi = verify.info.get("dpi", (None, None))
        if dpi[0] is None or dpi[1] is None:
            raise TiffExportError("الملف المحفوظ لا يحتوي على DPI metadata قابلة للقراءة.")
        frame_count = getattr(verify, "n_frames", 1)
        if frame_count != len(sheets):
            raise TiffExportError(f"عدد صفحات TIFF ({frame_count}) لا يطابق الأوراق ({len(sheets)}).")
        verified_layer_count = 0
        for page_index in range(frame_count):
            verify.seek(page_index)
            verified_layer_count += _verify_editable_layers(verify)
        if verified_layer_count != layer_count_total:
            raise TiffExportError(
                f"إجمالي طبقات TIFF ({verified_layer_count}) لا يطابق المتوقع ({layer_count_total})."
            )
        return TiffExportResult(
            file_path=str(output_path),
            width_px=verify.width,
            height_px=verify.height,
            dpi_x=float(dpi[0]),
            dpi_y=float(dpi[1]),
            mode=verify.mode,
            icc_profile_embedded=verify.info.get("icc_profile") is not None,
            page_count=frame_count,
            layer_count=verified_layer_count,
        )
