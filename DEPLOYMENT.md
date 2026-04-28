# BambuCLI Deployment & Security Checklist

Status as of 2026-04-24: pipeline + web UI complete, receipt printer wired, ledger working. Ready to move off the dev Mac onto a dedicated staff-only host.

This doc covers two things:
1. Picking a deploy path (Mac mini vs Raspberry Pi)
2. A pre-launch checklist — especially the **Security & Privacy** section below, which is what to walk through with the library supervisor / privacy officer before the tool goes live.

---

## Pick a deployment path

### Option A — Mac mini at the makerspace (lowest friction)

**Recommended if a Mac is already available.** Everything in `SETUP.md` was built and tested on macOS; zero code changes needed.

| | Mac mini |
| --- | --- |
| Hardware cost | $0 if already owned, else $600+ used |
| Slicer performance | Fast (OrcaSlicer M-series: queen.stl ~4s) |
| Runs as | LaunchDaemon (see `SETUP.md`) |
| Thumbnails | Label-style only — no OpenGL in CLI context on macOS |
| Dual use | Can also serve as a customer-facing kiosk via Guest profile |
| Power draw | ~10–30 W |

### Option B — Raspberry Pi 5 (cheapest dedicated host)

**Recommended if no spare Mac.** Pi 5 (8GB+) is very capable for this workload.

| | Raspberry Pi 5 (8GB) |
| --- | --- |
| Hardware cost | ~$120–160 CAD including SD card + case + PSU |
| Slicer performance | 3–5× slower than M-series Mac (queen.stl ~30s) |
| Runs as | systemd service |
| Thumbnails | Can render real 3D previews via Xvfb (OrcaSlicer + Linux virtual display) |
| Dual use | Dedicated appliance, no GUI kiosk function |
| Power draw | ~5–10 W |

**Differences from the Mac setup:**
- OrcaSlicer AppImage from `github.com/SoftFever/OrcaSlicer/releases` (ARM64 build). Make sure to grab the Linux ARM asset, not x86_64.
- Install `libusb-1.0-0-dev` via apt for pyusb (replaces `brew install libusb`).
- No `DYLD_FALLBACK_LIBRARY_PATH` trick needed — Linux linker finds libusb via the system path.
- Use systemd instead of launchd; unit file template at bottom of this doc.
- Xvfb for real thumbnails: `sudo apt install xvfb`, wrap OrcaSlicer calls with `xvfb-run`. Would need a small change to `SLICER_CLI` in `slice_order.py` to pass through `xvfb-run`. Worth the effort — gives you proper 3D previews the printer can display.

### Option C — Dual-use Mac (customer-facing kiosk + service running underneath)

**Good fit when a makerspace-area Mac is already powered on 24/7 for Guest-session kiosk use.** BambuCLI runs as a hidden unprivileged user in the background; the kiosk Guest session and any admin logins are untouched. Exploits the same LaunchDaemon-plus-Fast-User-Switching architecture from `SETUP.md`, but with stricter user isolation because the Mac is physically exposed to patrons.

**Layout:**

```
Mac (always on, customer-facing)
├── Login screen shows: Guest User [only]
├── Admin account (hidden)       ← you, for maintenance via Fast User Switching
├── bambucli account (hidden)    ← service runs as this user; standard, NOT admin
│   └── ~/BambuCLI/              ← chmod 700, owned by bambucli
└── LaunchDaemon (system-level, UserName=bambucli)
```

**Setup steps:**

1. Create the service user as **standard** (not admin):
   ```bash
   sudo sysadminctl -addUser bambucli -fullName "BambuCLI Service" -password -
   ```
   (You'll be prompted for a password. Pick something strong — you'll only type it when editing the LaunchDaemon or doing maintenance.)

2. Hide the user from the login screen:
   ```bash
   sudo dscl . create /Users/bambucli IsHidden 1
   ```

3. Copy the BambuCLI folder into the service user's home and lock permissions:
   ```bash
   sudo cp -R /path/to/BambuCLI /Users/bambucli/
   sudo chown -R bambucli:staff /Users/bambucli/BambuCLI
   sudo chmod -R 700 /Users/bambucli/BambuCLI
   ```

4. Prevent Spotlight from indexing the service user's home so Guest-session search can't surface patron filenames:
   ```bash
   sudo touch /Users/bambucli/.metadata_never_index
   ```

5. Install deps **as the bambucli user** so Python packages land in their home, not yours:
   ```bash
   sudo -u bambucli pip3 install --user \
       fastapi uvicorn python-multipart jinja2 \
       python-escpos pyusb pillow
   ```

6. Edit the LaunchDaemon plist from `SETUP.md` so `UserName` is `bambucli`, `WorkingDirectory` is `/Users/bambucli/BambuCLI`, and `ProgramArguments` points at `/Users/bambucli/Library/Python/3.9/bin/uvicorn`.

