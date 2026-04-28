#!/bin/bash
# Install BambuCLI as a macOS LaunchDaemon so it starts at boot, survives
# reboots, and runs in the background regardless of which user is logged in
# at the console (so a customer Guest session up front doesn't affect it).
#
# Run AFTER install.sh succeeds. Asks for sudo twice (cp + launchctl load).

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(whoami)"
PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
UVICORN="$HOME/Library/Python/$PY_VERSION/bin/uvicorn"
BREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
PLIST_PATH="/Library/LaunchDaemons/com.makerspace.bambucli.plist"

[[ -x "$UVICORN" ]] || fail "uvicorn not found at $UVICORN — run install.sh first."
[[ -d "$SCRIPT_DIR" ]] || fail "Working directory missing"

# Generate the plist with absolute paths from this host
TMP_PLIST="$(mktemp)"
cat > "$TMP_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.makerspace.bambucli</string>
    <key>UserName</key>
    <string>$USER_NAME</string>
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
    <string>/var/log/bambucli.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/bambucli.err</string>
</dict>
</plist>
EOF

# Unload any existing version first (idempotent). Use bootout (modern) and
# the legacy unload as a fallback in case it was originally loaded that way.
sudo launchctl bootout system "$PLIST_PATH" 2>/dev/null || true
sudo launchctl unload "$PLIST_PATH" 2>/dev/null || true

sudo cp "$TMP_PLIST" "$PLIST_PATH"
sudo chmod 644 "$PLIST_PATH"
sudo chown root:wheel "$PLIST_PATH"
rm "$TMP_PLIST"

# Pre-create log files with the daemon's user as owner. /var/log/ is root-only;
# without this step launchd can't open the redirect paths and the daemon exits
# with config error 78 before the Python app even starts.
sudo touch /var/log/bambucli.log /var/log/bambucli.err
sudo chown "$USER_NAME" /var/log/bambucli.log /var/log/bambucli.err

# Bootstrap into the SYSTEM domain (vs the user GUI domain). On macOS the
# legacy `launchctl load` can land the service in whichever domain the
# invoking shell happens to be in, which means logout kills it. `bootstrap
# system` always puts it in the long-running system domain.
sudo launchctl bootstrap system "$PLIST_PATH"
sudo launchctl enable system/com.makerspace.bambucli
ok "LaunchDaemon installed and started in the system domain"

cat <<EOF

Plist:    $PLIST_PATH
Logs:     tail -f /var/log/bambucli.log /var/log/bambucli.err
Restart:  sudo launchctl kickstart -k system/com.makerspace.bambucli
Stop:     sudo launchctl bootout system $PLIST_PATH
Re-start: sudo launchctl bootstrap system $PLIST_PATH

The service is now running on port 8000 in the system domain — survives
reboots, crashes, and user logout.

EOF
