# BambuCLI Setup

Pipeline for turning a patron's 3D print order (`.eml` email) into a sliced `.3mf` ready for the X1C. Designed for the Town of Ajax Library Makerspace.

## What this does

1. Reads an `.eml` email with STL attachments + patron info (name, card number, color, quantity).
2. Invokes OrcaSlicer's CLI with X1C settings + ELEGOO PLA filament + tree supports.
3. Produces a printer-compatible `.3mf` with embedded print labels (color swatch, patron name, plate N-of-M, time, mass) as the plate thumbnails.
4. Drops the final file into `printqueue/work/` named like `Aria_Apr 21_41.8g_1h26m.3mf`.

Orchestration today runs through Claude Code using the `/slice-orders` skill at `~/.claude/skills/slice-orders/SKILL.md` (Claude parses the email body, the Python helper does the mechanical work). A lightweight web UI is planned — see the bottom of this doc.

## Prerequisites on the host Mac

1. **OrcaSlicer** — download the DMG from [github.com/SoftFever/OrcaSlicer/releases](https://github.com/SoftFever/OrcaSlicer/releases), drag `OrcaSlicer.app` into `/Applications/`. **Launch it once from Finder** so macOS Gatekeeper allows the binary — the CLI will be blocked from running otherwise.
2. **Python 3** — comes with macOS (`python3 --version` should print `3.x.x`). Version 3.9+ is fine.
3. **Pillow** — used to render the plate-label thumbnails:
   ```
   pip3 install pillow --break-system-packages
   ```
   (or use a venv if you prefer).

## Files to copy

Copy the entire `BambuCLI/` folder to the new host. Layout looks like:

```
BambuCLI/
├── slice_order.py                                   # the pipeline (orchestrator + CLI)
├── process_cli.json                                 # our 0.24mm Draft overlay (supports, G92 E0)
├── ELEGOO PLA No Aux Fan @Bambu Lab X1 Carbon 0.4 nozzle.json   # filament preset
├── printqueue/
│   ├── inbox/        # (optional future dir for .eml drops; currently emails land in printqueue/ directly)
│   ├── work/         # per-order workdirs + final sliced 3MFs
│   └── processed/    # archived .eml files after slicing
├── SETUP.md          # this file
└── Presets/          # older BambuStudio GUI exports (kept as reference, not used)
```

Also copy:
```
~/.claude/skills/slice-orders/SKILL.md
```
…if the new host will run Claude Code for email parsing. If the host only runs the Python helper (e.g. a future web app calls `slice_order.py` directly), the skill isn't needed.

`slice_order.py` resolves paths relative to its own location (`BASE_DIR = Path(__file__).resolve().parent`), so you can drop the folder anywhere — no user-specific paths to edit.

## Verify the install works

From inside `BambuCLI/`:

```bash
# should print a JSON summary of an existing sliced 3MF
python3 slice_order.py inspect "printqueue/work/Aria_Apr 21_41.8g_1h26m.3mf"
```

If that prints plate info (time, mass, objects), you're good.

To actually slice, point it at a workdir containing an STL:

```bash
python3 slice_order.py slice \
  --workdir "printqueue/work/test" \
  --customer "Test Patron" \
  --color "Black" \
  --stls "some_model.stl" \
  --clones "1"
```

Expected output is a JSON summary plus a new `.3mf` file in `printqueue/work/`.

## Pipeline details worth knowing

- **Slicer settings are partially synthesised at runtime.** `slice_order.py` resolves OrcaSlicer's profile inheritance chain (0.24mm Draft → fdm_process_bbl_0.24 → common) into a single merged JSON under `~/Downloads/_merged_{process,machine}.json` each invocation. This is because OrcaSlicer CLI doesn't walk the `inherits` chain itself — without merging, you get Slic3r defaults (60 mm/s walls, 1000 mm/s² accel) instead of real X1C speeds.
- **Thumbnails are label-style**, not 3D renders. OrcaSlicer CLI on macOS can't render real thumbnails (no OpenGL context). Instead, each plate gets a PNG showing: color swatch → patron name → plate N-of-M → time + mass → date. Useful on the printer's file browser.
- **Printer metadata patching.** After slicing, the helper sets `printer_model_id = BL-P001`, `extruder_type = 0`, `nozzle_volume_type = 0` in `Metadata/slice_info.config`. Without this the X1C shows "current nozzle setting does not match the slicing file" when you try to print.
- **Print-file strip for SD-card compatibility.** OrcaSlicer CLI writes a full "project" 3MF with mesh data in `3D/Objects/*.model` side files and `<object>` blocks in `model_settings.config`. BambuStudio's Send-to-Printer flow produces a leaner "print-file" 3MF: empty `3D/3dmodel.model`, no mesh data, only `<plate>` blocks in `model_settings.config`. **Multi-plate slicer outputs don't appear on the X1C SD-card browser in project form** — the firmware rejects them silently. `_strip_to_print_file()` in `slice_order.py` converts every output to print-file shape after slicing (harmless for single-plate, required for multi-plate).
- **Supports default to tree(auto), infill 15% grid, brim off.** Tuned in `process_cli.json`.
- **Max plate time is 5h.** If arrange splits a color bucket across multiple plates, each plate must come in under 5h or the pipeline flags `any_over_5h: true` in its output.

## If OrcaSlicer updates and breaks the CLI

The merged profile logic reads from `/Applications/OrcaSlicer.app/Contents/Resources/profiles/BBL/`. If an update changes field names or inheritance structure, the merge will silently pick up the new values — usually fine. Watch out for:
- Slicer crashes after an update → check `slice_info.config` is still the expected schema
- Missing fields in merged JSON → check `_resolve_profile()` in `slice_order.py` still walks the chain correctly

If BambuStudio's CLI is ever fixed (it currently segfaults on X1C slicing), you could swap `SLICER_CLI` in `slice_order.py` to point at it — BambuStudio's bundled profiles live at `/Applications/BambuStudio.app/Contents/Resources/profiles/BBL/`, same layout.

## Exporting a new filament preset

To add a new filament (different color, new brand, etc.):

1. In OrcaSlicer GUI on any Mac, pick the filament, tweak, right-click → Save as new preset.
2. Right-click the preset → Export → save the JSON next to `slice_order.py`.
3. **Open the file in a text editor** and add `"type": "filament",` as the first line after the opening `{`. OrcaSlicer CLI requires this even though the GUI export doesn't write it. Without it, the CLI errors with `unknown config type`.
4. Update `FILAMENT_JSON` in `slice_order.py` to point at the new file (or, for a future web UI, expose a filament dropdown).

## Web UI

FastAPI front-end wrapping `slice_order.py`. Runs on any host that has the Python slicer pipeline installed. Two intake flows:

- **Slice**: upload one or more STLs, enter patron info + per-file colour/qty/scale, backend invokes `slice_order.py slice`.
- **Import**: upload a pre-sliced `.3mf` (e.g. from BambuStudio GUI for AMS/multi-material jobs), backend runs `slice_order.py inspect` and logs the order. No slicing.

Both flows end at a result page with per-plate breakdown, `Download 3MF`, and `Print receipt` (one-click to the TM-T88V).

### Dependencies

```
pip3 install fastapi uvicorn python-multipart jinja2 --user
```

(Plus the `python-escpos`, `pyusb`, and `libusb` already installed for the receipt printer.)

### File layout

```
BambuCLI/
├── app.py                                  FastAPI server
├── templates/
│   ├── base.html
│   ├── index.html                          intake form
│   ├── result.html                         order summary + actions
│   └── receipt_sent.html
├── static/
│   └── style.css                           navy + orange brand
├── slice_order.py                          unchanged, subprocess-invoked
└── printqueue/
    ├── orders.json                         auto-appended ledger
    └── work/                                per-order workdirs + 3MFs
```

### Run locally for testing

```bash
cd /Users/alex/Documents/makerspace/BambuCLI
~/Library/Python/3.9/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000/` on the host, or `http://<LAN-IP>:8000/` from any device on the same network. macOS will prompt for firewall permission the first time.

### Features

- **Accumulating file table** — `+ Add file` appends (does not replace); per-row Qty, Colour, Scale, and ✕ delete.
- **Multi-colour orders** — enter one colour per row; OrcaSlicer's multi-filament path separates by colour across plates automatically.
- **Auto-printable output** — every slice goes through `_make_printable()` and `_strip_to_print_file()` so the X1C SD-card browser accepts it (both single- and multi-plate).
- **CSV ledger** — `GET /ledger.csv` exports `printqueue/orders.json` as a spreadsheet with one row per order.
- **Receipt print button** — one-click POST to `/receipt` triggers `slice_order.py receipt --send`.

### Current v1 limitations

- **Same scale across a single order.** Per-file scale columns exist in the UI but the submit blocks if rows disagree. True per-file scaling needs per-scale-bucket slicing + merging — planned for v2, ~100 more lines in `slice_order.py`.
- **No auth.** Anyone on the LAN can submit. Intentional for a staff-only makerspace network; revisit if hosting changes (see `DEPLOYMENT.md`).
- **No CSRF token.** Single-origin, staff-only; acceptable now but revisit if the UI becomes public.
- **No file-size cap.** STLs can be 50MB+; a malicious upload could exhaust disk. Add `limit_max_request_size` in uvicorn args if this becomes a concern.

## Running as a background service (customer-facing Mac)

This Mac is also the customer-facing kiosk: the Guest profile is up front for patrons, the slicer service lives in the background. The right pattern is a **LaunchDaemon** — a system-level process that macOS manages, runs independently of whichever user is logged in at the console, survives reboots.

### Energy settings (not as restrictive as you'd think)

There are three "sleeps" in macOS, only one breaks the service:

| Sleep type                       | Breaks the service?                                    |
| -------------------------------- | ------------------------------------------------------ |
| **Display sleep** (screen off)   | No. Turn it on — the kiosk looks clean                 |
| **Disk sleep**                   | No. Drives wake on first read                          |
| **Computer sleep** (CPU suspend) | Yes, unless Wake-for-Network is on                     |

In System Settings → Battery → Options (or System Settings → Energy Saver on older macOS):

- **Display sleep after N minutes** — fine, set it to whatever. Customer-facing kiosk defaults (1-2 min) are appropriate.
- **Prevent automatic sleeping on power adapter** — enable.
- **Wake for network access** — enable. Belt-and-suspenders: if the computer ever does sleep, an incoming HTTP request wakes it in ~2 seconds.

### Guest profile for customers

System Settings → Users & Groups → enable the Guest User. Customers see only that session; they can't switch to your admin user without the admin password. Fast User Switching (optional, not required — the LaunchDaemon runs regardless of who's foregrounded) can be enabled in the Control Center settings if you want to access your admin session without logging Guest out.

