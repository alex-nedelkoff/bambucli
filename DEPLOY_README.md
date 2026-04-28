# BambuCLI — Quick Deploy on a New Mac

Drop-in deployment guide. For full security context, retention policy, and the customer-facing-Mac scenario, read `DEPLOYMENT.md` after this.

## What you need on the target Mac

- macOS 12+ (Big Sur or later)
- An admin account (to install Homebrew + LaunchDaemon)
- `OrcaSlicer.app` already in `/Applications/` and launched once (https://github.com/SoftFever/OrcaSlicer/releases)
- Internet for the install (Homebrew + Python packages)
- (Optional) Epson TM-T88V plugged into USB — for receipt printing
- (Optional) Tailscale account if you need cross-network access

## 5-step deploy

1. **Copy the entire `BambuCLI/` folder** to the target Mac.
   AirDrop, USB stick, or `scp -r BambuCLI alex@new-mac:~/Documents/makerspace/`.
2. Open Terminal on the target Mac, `cd` into the copied folder.
3. **Bootstrap dependencies**:
   ```bash
   bash install.sh
   ```
   Walks through Homebrew, libusb, Python deps, optional Tailscale. Idempotent — safe to re-run.
4. **Test the web app manually first**:
   ```bash
   ~/Library/Python/3.9/bin/uvicorn app:app --host 0.0.0.0 --port 8000
   ```
   Open http://localhost:8000/ in a browser. Submit a test order. Verify the result page works. Ctrl-C to stop.
5. **Install as a background service** so it survives reboots and login switching:
   ```bash
   bash install_service.sh
   ```
   Asks for sudo. The service binds port 8000, restarts on crash, and runs as your user even when nobody is logged in at the console.

## Tailscale setup (cross-network access)

If staff laptops aren't on the same Wi-Fi as this Mac (different subnets, client isolation, remote work), Tailscale gives you a private mesh that bypasses all of that. **Full step-by-step in `TAILSCALE.md`** — covers account signup, host Mac auth, per-laptop install, MagicDNS hostnames, and troubleshooting.

Quick summary if you've already done it before:

```bash
# On the host Mac
sudo brew services start tailscale
sudo tailscale up                                # opens browser for login

# On each staff laptop
brew install tailscale && sudo brew services start tailscale && sudo tailscale up
# OR install the Tailscale.app from https://tailscale.com/download/mac

# After auth, staff opens this URL from any network:
http://<host-mac-tailscale-name>:8000/
```

Use a shared makerspace Google account so every device counts as one Tailscale user (free tier covers ~100 devices). See `TAILSCALE.md` §1 for the account-model trade-offs.

## Local-network access (no Tailscale)

If everyone's on the same Wi-Fi, just use the Mac's LAN IP or mDNS hostname (printed at the end of `install.sh`):

```
http://<this-mac-name>.local:8000/
http://<lan-ip>:8000/
```

Pin the LAN IP in your router's DHCP reservations so it doesn't change.

## What the installer does NOT do

- Doesn't create a dedicated `bambucli` standard user (read `DEPLOYMENT.md` Option C if the Mac is a customer-facing kiosk and you want stricter user isolation).
- Doesn't enable FileVault — do that manually if PII storage is a concern (System Settings → Privacy & Security → FileVault).
- Doesn't set up the retention sweep — that's still listed as outstanding work in `DEPLOYMENT.md`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `OrcaSlicer not found` during install | Drag `OrcaSlicer.app` to `/Applications/` and launch it once |
| Receipt printing errors with `No backend available` | Ensure Homebrew installed libusb (`brew list libusb`) and the daemon picks up `DYLD_FALLBACK_LIBRARY_PATH` (auto-set by `install_service.sh`) |
| Mac sleeps and the service becomes unreachable | System Settings → Battery → enable "Wake for network access" and disable computer sleep on power adapter |
| Service fails to start at boot | `tail -f /var/log/bambucli.err`, verify uvicorn path matches `$HOME/Library/Python/<ver>/bin/uvicorn` |
| Tailscale URL doesn't resolve | Confirm both devices logged into the same tailnet; check `tailscale status` on the Mac |

## File layout you just copied

```
BambuCLI/
├── install.sh                  ← bootstrap
├── install_service.sh          ← LaunchDaemon installer
├── DEPLOY_README.md            ← this doc
├── DEPLOYMENT.md               ← full deploy / security checklist
├── SETUP.md                    ← architecture + receipt printer + retention notes
├── app.py                      ← FastAPI web app
├── slice_order.py              ← slicer orchestrator (CLI)
├── process_cli.json            ← OrcaSlicer process overlay (X1C, 0.24 Draft, no brim, tree supports)
├── ELEGOO PLA No Aux Fan @Bambu Lab X1 Carbon 0.4 nozzle.json   ← filament preset
├── templates/                  ← Jinja2 HTML
├── static/style.css            ← brand styling
└── printqueue/
    ├── work/                   ← per-order workdirs + final 3MFs
    ├── inbox/                  ← (legacy — manual eml drop zone)
    ├── processed/              ← archived emails
    └── orders.json             ← ledger (one row per order)
```
