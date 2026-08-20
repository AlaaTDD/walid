"""Standalone local-only entry point for the nesting backend.

The web app and the Python process run on the same device. Binding to the
loopback interface keeps image bytes on that device: there is no ngrok tunnel,
no public URL, and no internet dependency.
"""
from __future__ import annotations

import logging
import multiprocessing
import sys
import threading
import time


try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass


class _RequestLogHandler(logging.Handler):
    """Print every API request with a clear prefix in the console."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        print(f"[request] {message}", flush=True)


def _run_uvicorn(host: str, port: int, ready_event: threading.Event) -> None:
    import uvicorn

    class _ReadySignalFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if not ready_event.is_set() and "Uvicorn running on" in record.getMessage():
                ready_event.set()
            return True

    config = uvicorn.Config(
        "app.main:app",
        host=host,
        port=port,
        log_level="info",
        access_log=True,
    )
    logging.getLogger("uvicorn.error").addFilter(_ReadySignalFilter())

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(logging.INFO)
    access_logger.handlers = [_RequestLogHandler()]
    access_logger.propagate = False
    uvicorn.Server(config).run()


def main() -> None:
    multiprocessing.freeze_support()
    host, port = "127.0.0.1", 8000

    print(f"[server] Starting local backend on http://{host}:{port} ...")
    ready_event = threading.Event()
    server_thread = threading.Thread(
        target=_run_uvicorn,
        args=(host, port, ready_event),
        daemon=True,
    )
    server_thread.start()

    if not ready_event.wait(timeout=30):
        print("[server] Timed out waiting for the local server to start.", file=sys.stderr)
        raise SystemExit(1)

    print("=" * 60)
    print(f"  LOCAL API URL:  http://{host}:{port}")
    print("  Images remain on this device; no public tunnel is opened.")
    print("=" * 60)
    print("Listening for local requests. Press Ctrl+C to stop.\n")

    try:
        while server_thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[shutdown] Stopping local server...")
    finally:
        print("[shutdown] Done.")


if __name__ == "__main__":
    main()