**Tradeoff worth noting**: when the web service is actively slicing (5-30s of CPU burst per patron order), the Guest session can feel slightly laggy. OrcaSlicer pins one core during a slice. Non-issue for kiosk-style browsing; may matter if Guest is used for heavy graphics/CAD work.

### LaunchDaemon plist (ready to use when the web app exists)

Drop this at `/Library/LaunchDaemons/com.makerspace.bambucli.plist` — replace `ADMIN_USERNAME` with your actual macOS username, and adjust the `WorkingDirectory` if BambuCLI lives elsewhere. The `DYLD_FALLBACK_LIBRARY_PATH` entry lets pyusb find Homebrew's libusb for receipt printing:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.makerspace.bambucli</string>
    <key>UserName</key>
    <string>ADMIN_USERNAME</string>
    <key>WorkingDirectory</key>
    <string>/Users/ADMIN_USERNAME/Documents/makerspace/BambuCLI</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/ADMIN_USERNAME/Library/Python/3.9/bin/uvicorn</string>
        <string>app:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8000</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DYLD_FALLBACK_LIBRARY_PATH</key>
        <string>/opt/homebrew/lib</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/var/log/bambucli.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/bambucli.err</string>
</dict>
</plist>
```

Commands you'll actually use:

```bash
# First-time load (starts the service, also enables it at boot)
sudo launchctl load /Library/LaunchDaemons/com.makerspace.bambucli.plist

