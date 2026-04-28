# Tailscale Setup for BambuCLI

Tailscale gives the makerspace Mac and staff laptops a private encrypted mesh that works regardless of which physical network each device is on. Once configured, staff browses to the BambuCLI web app from anywhere — home Wi-Fi, a different floor of the library, a public network — using the same URL.

This doc walks through first-time setup. Plan on ~15 minutes for the host + ~3 minutes per laptop.

---

## 1. Decide on the account model (do once)

Tailscale's free tier has two shapes:

| Plan | Users | Devices | Cost |
| --- | --- | --- | --- |
| **Personal** | 1 | up to 100 | $0 forever |
| **Starter** (free) | up to 3 | up to 100 | $0 forever |
| **Premium** | unlimited | unlimited | $6/user/month |

For a makerspace, the cleanest free option is the **shared-account pattern**: one Google or Microsoft account (e.g. `makerspace@ajaxlibrary.ca`) that everyone signs into. The host Mac and every staff laptop are then "devices" under that single user — no per-staff license tax.

Alternative: Starter free plan with up to 3 personal accounts, if you want each staff to have their own Tailscale identity for audit purposes.

Either way works for the BambuCLI workflow. Pick one and stick with it before adding the first device.

---

## 2. Sign up for the Tailscale account (browser, on any device)

1. Go to **https://login.tailscale.com/start**
2. Click "Sign up with Google" (or Microsoft / GitHub / email).
3. If using the shared-account pattern: sign in with the makerspace's shared Google account.
4. You'll land in the Tailscale admin console at **https://login.tailscale.com/admin/machines** — empty for now. Keep this tab open; you'll come back to it.

That's the entire account setup. No payment, no plan picker.

---

## 3. Set up the host Mac (the one running BambuCLI)

If `install.sh` already ran, Tailscale is installed via Homebrew. If not:

```bash
brew install tailscale
```

Start the daemon and authenticate:

```bash
sudo brew services start tailscale
sudo tailscale up
```

The second command prints a URL. Open it in any browser, log in with the same account from step 2, and click "Connect". The terminal returns to the prompt once auth completes.

Verify the host is on the tailnet:

```bash
tailscale status
```

Should show this Mac and any other already-joined devices. Note the **hostname** (left column) — it's typically the Mac's name as set in System Settings → General → Sharing. You can rename it later in the Tailscale admin console if you want something cleaner like `makerspace-mac`.

The Mac now has a stable Tailscale IP (100.x.x.x) and a MagicDNS hostname like `makerspace-mac.tail-xxxx.ts.net`.

---

## 4. Set up each staff laptop

Two options per laptop:

**Option A: GUI app (easiest for non-technical users)**

1. Download the macOS app: https://tailscale.com/download/mac
2. Drag Tailscale.app into Applications, launch it.
3. Click "Log In", sign in with the same account from step 2.
4. The menu bar gets a Tailscale icon — green when connected.

**Option B: Homebrew CLI (for terminal-comfortable staff)**

```bash
brew install tailscale
sudo brew services start tailscale
sudo tailscale up
```

Same browser-auth flow as the host setup. End result is identical to the GUI app.

