# BambuCLI — Future Improvements

Engineering-focused roadmap. One section per proposal. When you act on
something, move it to "Done & shipped" at the bottom (or just delete it once
it's reflected in `architecture.md`).

Conventions per entry:
- **Why** — the problem it solves
- **What** — what gets built
- **Scope** — rough hour estimate + files touched
- **Dependencies / risks** — anything that would block or complicate it
- **Status** — `idea` / `scoped` / `in progress` / `blocked`

Pricing and contractor framing for any of these (if Alex decides to scope a
specific item as paid work) lives in the gitignored sibling docs, not here.

---

## Active proposals

### Email intake — Ollama-driven prefill (in progress)

- **Status:** in progress (initial cut shipped)
- **Why:** Eliminate re-typing of patron name, card #, color, qty. Originally
  only Alex could do this via Claude Code on his MacBook; now it's a
  staff-facing web feature on the host.
- **What:** "From email" intake mode on the index page. Staff drops `.eml`
  → `/intake/email` extracts STLs + asks Ollama to parse → fields pre-fill
  the existing slice form → staff reviews + submits through unchanged
  `/submit?intake=slice`. Detection layer flags file-share links (Drive,
  Dropbox, Thingiverse, etc.) and blocks submit if the patron sent files
  via link instead of attachment.
- **Shipped:** `email_parser.py` + `/intake/email` endpoint + UI tab +
  link-detection safety layer + relaxed card regex.
- **Open follow-ups:**
  - Confidence flagging on parsed fields (HIGH/MEDIUM/LOW from the scope
    doc) — currently we just pre-fill and rely on visual review. The full
    confidence-driven gate hasn't been built.
  - 3MF-attachment branch — if the email carries a pre-sliced 3MF, route
    to import flow instead of slice flow. Spec'd in scope doc, not built.
  - Parse-log persistence (parse_log.json per workdir) — for tuning prompts
    and auditing AI decisions.
  - The internal scope doc (`docs/email_intake_scope.md`, gitignored) has
    the full spec including pricing/phasing.

---

### Printer status monitoring (idea)

- **Status:** idea, not yet scoped in detail
- **Why:** Staff currently has to walk to the makerspace floor to check
  whether a printer is busy, what's printing, how long is left, and whether
  it's errored. From a desk monitor, all of that is invisible. Adding
  real-time status would mean staff can plan the next print, talk to
  patrons about wait times, and see error states without leaving the desk.
- **What:** A new `/printers` page (or panel on existing `/history`) that
  shows live state for each registered printer:
  - Connected / disconnected
  - Idle / printing / paused / error
  - Current job filename + estimated time remaining
  - Hotend + bed temps
  - Layer progress (current / total)
  - AMS state (slot temps, filament present per slot, filament colors)
  - Error/warning messages from the firmware (the same messages we've been
    chasing as "throttle reasons")
- **How it works:**
  - Bambu printers in **LAN-only mode** expose MQTT on port 8883 (TLS) with
    real-time telemetry. Subscribe to `device/<serial>/report` and the
    printer pushes state updates as JSON.
  - Each printer needs: local IP, 8-digit access code (from touchscreen),
    serial number. Stored in a config file (`printers.json`?) or env vars.
  - Backend service runs as a long-lived async task in the FastAPI app,
    maintaining MQTT connections per printer + caching the latest status.
  - Frontend polls `/printers/status` (or uses Server-Sent Events for live
    updates) to render the panel.
- **Scope:**
  - Library evaluation: `bambulabs_api` and `pybambu` exist; pick one or
    write a thin direct-MQTT client. ~2 hrs to evaluate.
  - Printer config storage + admin page to add/edit printers: ~3 hrs.
  - MQTT background task with reconnect logic: ~4 hrs.
  - Status JSON endpoint + polling/SSE: ~2 hrs.
  - Frontend panel + CSS: ~3 hrs.
  - Testing on actual printers: ~2 hrs.
  - **Total: ~16 hours** for a basic but production-ready monitor.
- **Dependencies / risks:**
  - **Each printer must be in LAN-only mode** — set on the touchscreen.
    Default is cloud mode. One-time per printer.
  - **Access codes can rotate** if someone factory-resets the printer or
    changes the LAN setting. We need a clear "reconfigure this printer"
    UX or staff has to edit the config file by hand.
  - **WiFi blips break MQTT.** Reconnect logic is essential or the panel
    will silently freeze.
  - **Not Tailscale-routed by default.** The printer talks to other devices
    on its physical LAN. The host iMac (also on the same LAN) is the right
    place to run this — Pickering printers would need their own Pickering
    host. Don't try to route MQTT over Tailscale.
- **Why this first** (vs auto-send): zero write actions to the printer, so
  the failure modes are limited to "panel shows wrong info" rather than
  "wrong file got printed on wrong printer." Lets us learn the protocol
  + reliability characteristics before trusting it for anything destructive.

---

### Auto-send print files to printer (idea)

- **Status:** idea, depends on monitoring being shipped first
- **Why:** Currently staff copies the sliced 3MF onto a microSD card,
  walks to the printer, inserts the card, navigates the SD browser, and
  starts the print. Every step is error-prone (wrong file, wrong card,
  wrong printer). Auto-send replaces the SD-card shuffle with a click on
  the web UI: "Send to X1C #1". File lands on the printer, ready to start.
- **What:** Add a "Send to printer" button on the order-result page and
  history page. Backend uses FTP-over-TLS (port 990) to upload the 3MF to
  the printer's `/sd/` directory, then optionally fires a print-start
  command via MQTT.
- **Variants:**
  - **Conservative:** upload only, staff still presses Go on the touchscreen.
    Eliminates the SD-card shuffle but keeps human-in-the-loop on the
    actual print decision. **Recommended starting point.**
  - **Aggressive:** upload + auto-start. Faster but no manual confirmation —
    a wrong-printer selection wastes filament and time.
- **Scope:**
  - Builds on the printer config from monitoring (need IP + access code +
    serial already).
  - FTP-over-TLS upload helper: ~3 hrs.
  - "Send to printer" button + per-printer router (which printer takes
    which print): ~3 hrs.
  - Result-page + history-page integration: ~2 hrs.
  - Optional MQTT print-start command (if going the aggressive variant): ~2 hrs.
  - Error handling (full SD card, network blip, printer busy): ~3 hrs.
  - Testing: ~2 hrs.
  - **Total: ~13 hrs conservative, ~15 hrs aggressive.**
- **Dependencies / risks:**
  - Hard-depends on monitoring being shipped first (need printer config
    + connection plumbing).
  - **Wrong-printer routing is the biggest risk.** Multi-printer makerspaces
    need clear UX: "Send to X1C #1, currently idle" vs "Send to P1S, in
    use until 4:23 PM." The history page would need to know which printer
    a print was sent to so staff doesn't double-send.
  - **SD card vs internal storage.** Bambu printers can store files on
    the SD card (~32 GB) or internal flash (~smaller). Upload destination
    matters — confirm before building.
  - **MFIPPA again.** Patron filenames embed customer first name (per the
    existing convention); these become visible on the printer's SD browser.
    Already true today (staff puts the SD in), but worth flagging if the
    auto-send variant changes who can see filenames remotely.

---

### Other ideas (backlog, not scoped)

These are minor or distant. Each gets a one-liner; expand into a real entry
when they become active.

- **Patron-facing self-service portal.** Patrons upload STLs themselves
  via a public web page that's on a separate auth boundary from staff intake.
  Big scope; needs auth, per-patron quotas, abuse mitigation.
- **AMS / multi-color slicing.** Currently explicit out-of-scope. Bambu
  AMS support per-plate color changes. Would need significant rework of
  the bucket / merge logic.
- **Cost recovery report.** Monthly aggregation: total grams printed by
  patron, by branch, by filament colour. CSV export beyond the current
  per-order ledger. Useful for budget conversations with library admin.
- **Filament inventory tracking.** Know how much PLA is left on each spool
  (manual entry + auto-decrement based on print mass). Alerts when low.
- **Better receipt previews on the web UI.** Currently the web result page
  doesn't show what the receipt will look like — staff has to trust the
  printout. A preview panel before the "Print receipt" button would help.
- **Print-failure detection.** Bambu's lidar can detect failed prints. If
  the monitoring layer (above) catches a `print_failed` event, automatically
  flag the order in the ledger and notify the patron via email.
- **Email notifications to patrons.** Auto-send "Your print is ready for
  pickup" emails when staff marks an order completed. Requires SMTP or a
  transactional email service.
- **Pickering deployment runbook.** Once Ajax has a few months of stable
  operation, write a deployment-from-zero doc tuned for a sister branch.
  Builds on `DEPLOY_README.md` + `TAILSCALE.md` but adds branch-specific
  considerations.

---

## Done & shipped

Move entries here when they land in `architecture.md`. Keep one-liners with
the commit hash so it's easy to grep history.

- `25e5668` — Receipt: waiver checkbox + SD card option list (R1/R2/R3/B1).
- `4a7ae2a` — Email intake — initial cut (regex + Ollama prefill, no
  confidence gate yet).
- `fd2d51e` — Email intake — file-share link detection + blocking submit.
- `ce3ecb2` — Email intake — relaxed card regex for arbitrary digit
  groupings.
- `6bafb91` — Filament profile: revert to 21 mm³/s after the throttle
  investigation. Lore note added to architecture.md.
- `efe272e` — Filament profile: rename ELEGOO → Generic PLA - No Aux Fan.
- `772428c` — Multi-plate bucket merging in `_merge_3mfs`.
- `3ff4689` — `format=json` mode on `/submit` for programmatic clients.

---

## How to use this doc going forward

When a new feature comes up in conversation:
1. Add a new entry under "Active proposals" or "Backlog" (one-liner if
   not yet considered seriously).
2. As you work on something, change its status field.
3. When it ships, move the entry's headline + commit hash to "Done &
   shipped" and any relevant lore to `architecture.md`.

For pricing, contractor framing, or stakeholder strategy on a specific
feature: write a sibling scope doc (e.g. `docs/<feature>_scope.md`) and
gitignore it. Don't put that material in this file.