# Restart after code changes (pulls in new slice_order.py etc.)
sudo launchctl kickstart -k system/com.makerspace.bambucli

# Stop + disable
sudo launchctl unload /Library/LaunchDaemons/com.makerspace.bambucli.plist

# Tail logs when debugging
tail -f /var/log/bambucli.log /var/log/bambucli.err
```

### Networking

Find the Mac's LAN IP: System Settings → Network → Wi-Fi/Ethernet → Details. Colleagues use `http://<that-ip>:8000` in their browsers. Also try `http://<mac-hostname>.local:8000` — works via mDNS without needing to know the IP, usually.

For stable addressing, either:
- Reserve the Mac's IP in the router's DHCP settings (set-and-forget), or
- Set a static IP on the Mac itself (System Settings → Network → Details → TCP/IP → Configure IPv4 → Manually).

First time a colleague hits the port, macOS Firewall will pop up "Allow incoming connections for Python?" Click Allow.

## Receipt printer (Epson TM-T88V)

Every sliced order can drop a printed receipt on an 80mm thermal printer for the library's records. Current pricing: **$0.05 per gram**, hard-coded in `slice_order.py` as `PRICE_PER_GRAM` — edit that constant if the rate changes.

### Prerequisites

```bash
pip3 install python-escpos --break-system-packages
```