After each laptop joins, refresh the Tailscale admin console (https://login.tailscale.com/admin/machines) and confirm it appears in the device list.

---

## 5. Test the connection

From any staff laptop after joining the tailnet, open in a browser:

```
http://<host-mac-tailscale-name>:8000/
```

Replace `<host-mac-tailscale-name>` with whatever shows in `tailscale status` for the host. The intake form should load just like it does over LAN.

If it doesn't:

```bash
# On the laptop
ping <host-mac-tailscale-name>
```

If ping works but the browser fails: BambuCLI service isn't running on the host. SSH or screen-share in and check `tail -f /var/log/bambucli.log`.

If ping fails: Tailscale isn't routing. Run `tailscale status` on both ends, both should show "active". Check the admin console for either device showing as "expired" — re-auth if so.

---

## 6. Optional: clean up MagicDNS hostnames

By default, hostnames look like `aria-macbook.tail-1234.ts.net`. To get something nicer:

1. Tailscale admin console → **DNS** → enable **MagicDNS** (already on by default).
2. **Machines** → click the host Mac → "Edit machine name" → set to `makerspace-mac`.
3. Now staff types `http://makerspace-mac:8000/` (no domain suffix needed within the tailnet).

---

## 7. Optional: lock down with ACLs

Free tier supports ACLs in the admin console under **Access controls**. Default is "everyone can reach everyone" within the tailnet, which is fine for a small staff team. If you want to restrict the Mac so only specific staff laptops can hit it, edit the JSON ACL — Tailscale's docs cover the syntax.

For BambuCLI's threat model (everyone on the tailnet is trusted staff), the default ACL is appropriate.

---

## 8. Adding a new staff member later

1. Have them sign in with the shared makerspace account (or invite them as a new user under the Starter plan, depending on the pattern you picked).
2. Install Tailscale per step 4.
3. Done — no changes to the host Mac needed.

---

## 9. Removing a staff laptop (e.g. someone leaves)

Tailscale admin console → Machines → click the device → "Remove". The laptop instantly loses tailnet access. If the device is still around, also tell the user to log out of the Tailscale app on it.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `tailscale up` hangs without printing a URL | Check the venue's network blocks UDP — Tailscale falls back to DERP-relayed connections, which still work but slower. Try a different network. |
| Staff laptop sees host in `tailscale status` but browser times out | BambuCLI service isn't bound to `0.0.0.0`. Confirm the LaunchDaemon plist has `--host 0.0.0.0` (the default in `install_service.sh`). |
| `tailscale status` shows host as "offline" | `sudo brew services restart tailscale` on the host. |
| Receipt-printer USB stops working when the service is on a remote network | Unrelated — Tailscale doesn't route USB. The printer must be physically attached to whichever Mac runs the service. |
| Need to access from outside the makerspace temporarily, without joining tailnet | Use `tailscale serve` on the host to publish a one-shot share link. See `tailscale serve --help`. |

---

## What Tailscale does NOT do

- Doesn't share USB devices across the tailnet — printer still has to be on the host Mac.
- Doesn't replace HTTPS — traffic between tailnet members is WireGuard-encrypted, but the BambuCLI app speaks plain HTTP. Use `tailscale serve --tls` if you want encryption end-to-end (overkill for staff-only).
- Doesn't bypass the OS firewall on macOS — the first time someone hits the service from a Tailscale peer, macOS still asks "Allow incoming connections for Python?" Click Allow once.

---

## File pointers

- `install.sh` installs Tailscale via brew but does NOT auto-authenticate (`tailscale up` is interactive).
- `DEPLOY_README.md` has a 2-line summary of the steps; this doc is the long form.
- `DEPLOYMENT.md` covers the security implications under "Networking" (data flows P2P, doesn't transit Tailscale's servers in cleartext, MFIPPA-friendly).

---

## Real-world deployment lessons (Apr 2026)

Notes from the first actual deploy onto the makerspace iMac. Each was a real wall we hit; future deploys can short-circuit them.

### 1. The Mac App Store / GUI Tailscale.app dies on logout

The standalone `Tailscale.app` (whether from tailscale.com or the App Store) is a per-user GUI app. It runs only while you're logged into the Mac console; logging out terminates it, and remote tailnet devices see the host as "offline" until someone logs back in. Same failure mode as a LaunchAgent.

**Fix:** install the Homebrew CLI version, which registers `tailscaled` as a system-level launchd service:

```bash
osascript -e 'tell application "Tailscale" to quit' 2>/dev/null
brew install tailscale
sudo brew services start tailscale
sudo tailscale up --operator=$(whoami)
```

The `--operator=$USER` flag lets the named user run `tailscale` commands without sudo for daily ops. Set it once during the initial `tailscale up` so you don't have to chase IT for every status check later.

### 2. CLI not on PATH after GUI install

If the user starts with the GUI Tailscale.app and later wants `tailscale` in Terminal, the binary lives buried in the bundle:

```bash
sudo ln -s /Applications/Tailscale.app/Contents/MacOS/Tailscale /usr/local/bin/tailscale
```

Skipped entirely if you go straight to the Homebrew install (path 1) — that puts `tailscale` on PATH automatically.

### 3. Sudo restrictions on managed Macs

The user account on the makerspace iMac was not in the sudoers file. IT had to remote in to run `sudo brew install` / `sudo tailscale up`. Worth scheduling a single ~10-minute IT touch that bundles ALL the privileged steps:

- Homebrew install (if not present)
- `brew install tailscale` and `brew install libusb`
- `sudo brew services start tailscale`
- `sudo tailscale up --operator=$THE_USER`
- `sudo /Applications/Tailscale.app/Contents/MacOS/Tailscale set --operator=$USER` (only if keeping the GUI app)
- The macOS Application Firewall rule fix (see #5 below) — preferably batched into the same session

After that one window, the user can deploy and maintain BambuCLI without further admin escalation, except for `install_service.sh` and per-update `launchctl kickstart`.

### 4. LaunchDaemon must be loaded into the **system** domain, not the user GUI domain

The legacy `sudo launchctl load <plist>` command loads into whatever domain the invoking shell happens to be in. If you're running it from a logged-in Terminal session, that's `gui/$UID`, which means **the daemon dies when the user logs out** — defeating the entire point of a LaunchDaemon.

**Use the modern bootstrap syntax instead:**

```bash
sudo launchctl bootout system /Library/LaunchDaemons/com.makerspace.bambucli.plist 2>/dev/null
sudo launchctl bootstrap system /Library/LaunchDaemons/com.makerspace.bambucli.plist
sudo launchctl enable system/com.makerspace.bambucli
```

Verify with `sudo launchctl print system/com.makerspace.bambucli | head` — should show `type = LaunchDaemon` and `state = running`. `install_service.sh` was patched to do this automatically; older copies of the script may still use `load`.

### 5. macOS Application Firewall blocks the Python framework binary

Even after explicitly whitelisting `/usr/bin/python3` and `/Library/Developer/CommandLineTools/usr/bin/python3`, the daemon's listening socket was still rejected with `Connection reset by peer`. Reason: when uvicorn's shebang resolves through the CLT Python stub, the underlying process binary that the firewall sees is actually:

```
/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app
```

This is a *different* allow-list entry from the others, and macOS auto-creates it as **Block** the first time the daemon tries to accept a connection (because daemons can't show the GUI prompt that asks the user to allow it).

**Diagnostic:**
```bash
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --listapps
```
Look for an entry containing `Python.app` set to "Block incoming connections".

**Fix:**
```bash
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp \
  "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app"
sudo launchctl kickstart -k system/com.makerspace.bambucli
```

The `kickstart` is required — the firewall caches deny decisions per-process, so the running daemon ignores the rule change until restarted. Replace the `3.9` in the path with whatever Python version the daemon's `uvicorn` resolves to (`head -1 ~/Library/Python/*/bin/uvicorn`).

This rule may auto-revert when macOS or CommandLineTools updates Python — the binary's signing identity changes and the firewall re-quarantines it. Symptom: the URL stops working out of nowhere with `Connection reset by peer`. Recovery is the same two commands.

### 6. /var/log ownership for daemon redirects

The LaunchDaemon plist redirects stdout/stderr to `/var/log/bambucli.log` and `bambucli.err`. `/var/log/` is root-owned, but the daemon runs as the regular user (set via `UserName` in the plist). Without pre-creating the log files with the right owner, launchd can't open them, and the daemon exits with config error 78 before the Python app starts.

`install_service.sh` now does this automatically:

```bash
sudo touch /var/log/bambucli.log /var/log/bambucli.err
sudo chown "$USER_NAME" /var/log/bambucli.log /var/log/bambucli.err
```

If you ever change the daemon's `UserName`, also re-chown the log files or you'll get exit 78 again.

### 7. Stray local uvicorn on a staff laptop

A staff laptop happened to have its own `uvicorn` running on port 8000 from earlier testing. When the user typed `http://localhost:8000/` (or even just bookmarked the wrong host), they hit the laptop's local instance — which had a stale ledger — and assumed the host wasn't syncing. Quick check:

```bash
lsof -i :8000
```

If a Python process listens on port 8000 on a staff laptop, kill it. Bookmarks should always use the host's tailnet name (or IP), never `localhost`.

---

## Recovery playbook (paste this in a tear-out for desk staff)

If the URL stops working from a staff laptop:

1. **Tailscale on the laptop is connected?**
   - Menu bar icon should be solid; click it → host should appear under "My Devices"
   - If not connected: click "Log in" with the makerspace account
2. **Host is online?**
   - From laptop: `tailscale ping <host-name>` should show timing
3. **App is reachable from the laptop?**
   - `curl http://<host-name>:8000/` returns 200 if so
4. **`Connection reset by peer` from laptop?**
   - Likely the macOS firewall blocked Python.app again (after a system update)
   - On host: `sudo /usr/libexec/ApplicationFirewall/socketfilterfw --listapps | grep -A1 Python.app`
   - If it says "Block": run the unblock + kickstart from #5 above
5. **Host shows offline in Tailscale?**
   - On host: `sudo brew services restart tailscale`
   - Check: `tailscale status`
6. **Daemon stopped?**
   - On host: `sudo launchctl print system/com.makerspace.bambucli | grep state`
   - Should be `running`. If not: `sudo launchctl kickstart -k system/com.makerspace.bambucli`
