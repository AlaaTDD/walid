#!/usr/bin/env bash
# ============================================================================
# build_release.sh — Build a complete release for macOS or Linux
# ============================================================================
#
# Usage:
#     ./build_release.sh            # Auto-detect current OS
#     ./build_release.sh macos      # Force macOS build
#     ./build_release.sh linux      # Force Linux build
#
# Prerequisites:
#     - Python 3.10+ with venv in backend/.venv
#     - Flutter SDK in PATH
#     - (macOS only) create-dmg: brew install create-dmg  (optional, for .dmg)
#
# Output:
#     release/                      — Final release artifacts
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
RELEASE_DIR="$SCRIPT_DIR/release"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log()   { echo -e "${BLUE}[BUILD]${NC} $*"; }
ok()    { echo -e "${GREEN}[  OK ]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN ]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# Detect platform
PLATFORM="${1:-}"
if [ -z "$PLATFORM" ]; then
    case "$(uname -s)" in
        Darwin) PLATFORM="macos" ;;
        Linux)  PLATFORM="linux" ;;
        *)      error "Unsupported OS: $(uname -s). Use 'macos' or 'linux'."; exit 1 ;;
    esac
fi

log "Building release for: $PLATFORM"
log "Script directory: $SCRIPT_DIR"

# ── Step 1: Build Backend with PyInstaller ──────────────────────────────────

log "Step 1/4: Building backend with PyInstaller..."

if [ ! -d "$BACKEND_DIR" ]; then
    error "Backend directory not found: $BACKEND_DIR"
    exit 1
fi

# Find Python — prefer venv, fall back to system
PYTHON=""
if [ -f "$BACKEND_DIR/.venv/bin/python" ]; then
    PYTHON="$BACKEND_DIR/.venv/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    error "Python not found. Please install Python 3.10+ or create a venv."
    exit 1
fi

log "Using Python: $PYTHON"
"$PYTHON" "$BACKEND_DIR/build_backend.py"

BACKEND_DIST="$BACKEND_DIR/dist/nesting_server"
if [ ! -d "$BACKEND_DIST" ]; then
    error "PyInstaller output not found: $BACKEND_DIST"
    exit 1
fi
ok "Backend built successfully: $BACKEND_DIST"

# ── Step 2: Build Flutter Desktop ───────────────────────────────────────────

log "Step 2/4: Building Flutter desktop app..."

if [ ! -d "$FRONTEND_DIR" ]; then
    error "Frontend directory not found: $FRONTEND_DIR"
    exit 1
fi

cd "$FRONTEND_DIR"
flutter pub get
flutter build "$PLATFORM" --release
ok "Flutter $PLATFORM build completed."

# ── Step 3: Bundle Backend into Flutter App ─────────────────────────────────

log "Step 3/4: Bundling backend into Flutter app..."

if [ "$PLATFORM" = "macos" ]; then
    # macOS: Runner.app/Contents/Resources/nesting_server/
    APP_BUNDLE="$FRONTEND_DIR/build/macos/Build/Products/Release/sheet_nesting_app.app"
    if [ ! -d "$APP_BUNDLE" ]; then
        # Try alternate name from CMakeLists
        APP_BUNDLE=$(find "$FRONTEND_DIR/build/macos/Build/Products/Release" -name "*.app" -maxdepth 1 | head -1)
    fi
    if [ -z "$APP_BUNDLE" ] || [ ! -d "$APP_BUNDLE" ]; then
        error "Could not find .app bundle in build output."
        exit 1
    fi
    RESOURCES_DIR="$APP_BUNDLE/Contents/Resources"
    DEST_DIR="$RESOURCES_DIR/nesting_server"
    mkdir -p "$DEST_DIR"
    cp -R "$BACKEND_DIST/"* "$DEST_DIR/"
    chmod +x "$DEST_DIR/nesting_server"
    ok "Backend bundled into: $DEST_DIR"

elif [ "$PLATFORM" = "linux" ]; then
    # Linux: bundle/nesting_server/
    BUNDLE_DIR="$FRONTEND_DIR/build/linux/x64/release/bundle"
    if [ ! -d "$BUNDLE_DIR" ]; then
        error "Flutter Linux bundle not found: $BUNDLE_DIR"
        exit 1
    fi
    DEST_DIR="$BUNDLE_DIR/nesting_server"
    mkdir -p "$DEST_DIR"
    cp -R "$BACKEND_DIST/"* "$DEST_DIR/"
    chmod +x "$DEST_DIR/nesting_server"
    ok "Backend bundled into: $DEST_DIR"
fi

# ── Step 4: Create Distributable Package ────────────────────────────────────

log "Step 4/4: Creating distributable package..."

mkdir -p "$RELEASE_DIR"

if [ "$PLATFORM" = "macos" ]; then
    DMG_NAME="SheetNestingApp-$(date +%Y%m%d).dmg"
    DMG_PATH="$RELEASE_DIR/$DMG_NAME"

    # Try create-dmg if available (prettier DMG with background, etc.)
    if command -v create-dmg &>/dev/null; then
        log "Using create-dmg for fancy DMG..."
        create-dmg \
            --volname "Sheet Nesting App" \
            --window-pos 200 120 \
            --window-size 600 400 \
            --icon-size 100 \
            --icon "$(basename "$APP_BUNDLE")" 150 200 \
            --app-drop-link 450 200 \
            --no-internet-enable \
            "$DMG_PATH" \
            "$APP_BUNDLE" \
        || {
            warn "create-dmg failed, falling back to hdiutil..."
            hdiutil create -volname "Sheet Nesting App" \
                -srcfolder "$APP_BUNDLE" \
                -ov -format UDZO \
                "$DMG_PATH"
        }
    else
        log "create-dmg not found, using hdiutil..."
        hdiutil create -volname "Sheet Nesting App" \
            -srcfolder "$APP_BUNDLE" \
            -ov -format UDZO \
            "$DMG_PATH"
    fi
    ok "macOS DMG created: $DMG_PATH"

elif [ "$PLATFORM" = "linux" ]; then
    ARCHIVE_NAME="SheetNestingApp-linux-$(date +%Y%m%d).tar.gz"
    ARCHIVE_PATH="$RELEASE_DIR/$ARCHIVE_NAME"
    tar -czf "$ARCHIVE_PATH" -C "$(dirname "$BUNDLE_DIR")" "$(basename "$BUNDLE_DIR")"
    ok "Linux archive created: $ARCHIVE_PATH"
fi

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ Release build completed successfully!${NC}"
echo -e "${GREEN}  Platform:  $PLATFORM${NC}"
echo -e "${GREEN}  Output:    $RELEASE_DIR${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