On macOS you may also need `pyusb` + `libusb` for the USB backend:

```bash
pip3 install pyusb --break-system-packages
brew install libusb
```

First time the script accesses the USB printer, macOS may prompt for USB permission — allow it.

### Connecting the TM-T88V

The TM-T88V uses USB vendor ID `0x04B8` (Epson) and product ID `0x0202` by default. Hardcoded in the send function; edit `_send_to_tm_t88v()` in `slice_order.py` if the printer reports different IDs (check with `system_profiler SPUSBDataType` on macOS or the Device Manager on Windows).

### Usage

Render a preview (default, no hardware needed):

```bash
python3 slice_order.py receipt "printqueue/work/<order>.3mf" \
    --customer "Aria Jones" --card "29342503188115" --color "Purple"
```

Print for real once the printer is connected:

```bash
python3 slice_order.py receipt "printqueue/work/<order>.3mf" \
    --customer "Aria Jones" --card "29342503188115" --color "Purple" \
    --send
```

### Receipt format (80mm, 48 cols)

- Header: library + makerspace branding
- Date, order number (`MonD-HHMM`)
- Patron name + library card (grouped 4-4-4-2)
- File list with per-file quantities
- Color, plate count (if multi-plate)
- Total mass, total time
- Price (emphasised, bold on the physical print)
- "Library copy · retain for records" footer

Easy to tweak in `_render_receipt_text()` and `_send_to_tm_t88v()` without touching the slicing code.

## Data handling & security

