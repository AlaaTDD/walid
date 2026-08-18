#!/usr/bin/env python3
"""Build the nesting backend into a standalone executable.

Usage (from the backend/ directory):
    python build_backend.py

Prerequisites:
    pip install pyinstaller   (inside the same venv that has the project deps)

Output:
    dist/nesting_server/      — a self-contained directory with the executable
                                and all its shared libraries.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    backend_dir = Path(__file__).resolve().parent
    spec_file = backend_dir / "nesting_server.spec"

    if not spec_file.exists():
        print(f"ERROR: spec file not found: {spec_file}", file=sys.stderr)
        return 1

    # Ensure PyInstaller is available
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing PyInstaller...", file=sys.stderr)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
        )

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        str(spec_file),
    ]

    print(f"Running: {' '.join(cmd)}")
    print(f"Working directory: {backend_dir}")
    result = subprocess.run(cmd, cwd=str(backend_dir))

    if result.returncode == 0:
        dist_path = backend_dir / "dist" / "nesting_server"
        print(f"\n✅ Build succeeded! Output: {dist_path}")
    else:
        print(f"\n❌ Build failed with code {result.returncode}", file=sys.stderr)

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
