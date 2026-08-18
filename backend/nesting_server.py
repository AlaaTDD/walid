"""Standalone entry-point that starts the FastAPI nesting backend.

This module is the PyInstaller target: it boots Uvicorn with the
``app.main:app`` application on ``127.0.0.1:8000`` so that the Flutter
desktop client can connect without requiring a manual ``python -m uvicorn``
invocation — and, critically, without requiring Python to be installed on
the end-user's machine at all.
"""
from __future__ import annotations

import multiprocessing
import sys


def main() -> None:
    # PyInstaller frozen executables on Windows need the multiprocessing
    # freeze-support call before anything else touches process spawning.
    multiprocessing.freeze_support()

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
