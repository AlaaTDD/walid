"""Stops the nesting backend (and its ngrok tunnel) cleanly.

Run this whenever you're not sure if a server is still running, or if
starting a new one fails with "address already in use". It finds every
process currently listening on the backend's port (there can be more than
one if a previous run wasn't closed properly -- for example the window was
force-closed, or the machine slept mid-session), asks each one to shut down
politely, and force-kills any that don't respond in time. It then does the
same for any lingering ``ngrok`` process, since a leftover tunnel process
can itself hold onto its own local inspection port (4040) even after the
server behind it is gone.

This is the companion tool to ``run_server.py``: that one starts the server,
this one guarantees it (and only it) is stopped, no matter how many times it
was accidentally started or how it was left running before.

Usage:
    python stop_server.py
    python stop_server.py --port 8000
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

# Make sure every print() shows up immediately, matching run_server.py's own
# terminal-buffering behavior so both tools feel like one consistent pair.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

IS_WINDOWS = sys.platform.startswith("win")

# How long to wait, after asking a process to shut down politely, before
# assuming it's stuck and force-killing it instead. Mirrors the grace period
# convention used elsewhere in this project's tooling.
GRACE_PERIOD_SECONDS = 5.0


def _find_pids_on_port(port: int) -> list[int]:
    """Returns every PID currently listening on the given TCP port.

    Uses ``lsof`` on macOS/Linux (present by default on both) and
    ``netstat`` + ``findstr`` on Windows (also present by default), so this
    never depends on installing anything extra -- matching run_server.py's
    own "no manual setup steps" philosophy.
    """
    if IS_WINDOWS:
        try:
            output = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        except OSError:
            return []
        pids: set[int] = set()
        needle = f":{port} "
        for line in output.splitlines():
            if "LISTENING" not in line or needle not in line:
                continue
            parts = line.split()
            if not parts:
                continue
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                continue
        return sorted(pids)

    try:
        output = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        # lsof isn't installed (rare, but possible on a minimal Linux
        # install) -- nothing more we can safely do without it.
        return []

    pids: list[int] = []
    for line in output.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _find_ngrok_pids() -> list[int]:
    """Returns every PID for a running ``ngrok`` process, regardless of which
    port it's tunnelling to. A leftover ngrok process (from a backend that
    was killed without going through its own clean shutdown path) keeps
    running on its own, still holding its local inspection API port open,
    and confuses the next run into thinking a tunnel already exists.
    """
    if IS_WINDOWS:
        try:
            output = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq ngrok.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        except OSError:
            return []
        pids: list[int] = []
        for line in output.splitlines():
            fields = [f.strip('"') for f in line.strip().split('","')]
            if len(fields) >= 2:
                try:
                    pids.append(int(fields[1]))
                except ValueError:
                    continue
        return pids

    try:
        output = subprocess.run(
            ["pgrep", "-x", "ngrok"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return []
    return [int(line) for line in output.splitlines() if line.strip().isdigit()]


def _is_alive(pid: int) -> bool:
    if IS_WINDOWS:
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in output

    import os
    import errno

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it -- still "alive" from our
        # point of view (and also not one we should be touching).
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True


def _terminate(pid: int) -> None:
    """Asks a process to shut down politely (SIGTERM / Windows equivalent)."""
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return

    import os
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # already gone


def _kill(pid: int) -> None:
    """Force-kills a process that didn't respond to a polite shutdown."""
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            check=False,
        )
        return

    import os
    import signal

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _stop_pids(pids: list[int], label: str) -> None:
    """Terminates every given PID, escalating to a force-kill for any that
    are still alive after the grace period. Mirrors the same
    terminate-then-escalate shape used by this project's process-management
    tooling elsewhere, so behaviour stays predictable across the whole repo.
    """
    if not pids:
        print(f"[stop] No {label} process found. Nothing to do.")
        return

    plural = "es" if len(pids) != 1 else ""
    print(f"[stop] Found {len(pids)} {label} process{plural}: {', '.join(map(str, pids))}")

    for pid in pids:
        print(f"[stop] Asking {label} process {pid} to shut down...")
        _terminate(pid)

    deadline = time.monotonic() + GRACE_PERIOD_SECONDS
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _is_alive(pid)}
        if remaining:
            time.sleep(0.3)

    for pid in remaining:
        print(f"[stop] {label} process {pid} didn't stop in time -- forcing it closed.")
        _kill(pid)

    still_stuck = [pid for pid in remaining if _is_alive(pid)]
    if still_stuck:
        print(
            f"[stop] WARNING: {label} process(es) {', '.join(map(str, still_stuck))} "
            "could not be stopped. You may need to close them manually "
            "(Activity Monitor / Task Manager).",
            file=sys.stderr,
        )
    else:
        print(f"[stop] All {label} process(es) stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stop the nesting backend server and its ngrok tunnel, however many copies are running."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Local port the server was started on (default: 8000, matches run_server.py's default)",
    )
    parser.add_argument(
        "--skip-ngrok",
        action="store_true",
        help="Only stop the server process(es); leave any ngrok tunnel process running",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  STOPPING NESTING BACKEND")
    print("=" * 60)

    server_pids = _find_pids_on_port(args.port)
    _stop_pids(server_pids, f"server (port {args.port})")

    if not args.skip_ngrok:
        ngrok_pids = _find_ngrok_pids()
        _stop_pids(ngrok_pids, "ngrok")

    print("=" * 60)
    print("  Done. It's now safe to start the server again.")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[stop] Cancelled.")
        sys.exit(1)
