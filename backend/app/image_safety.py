"""Scoped Pillow pixel limits for untrusted images and known export files."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock

from PIL import Image


def _configured_input_pixel_limit() -> int | None:
    """Return the Pillow pixel limit applied to untrusted uploaded images.

    Security review finding: a small PNG (a few MB on disk) whose header
    declares an enormous pixel grid can decode to gigabytes in memory --
    measured directly: a 3.4MB solid-colour PNG at 30000x30000px decodes to
    ~3.4GB as RGBA, well under MAX_UPLOAD_BYTES' 1GiB file-size cap, which
    checks bytes on disk and has no visibility into decoded size at all.
    Multiple such uploads analysed concurrently (MAX_PARALLEL_IMAGE_ANALYSES)
    can exhaust available memory well before any file-size limit is reached.

    Desktop production jobs can legitimately contain very large print files
    (a 300 DPI A0 poster is already ~138 million pixels), so the default here
    is deliberately generous rather than Pillow's own stock ~89 million-pixel
    default -- 500 million pixels comfortably covers realistic large-format
    print sizes while still bounding the decode-memory amplification factor
    to roughly 500x instead of unlimited. An operator who genuinely needs a
    single image larger than this can still raise or disable the limit
    explicitly via NESTING_MAX_IMAGE_PIXELS (0 or a blank value disables it
    entirely, matching the previous default), so this changes only the
    out-of-the-box posture for a fresh install, not what the tool is capable
    of handling when an operator opts into a larger/unlimited value.
    """
    configured = os.getenv("NESTING_MAX_IMAGE_PIXELS")
    if configured is not None and configured.strip() == "0":
        return None
    if configured is None or not configured.strip():
        return _DEFAULT_MAX_INPUT_IMAGE_PIXELS
    try:
        return max(1, int(configured))
    except ValueError:
        return _DEFAULT_MAX_INPUT_IMAGE_PIXELS


_DEFAULT_MAX_INPUT_IMAGE_PIXELS = 500_000_000


MAX_INPUT_IMAGE_PIXELS = _configured_input_pixel_limit()
_pillow_pixel_limit_lock = RLock()


@contextmanager
def pillow_pixel_limit(max_pixels: int | None) -> Iterator[None]:
    """Temporarily set Pillow's process-global bomb limit without races.

    Pillow exposes this guard as a module global. All image opens in this app
    use this lock so a trusted, locally generated TIFF can be inspected at its
    known canvas size without weakening validation of user uploads.
    """
    if max_pixels is not None and max_pixels < 1:
        raise ValueError("max_pixels يجب أن يكون موجباً أو None.")
    with _pillow_pixel_limit_lock:
        previous = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = max_pixels
        try:
            yield
        finally:
            Image.MAX_IMAGE_PIXELS = previous


@contextmanager
def open_image_with_limit(path: str | os.PathLike[str], *, max_pixels: int | None) -> Iterator[Image.Image]:
    """Open one image while the requested Pillow pixel limit is in force."""
    with pillow_pixel_limit(max_pixels):
        with Image.open(path) as image:
            yield image
