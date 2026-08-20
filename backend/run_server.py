"""Start the nesting backend locally, with no public tunnel.

The first run creates a private virtual environment beside this script. The
web interface connects to ``http://127.0.0.1:8000`` on the same device, so the
selected images are copied only to the local Python process for analysis.
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing
import os
import sys
import threading
from pathlib import Path


try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass


SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = SCRIPT_DIR / ".runtime"
RUNTIME_MARKER = RUNTIME_DIR / "ready.txt"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _read_dependencies_from_pyproject() -> list[str]:
    """Read the backend dependency names without installing this repo itself."""
    fallback = [
        "fastapi>=0.110.0",
        "uvicorn[standard]>=0.29.0",
        "shapely>=2.0.0",
        "pillow>=10.0.0",
        "numpy>=1.26.0",
        "psdtags>=2025.1.1",
        "opencv-python-headless>=4.9.0",
        "pydantic>=2.6.0",
        "python-multipart>=0.0.9",
    ]
    pyproject_path = SCRIPT_DIR / "pyproject.toml"
    if not pyproject_path.exists():
        return fallback
    try:
        import tomllib

        with pyproject_path.open("rb") as fh:
            dependencies = list(tomllib.load(fh).get("project", {}).get("dependencies", []))
        return dependencies or fallback
    except Exception:
        return fallback


def _runtime_python() -> Path:
    return RUNTIME_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _ensure_runtime_and_relaunch() -> None:
    if os.environ.get("_NESTING_TOOL_RUNTIME_ACTIVE") == "1":
        return

    runtime_python = _runtime_python()
    if not RUNTIME_MARKER.exists() or not runtime_python.exists():
        import subprocess
        import venv

        print("[setup] Creating the private runtime and installing dependencies...")
        venv.EnvBuilder(with_pip=True, clear=True).create(str(RUNTIME_DIR))
        result = subprocess.run(
            [str(runtime_python), "-m", "pip", "install", "--quiet", *_read_dependencies_from_pyproject()],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            raise SystemExit("[setup] Dependency installation failed.")
        RUNTIME_MARKER.write_text("ok", encoding="utf-8")

    import subprocess

    env = os.environ.copy()
    env["_NESTING_TOOL_RUNTIME_ACTIVE"] = "1"
    raise SystemExit(
        subprocess.run([str(runtime_python), str(Path(__file__).resolve()), *sys.argv[1:]], env=env).returncode
    )


class _RequestLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        print(f"[request] {message}", flush=True)


def _run_uvicorn(host: str, port: int) -> None:
    import uvicorn

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(logging.INFO)
    access_logger.handlers = [_RequestLogHandler()]
    access_logger.propagate = False
    uvicorn.run("app.main:app", host=host, port=port, log_level="info", access_log=True)


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Start the local nesting backend.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback host for same-device use (default: 127.0.0.1)",
    )
    args = parser.parse_args()

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        _ensure_runtime_and_relaunch()
        return

    print("=" * 60)
    print(f"  LOCAL API URL: http://{args.host}:{args.port}")
    print("  Images stay on this device. No internet tunnel is opened.")
    print("=" * 60)
    _run_uvicorn(args.host, args.port)


if __name__ == "__main__":
    main()
