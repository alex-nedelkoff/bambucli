#!/bin/bash
# No-sudo alternative to install_service.sh.
#
# Installs BambuCLI as a per-user LaunchAgent (~/Library/LaunchAgents/) instead
# of a system-level LaunchDaemon (/Library/LaunchDaemons/). Trade-off:
#
#   LaunchDaemon (install_service.sh)
#     - runs even when nobody is logged in at the console
#     - needs sudo to install
#
#   LaunchAgent (this script)
#     - runs only while your user is logged in (Fast User Switching counts)
#     - no admin password needed
#
# For a Mac you log into and leave running, the agent is functionally equivalent
# and saves you a trip to IT. If the Mac reboots, log in once and the agent
# starts automatically.

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
UVICORN="$HOME/Library/Python/$PY_VERSION/bin/uvicorn"
BREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
PLIST_PATH="$HOME/Library/LaunchAgents/com.makerspace.bambucli.plist"

[[ -x "$UVICORN" ]] || fail "uvicorn not found at $UVICORN — run install.sh first."

mkdir -p "$HOME/Library/LaunchAgents"

# Logs go in user-writable space so we don't need sudo.
LOG_DIR="$HOME/Library/Logs/BambuCLI"
mkdir -p "$LOG_DIR"

# Unload any existing version (idempotent)
launchctl unload "$PLIST_PATH" 2>/dev/null || true

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.makerspace.bambucli</string>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UVICORN</string>
        <string>app:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8000</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DYLD_FALLBACK_LIBRARY_PATH</key>
        <string>$BREW_PREFIX/lib</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/bambucli.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/bambucli.err</string>
</dict>
</plist>
EOF

launchctl load "$PLIST_PATH"
ok "LaunchAgent installed and started"

cat <<EOF

Plist:    $PLIST_PATH   (no sudo required to edit)
Logs:     tail -f $LOG_DIR/bambucli.log $LOG_DIR/bambucli.err
Restart:  launchctl kickstart -k gui/$UID/com.makerspace.bambucli
Stop:     launchctl unload $PLIST_PATH

Service runs whenever you're logged in. After a reboot, log in once and it
auto-starts. If the Mac is shared, leave Fast User Switching off — the agent
needs your session active.

EOF