This pipeline stores patron personal information — full names, 14-digit library card numbers, email bodies, and occasionally email addresses. Under **MFIPPA** (Ontario's Municipal Freedom of Information and Protection of Privacy Act), library card numbers in particular are sensitive: they're the key to a patron's account history. Treat the host Mac — and any backup of `BambuCLI/` — as containing protected municipal data.

### Required before going live

- **Enable FileVault** on the host Mac: System Settings → Privacy & Security → FileVault → On. Full-disk encryption; if the Mac is lost or stolen, patron data is unreadable without the account password. Five minutes to enable, single biggest security win.
- **Loop in the library's privacy officer** before sharing the URL with colleagues. Town of Ajax will have someone responsible for MFIPPA compliance — they'll run you through a Privacy Impact Assessment (PIA) and confirm the retention policy. It's paperwork, not a blocker, and it protects you if something ever goes sideways.

### Code-side measures (bake into the pipeline)

- **Retention sweep.** Auto-delete `printqueue/processed/*.eml` and per-order workdirs (`printqueue/work/<timestamp>-<slug>/`) older than 30 days. Sliced `.3mf` files in `printqueue/work/` can have a shorter lifetime — 7 days after pickup is plenty. Run via a launchd `StartCalendarInterval` job or a cron. Not yet implemented; add alongside the web UI.
- **Mask card numbers in logs.** If log files get added, never write the full 14-digit card. Last 4 digits max: `*** **** **** 5678`.
- **Keep card numbers out of filenames.** Current naming (`Firstname_Mon D_mass_time.3mf`) is safe — first name only, no card. Leave it that way.
- **Access control on the web UI** even on LAN. Don't assume "internal = trusted". At minimum a shared makerspace password; ideally SSO against the library's directory.
- **HTTPS even internally.** Self-signed cert is fine for LAN-only; Tailscale and Cloudflare Tunnel handle TLS automatically if you go that route. Plaintext HTTP leaks card numbers to anyone sniffing the makerspace Wi-Fi.
- **Collect only what's needed.** The library's ILS already has each patron's full name keyed by card. The pipeline only really needs first name (for the label thumbnail) + card number (for the audit trail). Consider dropping last name from the intake form — less exposure, no lost functionality.
- **Thumbnail labels** currently show patron first name on the printer's file browser — a minor visible exposure (anyone walking past the printer sees other patrons' first names on pending jobs). Probably fine, but if the privacy officer flags it, swap to anonymous IDs (`Order #12345`) and keep names in a staff-only admin view.
- **Intake form disclosure**. Every submission page should include a short privacy notice. Ask the privacy officer for exact wording; typical shape:
  > *This form collects your name and library card number for processing your 3D print order. Data is retained for 30 days and used only by makerspace staff. Questions: privacy@ajax.ca.*
- **Incident response plan.** If the Mac is ever compromised, MFIPPA requires notifying the affected patrons and the Ontario IPC (Information and Privacy Commissioner). Document the response steps (who to call, what to capture, how to contain) somewhere *before* you need them.

### Don't

- Email full card numbers back to patrons as order confirmations — truncate or omit.
- Sync `BambuCLI/` to third-party clouds (iCloud, Dropbox, Google Drive) without privacy-officer approval — those are outside the municipal IT boundary.
- Share `printqueue/processed/` with anyone for debugging or audit purposes — it's in-scope patron data.
- Check `BambuCLI/` into a public Git repo without first scrubbing `printqueue/` to ensure no patron data is committed. A `.gitignore` excluding `printqueue/inbox/`, `printqueue/work/`, `printqueue/processed/` is a one-line safeguard.
- Assume Guest-session isolation protects patron data on its own. The LaunchDaemon runs as your admin user and can read anything that user can read — Guest is walled off from those files, but a rogue admin login isn't. Access control at the web layer still matters.

## File locations (cheat sheet)

| What                           | Where                                                                                         |
| ------------------------------ | --------------------------------------------------------------------------------------------- |
| OrcaSlicer CLI                 | `/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer`                                      |
| OrcaSlicer bundled profiles    | `/Applications/OrcaSlicer.app/Contents/Resources/profiles/BBL/`                               |
| Pipeline code                  | `BambuCLI/slice_order.py`                                                                     |
| Process overlay                | `BambuCLI/process_cli.json`                                                                   |
| Filament preset                | `BambuCLI/ELEGOO PLA No Aux Fan @Bambu Lab X1 Carbon 0.4 nozzle.json`                         |
| Email queue (inbox)            | `BambuCLI/printqueue/`                                                                        |
| Per-order scratch + final 3MFs | `BambuCLI/printqueue/work/`                                                                   |
| Archived emails                | `BambuCLI/printqueue/processed/`                                                              |
| Runtime merged profiles        | `BambuCLI/.cache/_merged_process.json`, `BambuCLI/.cache/_merged_machine.json` (regenerated on slice) |
| Web app                        | `BambuCLI/app.py`, `BambuCLI/templates/`, `BambuCLI/static/`                                  |
| Order ledger (JSON)            | `BambuCLI/printqueue/orders.json`                                                              |
| Order ledger (CSV export)      | `GET http://<host>:8000/ledger.csv`                                                            |
| Claude Code skill              | `~/.claude/skills/slice-orders/SKILL.md`                                                      |