7. Load the daemon. It'll start running as bambucli and survive reboots.

**Security delta vs. Option A (dedicated non-public Mac):**

| Concern | Option A | Option C |
| --- | --- | --- |
| Physical access risk | Low (locked staff room) | Higher (patron kiosk — recovery-mode boot attempts are possible) |
| Mitigation | FileVault recommended | FileVault **required**, non-negotiable |
| Inter-session isolation | N/A (single user) | Guest sandbox + standard-user service = two layers |
| Service privilege | Admin user runs service | Standard user runs service (stricter blast radius) |

**Extra checklist items specific to this scenario:**

- [ ] `bambucli` is a **standard** account, not admin. Verify: `dscl . read /Groups/admin GroupMembership` should NOT list bambucli.
- [ ] `bambucli` hidden from the login screen. Verify: log out, confirm only Guest and your admin profile appear (or just Guest if admin is also hidden).
- [ ] `/Users/bambucli/BambuCLI/` is `chmod 700` owned by bambucli. Verify: from a Guest session, `ls /Users/bambucli/` should return "Permission denied".
- [ ] `.metadata_never_index` present in `/Users/bambucli/`. Verify with `ls -la /Users/bambucli/ | grep metadata`.
- [ ] **FileVault is enabled.** No exceptions for this scenario — the Mac is patron-accessible.
- [ ] Guest's "Erase on logout" is on (macOS default; reconfirm in System Settings → Users & Groups → Guest User).
- [ ] Screen Time / Parental Controls on the Guest account disables USB mass-storage mounting if patrons have no legitimate reason to plug drives into the kiosk.
- [ ] Network stack note: the service still binds port 8000. Staff on the same LAN reach it at `http://<mac>.local:8000/` or the Mac's static IP. Guest session has no reason to know that port exists.

---

## Pre-launch checklist

### Infrastructure

- [ ] Host Mac/Pi has a **static IP** (reserve in router DHCP or set manually).
- [ ] Host is physically in a **non-public location** — staff-only room; not the customer-facing floor.
- [ ] BambuCLI folder copied to host, paths verified by running `python3 slice_order.py inspect <existing 3mf>` successfully.
- [ ] Dependencies installed: OrcaSlicer, Python 3, Pillow, python-escpos + pyusb + libusb, FastAPI + uvicorn + python-multipart + jinja2.
- [ ] TM-T88V printer connected; `--send` roundtrip tested once end-to-end.
- [ ] Mac: LaunchDaemon loaded and KeepAlive verified (kill the process, confirm it restarts).
- [ ] Pi: systemd unit loaded and enabled on boot.
- [ ] Firewall allows inbound on port 8000 from the makerspace LAN only (not the public internet).
- [ ] Staff member on a different device successfully opens `http://<host>:8000/` and submits a test order.

### Security & privacy — bring this section to the boss meeting

- [ ] **FileVault (Mac) / LUKS (Pi)** full-disk encryption enabled on the host. Protects patron PII if hardware is lost/stolen.
  - *Why*: `printqueue/` stores full names, 14-digit library cards, and sometimes emails. Unencrypted disk = a stolen device leaks all of it.
- [ ] **Privacy Impact Assessment (PIA)** initiated with Town of Ajax's privacy officer. MFIPPA requires this for any new municipal system that collects patron identifiers.
- [ ] **Retention policy** agreed with privacy officer. Proposed: auto-delete `printqueue/processed/*.eml` and `printqueue/work/<timestamp>-*/` after 30 days; delete sliced 3MFs after 7 days post-pickup. Not yet implemented — add as a scheduled cleanup job once the retention window is confirmed.
- [ ] **Network scope** confirmed. Tool is bound to the **staff-only Wi-Fi / wired network** only. Confirmed with IT that the subnet is isolated from patron Wi-Fi.
- [ ] **No public exposure**. No port forwarding, no Cloudflare Tunnel with open access, no public DNS pointing at this host. Remote staff access (if needed) goes through Tailscale or the library's VPN.
- [ ] **Auth decision** documented. Current state: no authentication — relies on network isolation. If the host ever becomes reachable beyond the staff LAN, a shared password or SSO must be added before that change.
- [ ] **HTTPS decision** documented. Current state: plain HTTP on LAN. If hosting changes or if `library-pii traffic` flows over shared Wi-Fi, add a self-signed cert with uvicorn's `--ssl-keyfile` / `--ssl-certfile` args.
- [ ] **Audit log** reviewed. Every submission appends a record to `printqueue/orders.json` with timestamp, patron, card, files, mass, time, price. This is the primary evidence trail.
- [ ] **Backup strategy** in place. Proposed: weekly Time Machine (Mac) or rsync-to-external-drive (Pi) of the `BambuCLI/` folder. Rotate monthly. Confirm destination disk is also encrypted.
- [ ] **Incident response runbook** written. Minimum contents: (1) who to notify within the library (privacy officer), (2) how to preserve `printqueue/orders.json` as evidence, (3) MFIPPA notice-of-breach obligation to the Ontario IPC if patron PII is exposed.
- [ ] **Third-party dependencies** reviewed. Main ones: FastAPI (MIT), OrcaSlicer (AGPLv3 — but we use it as an unmodified CLI, so obligations are limited to not redistributing modified binaries), python-escpos (MIT), Pillow (MIT-CMU). All OSI-approved, no commercial licensing needed.
- [ ] **No telemetry or cloud services**. Pipeline is fully local — no calls to Anthropic, Bambu Cloud, or other external services during normal operation. (Receipt printing, slicing, ledger export all stay on-device.)
- [ ] **Disclosure text** on the intake form reviewed by privacy officer before go-live. Draft: *"This form collects your name and library card number for the purpose of processing your 3D print order. Data is retained for 30 days and used only by makerspace staff. Questions: privacy@ajax.ca."* — adjust wording per their guidance.

