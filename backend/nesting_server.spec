# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for the nesting backend server.

Produces a single-directory distribution (``dist/nesting_server/``) that
contains the executable plus its shared libraries.  One-file mode is
intentionally avoided because:

  1. Startup is faster — no temp-directory extraction on each launch.
  2. Anti-virus false-positive rates on Windows drop dramatically.
  3. The Flutter app already bundles everything in a directory anyway.

Run with:
    pyinstaller nesting_server.spec
"""

import sys
from pathlib import Path

block_cipher = None

# Shapely ships a native GEOS library that PyInstaller cannot detect
# automatically.  We locate it and add it as a binary.
_shapely_libs = []
try:
    import shapely
    _shapely_dir = Path(shapely.__file__).parent
    if sys.platform == "win32":
        _shapely_libs = [(str(p), ".") for p in _shapely_dir.glob("*.dll")]
    elif sys.platform == "darwin":
        _shapely_libs = [(str(p), ".") for p in _shapely_dir.glob("*.dylib")]
        _shapely_libs += [(str(p), ".") for p in (_shapely_dir / ".dylibs").glob("*")]
    else:
        _shapely_libs = [(str(p), ".") for p in _shapely_dir.glob("*.so*")]
except Exception:
    pass

# OpenCV headless ships with native .so/.dll files as well.
_cv2_libs = []
try:
    import cv2
    _cv2_dir = Path(cv2.__file__).parent
    if sys.platform == "win32":
        _cv2_libs = [(str(p), "cv2") for p in _cv2_dir.glob("*.dll")]
        _cv2_libs += [(str(p), "cv2") for p in _cv2_dir.glob("*.pyd")]
    elif sys.platform == "darwin":
        _cv2_libs = [(str(p), "cv2") for p in _cv2_dir.glob("*.dylib")]
        _cv2_libs += [(str(p), "cv2") for p in _cv2_dir.glob("*.so")]
    else:
        _cv2_libs = [(str(p), "cv2") for p in _cv2_dir.glob("*.so*")]
except Exception:
    pass

a = Analysis(
    ["nesting_server.py"],
    pathex=[],
    binaries=_shapely_libs + _cv2_libs,
    datas=[
        # Include the entire app package so that ``app.main:app`` can be
        # resolved at runtime by uvicorn's module loader.
        ("app", "app"),
    ],
    hiddenimports=[
        # --- FastAPI / Uvicorn stack ---
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "fastapi",
        "fastapi.middleware",
        "fastapi.middleware.cors",
        "starlette",
        "starlette.responses",
        "starlette.routing",
        "starlette.middleware",
        "starlette.middleware.cors",
        "anyio",
        "anyio._backends",
        "anyio._backends._asyncio",
        # --- Pydantic v2 ---
        "pydantic",
        "pydantic.deprecated",
        "pydantic.deprecated.decorator",
        "pydantic_core",
        "annotated_types",
        # --- Image / numeric stack ---
        "PIL",
        "PIL.Image",
        "PIL.ImageColor",
        "PIL.TiffImagePlugin",
        "PIL.TiffTags",
        "numpy",
        "numpy.core",
        "numpy.core._methods",
        "numpy.lib",
        "numpy.lib.format",
        "cv2",
        "shapely",
        "shapely.geometry",
        "shapely.ops",
        "shapely.prepared",
        "shapely.validation",
        # --- Misc ---
        "psdtags",
        "multipart",
        "python_multipart",
        "email.mime",
        "email.mime.multipart",
        # --- App internals ---
        "app",
        "app.main",
        "app.api",
        "app.api.job_storage",
        "app.api.processed_images",
        "app.api.schemas",
        "app.core_logging",
        "app.geometry",
        "app.geometry.contour",
        "app.geometry.units",
        "app.image_safety",
        "app.nesting",
        "app.nesting.collision",
        "app.nesting.compaction",
        "app.nesting.engine",
        "app.nesting.lns",
        "app.rasterization",
        "app.rasterization.tiff_export",
        "app.validation",
        "app.validation.alpha_check",
        "app.validation.qa_check",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="nesting_server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # Console window shows the ngrok URL and live request log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="nesting_server",
)
