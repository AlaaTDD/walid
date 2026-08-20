#!/bin/bash
# Double-click this file to start the server. Works no matter where this
# folder is located on the Mac (Desktop, external drive, anywhere).

# Move into this script's own folder first, so run_server.py finds the
# app code and its saved token regardless of where the .command file
# itself was double-clicked from.
cd "$(dirname "$0")"

# Find a working Python 3 interpreter without assuming the user has one
# named exactly "python" on their PATH (macOS often only has "python3").
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "============================================================"
    echo "  Python was not found on this Mac."
    echo "  Install it from https://www.python.org/downloads/ and"
    echo "  then double-click this file again."
    echo "============================================================"
    read -p "Press Enter to close..."
    exit 1
fi

"$PYTHON" run_server.py