### Operational

- [ ] Two staff members have walked through a real order end-to-end (intake form → slice → SD card → printer → receipt → pickup).
- [ ] Runbook exists for common failure modes: slicer crash, printer out of paper, 3MF rejected by X1C, network goes down.
- [ ] Staff know how to access `printqueue/orders.json` / `ledger.csv` for billing reconciliation.
- [ ] Staff know how to stop/restart the service (`launchctl kickstart -k system/com.makerspace.bambucli` on Mac; `systemctl restart bambucli` on Pi).

---

## Security review — one-paragraph summary for the boss meeting

> BambuCLI is a 3D print intake tool that runs entirely on a single dedicated host on the library's internal staff network. It collects patron names and 14-digit library card numbers via a web form, slices STL files locally, and prints a paper receipt. No patron data leaves the device. Disk-level encryption (FileVault / LUKS) is enabled to protect PII at rest, and the host is not reachable from the public internet or patron Wi-Fi. If co-located with a customer-facing Guest kiosk, the service runs as a hidden standard (non-admin) user with `chmod 700` permissions, so the Guest session cannot read patron data even via Terminal. An audit trail of every order is written to `orders.json` and can be exported as CSV for billing reconciliation. A 30-day retention policy on raw emails and working files is recommended, pending final agreement with the privacy officer. Risks are limited to (a) theft or physical tampering with the host, mitigated by encryption, and (b) a staff account with host access, mitigated by the library's existing HR-level controls. The tool has no external dependencies at runtime (no cloud APIs, no LLM calls) and uses only OSI-approved open-source libraries.

---

## Raspberry Pi systemd unit (reference)

```ini
# /etc/systemd/system/bambucli.service
[Unit]
Description=Makerspace BambuCLI intake web app
After=network-online.target

[Service]
Type=simple
User=makerspace
WorkingDirectory=/home/makerspace/BambuCLI
ExecStart=/home/makerspace/.local/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5s
StandardOutput=append:/var/log/bambucli.log
StandardError=append:/var/log/bambucli.err

[Install]
WantedBy=multi-user.target
```

Install and enable:
```
sudo systemctl daemon-reload
sudo systemctl enable --now bambucli
sudo journalctl -u bambucli -f        # tail logs
```

---

## Outstanding work items (pick up here next time)

- [ ] **Multi-colour slicing fix (high priority)** — `slice_order.py slice --color "A,B,C"` currently uses the colour list only for label/filename metadata, not for actual slicer configuration. Multi-colour orders submitted through the web UI silently produce a single-filament 3MF with everything on one plate. Fix: when comma-separated colours are detected, internally split into per-colour slice calls and merge using `_merge_3mfs` (the pattern that already works for John-Hugh, Nakib, Mobi when run manually). ~50 lines in `cmd_slice`.
- [ ] Retention sweep: daily/weekly job that prunes `printqueue/processed/` and `printqueue/work/*-web/` older than 30 days.
- [ ] Per-file scale support: requires per-scale-bucket slicing + multi-plate-aware merge. ~100 lines in `slice_order.py`.
- [ ] Optional auth layer: shared password in an env var, checked via FastAPI dependency. ~15 lines.
- [ ] File-size cap + MIME sniff on uploads to reject anything non-STL/3MF before saving to disk.
- [ ] Receipt auto-print checkbox on the result page (currently one manual click).
- [ ] Migrate ledger from JSON blob to SQLite when order count > ~1000 — JSON loads into memory on every CSV export.
- [ ] Paste-email-to-prefill helper (optional local LLM via Ollama) — adds speed for staff but not required.
