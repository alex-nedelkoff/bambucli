#!/bin/bash
# Bootstrap installer for BambuCLI on a fresh macOS host.
# Copy this whole BambuCLI folder onto the target Mac and run:
#
#     bash install.sh
#
# Idempotent — safe to re-run after upgrades.

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== BambuCLI installer ==="
echo "Working dir: $SCRIPT_DIR"
echo

# 1. macOS check
[[ "$(uname)" == "Darwin" ]] || fail "This installer is macOS-only. See DEPLOYMENT.md for the Raspberry Pi path."
ok "macOS detected ($(sw_vers -productVersion))"

# 2. OrcaSlicer (must be installed manually first — license + Gatekeeper)
if [[ ! -d "/Applications/OrcaSlicer.app" ]]; then
    cat <<EOF

${YELLOW}OrcaSlicer.app is not in /Applications.${NC}
  1. Download the latest release: https://github.com/SoftFever/OrcaSlicer/releases
  2. Drag OrcaSlicer.app into /Applications/
  3. Launch it once from Finder so macOS Gatekeeper allows the binary.
  4. Re-run this installer.

EOF
    exit 1
fi
ok "OrcaSlicer detected"

# 3. Homebrew (required for libusb + tailscale)
if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew not found — installing"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
ok "Homebrew available"

# Detect brew prefix (different on Apple Silicon vs Intel)
BREW_PREFIX="$(brew --prefix)"
LIBUSB_PATH="$BREW_PREFIX/lib"

# 4. libusb (for receipt printer USB access)
brew list libusb >/dev/null 2>&1 || brew install libusb
ok "libusb installed at $LIBUSB_PATH"

# 5. Tailscale (optional but recommended for cross-network access)
if ! command -v tailscale >/dev/null 2>&1 && [[ ! -d "/Applications/Tailscale.app" ]]; then
    read -p "Install Tailscale via Homebrew? [Y/n]: " yn
    if [[ ! "$yn" =~ ^[Nn]$ ]]; then
        brew install tailscale
        ok "Tailscale installed (run 'sudo brew services start tailscale && sudo tailscale up' to activate)"
    else
        warn "Skipping Tailscale — install manually later if needed"
    fi
else
    ok "Tailscale already installed"
fi

# 6. Python 3 (system-provided on macOS 12+)
PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
ok "Python $PY_VERSION detected"
PY_USER_BIN="$HOME/Library/Python/$PY_VERSION/bin"

# 7. Python dependencies
echo "Installing Python packages (--user, no system pollution)…"
pip3 install --user --quiet --upgrade \
    fastapi \
    uvicorn \
    python-multipart \
    jinja2 \
    python-escpos \
    pyusb \
    pillow
ok "Python packages installed to $PY_USER_BIN"

# 8. Smoke test the pipeline
SAMPLE_3MF="$(ls "$SCRIPT_DIR/printqueue/work"/*.3mf 2>/dev/null | head -1 || true)"
if [[ -n "$SAMPLE_3MF" ]]; then
    if python3 "$SCRIPT_DIR/slice_order.py" inspect "$SAMPLE_3MF" >/dev/null 2>&1; then
        ok "Pipeline smoke test passed"
    else
        warn "Pipeline smoke test failed — check OrcaSlicer install"
    fi
else
    warn "No sample 3MF in printqueue/work/ — skipping smoke test"
fi

# 9. Mac LAN address — useful for staff laptops to point at
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '')"
HOSTNAME_LOCAL="$(scutil --get LocalHostName 2>/dev/null || hostname).local"

cat <<EOF

=== Setup complete ===

Start the web app:
  $PY_USER_BIN/uvicorn app:app --host 0.0.0.0 --port 8000

Then reach it from any device on the same network:
  http://localhost:8000/                      (this Mac)
  http://${LAN_IP:-<lan-ip>}:8000/                (LAN)
  http://${HOSTNAME_LOCAL}:8000/    (mDNS)

To run as a background service that survives reboots and screen-locks:
  bash install_service.sh

For cross-network access (staff on different Wi-Fi):
  sudo brew services start tailscale
  sudo tailscale up
  (browser opens — log in with the makerspace account)

EOF
