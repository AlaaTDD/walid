"""Scoped Pillow pixel limits for untrusted images and known export files."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock

from PIL import Image


def _configured_input_pixel_limit() -> int | None:
    """Return an optional Pillow pixel limit.

    Desktop production jobs can legitimately contain very large print files.
    There is intentionally no hidden default pixel cap; operators that need a
    defensive limit can opt in with ``NESTING_MAX_IMAGE_PIXELS``.
    """
    configured = os.getenv("NESTING_MAX_IMAGE_PIXELS")
    if configured is None or not configured.strip() or configured.strip() == "0":
        return None
    try:
        return max(1, int(configured))
    except ValueError:
        return None


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
