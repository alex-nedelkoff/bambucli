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

### Printer status monitoring (shipped, follow-ups remain)

- **Status:** initial cut shipped
- **Shipped:** `printer_dashboard.py` + `templates/dashboard.html` +
  `printers.json` config + `/dashboard` kiosk page. Per-printer paho-mqtt
  client over TLS:8883 maintains a persistent connection, deep-merges
  delta reports into a cached state, fans out to the page over a
  WebSocket. Tile shows gcode_state, % + bar, time left, layer,
  nozzle/bed/chamber temps (chamber falls back to `device.ctc.info.temp`),
  AMS slot/filament/colour, plus a 60s-cadence webcam frame pulled via
  RTSPS:322 through bundled ffmpeg (`imageio-ffmpeg`).
- **Critical correction to earlier assumptions:**
  - Cloud mode + Handy keep working — printers do NOT need to be in
    LAN-only mode for MQTT or for the camera. Camera does need
    "LAN Mode Liveview" toggled on (Settings → General on X1C); when it's
    on, `print.ipcam.rtsp_url` flips from `"disable"` to a `rtsps://...`
    URL — that's how to verify without trial-and-error.
  - Camera protocol changed in current firmware: the legacy port-6000
    custom-protocol path is gated behind full LAN-Only Mode and would
    break Handy. Modern firmware exposes RTSPS on port 322 with H264;
    that's what we use. Don't reintroduce the port-6000 path without
    re-checking firmware behavior.
  - `paho-mqtt` + a thin direct client beat both `bambulabs_api` and
    `pybambu` for our case — both libraries just read the same MQTT
    fields, with extra abstractions we didn't want. We do exactly the
    deep-merge + WebSocket fanout we need in ~250 lines.
- **Open follow-ups:**
  - Filename matching for SD-touchscreen-initiated prints — see next entry.
  - "Send to Printer" replacing the SD-card sneakernet — gives us
    `subtask_name` for free + automation; see existing auto-send entry.
  - **P1S camera (LAN-direct) — not currently working.** P1S firmware
    01.10.00.00 (latest as of mid-2026) does not surface a LAN Mode
    Liveview toggle anywhere obvious in Settings — toggle was added in
    01.05.06 per Bambu's release notes but staff couldn't find it on
    01.10. Effects: `print.ipcam.rtsp_url` is absent from MQTT (vs
    `"disable"` when the toggle exists but is off — useful diagnostic),
    so the dashboard's RTSPS path can't connect. Bambu Studio still
    shows the camera because Studio falls back to cloud-relay when
    LAN-direct isn't available — don't take a working Studio preview as
    proof Liveview is on. Options: (a) walk through every settings
    submenu carefully looking for "LAN" or "Liveview" wording variants,
    (b) ask Bambu support for the exact menu path on 01.10, (c) add a
    cloud-relay fallback to our snapshot grabber (needs Bambu cloud
    auth tokens — significant scope, breaks the "all-LAN" model), or
    (d) accept no P1S camera until Bambu re-exposes the toggle. The
    X1Cs (P2, P3) work fine on the same firmware family, so this is
    P1S-firmware-specific, not a dashboard bug.
  - Camera retry / cooldown when LAN Mode Liveview is off — currently
    we hit it every 60s and surface the error; could back off to 5min
    after N failures. Particularly relevant for the P1S given the
    above; right now it noisily errors every minute.
  - **P1-series `/request` subscribe heuristic may break on future
    firmware.** P1S firmware 01.10 kicks any client subscribing to
    `device/<serial>/request` (DISCONNECT rc=Unspecified Error within
    ~50ms, paho reconnects, infinite ping-pong). Workaround in
    `PrinterClient.__init__`: skip the subscription whenever
    `cfg["model"]` starts with "P1". Risks:
    - Bambu loosens the policy on a later P1 firmware → we'd silently
      forgo filename eavesdropping on P1 prints we could have caught.
      Low cost; symptom is "P1 SD prints show 'no filename sent'".
    - Bambu tightens the policy on an X1 firmware update → X1Cs would
      start kick-looping just like P1S did. Symptom: tile shows
      "reconnecting…" indefinitely. Diagnostic: re-run the mimic-service
      probe in `printer_dashboard.py` history (subscribe to `/report`
      vs `/report+/request`, time the disconnect).
    - Override exists per-printer via `"eavesdrop_request": true|false`
      in `printers.json`. Could promote to runtime auto-detect (try
      `/request` subscribe, observe whether we're disconnected within
      1s, persist the verdict) if firmware behavior turns out to flap.
  - Tighter kiosk mode (drop the global header/nav for `/dashboard`,
    add Edge auto-launch on boot) — currently a shared template.
  - Per-printer "current order" overlay once filename matching works
    (cross-reference subtask_name against `orders.json`).

---

### Filename matching for SD-touchscreen prints (idea)

- **Status:** idea, gated by upload-path decision
- **Why:** The dashboard currently can't tell what 3MF is printing when a
  staffer started the job from the printer's touchscreen against an SD
  card. Without that, we can't link a running print back to the patron's
  ledger row in `orders.json`, which is the prereq for "current order"
  overlays, "X minutes left" notifications, and post-print reconciliation.
- **What we observed empirically (60s MQTT capture across 54 reports):**
  - `print.subtask_name`, `subtask_id`, `print_type`, `profile_id`,
    `model_id` — all empty for the duration of an SD-touchscreen print.
  - `print.gcode_file` and `file` — the literal string
    `"/data/Metadata/plate_1.gcode"`, useless (it's the printer's
    internal extracted-gcode path; the original 3MF name is gone).
  - `print.task_id` — `"478"` (printer-internal counter, increments per
    print). Useful as a stable id for binding eavesdropped names to.
  - `print.sdcard` — `True`. Useful as a positive signal "this is an SD
    print, filename will not be transmitted" — could replace the current
    "Local print (no filename sent)" label with something less ambiguous.
  - **Community libraries (`bambulabs_api`, HA Bambu) read the same
    fields**; they don't have any secret sauce. Their results are only
    as good as what the firmware publishes. For SD-touchscreen prints,
    they're equally blind.
- **Already shipped (catches non-SD prints):** the `/request` topic
  eavesdropper in `printer_dashboard.PrinterClient._on_message` latches
  `subtask_name` from any `project_file` command sent to the printer,
  ties it to `task_id`, and clears on a new print. So Send-to-Printer /
  Handy / cloud-initiated prints get filenames automatically.
- **Options for the SD-touchscreen gap, in order of effort:**
  1. **FTPS browse the SD card.** Port 990, same `bblp` + access-code
     auth. List `.3mf` files at print-start, take the most recently
     modified. Heuristic but ~95% accurate for single-user use.
     ~30 lines + `aioftp` or stdlib `ftplib` over TLS. **Brittle:**
     wrong file if a staffer uploaded multiple .3mfs in quick
     succession or started an older one.
  2. **Move the workflow to Send-to-Printer.** Replaces the SD-card
     sneakernet entirely with the FTPS upload + MQTT `project_file`
     command path (the auto-send proposal below already covers this).
     Once shipped, every print has a real `subtask_name` and the
     existing eavesdropper handles matching. **Recommended path** —
     solves filename matching as a side effect of solving the
     UX problem.
- **Smaller polishing items independent of the above:**
  - Use `print.sdcard` to label SD prints distinctly in the UI rather
    than the current "Local print (no filename sent)" — small UX win
    without solving the matching problem.
  - Persist `task_id` → eavesdropped filename as a small JSON sidecar
    so a dashboard restart mid-print doesn't lose the captured name.

---

### Auto-send-and-start prints (blocked by firmware, deferred)

- **Status:** blocked / deferred. Save-to-SD half is shipped (see "Done &
  shipped"); auto-launch half is parked.
- **Why parked:** The MQTT `project_file` command path is gated by two
  firmware checks on current X1C/P1S firmware that we can't satisfy
  LAN-only:
  1. **MQTT command-auth gate (HMS 0500-0500-0001-0007).** Resolved by
     enabling Developer Mode on the printer — operationally fine and
     already done on all three printers. *Not* the blocker.
  2. **AMS Mapping Table build (HMS 0700-2400-0002-0008, "Failed to get
     AMS Mapping Table").** This is the blocker. Bambu Studio's slice
     embeds a hashed filament_id (`P84c4869`) that lives in Bambu's cloud
     profile DB. The X1C firmware needs the cloud to resolve hash → slot
     `tray_info_idx` (e.g. `GFL99`). Without cloud auth the mapping table
     build fails, the print starts heating, then pauses with the HMS.
  - **Workarounds attempted, both insufficient:**
    - Pre-publish `ams_filament_setting` to set the slot's `tray_info_idx`
      to match the file's hash. Confirmed mechanically (slot tray_info_idx
      did change), but HMS still fires — tray_info_idx-matching alone
      doesn't satisfy the mapping table build.
    - Rewrite the 3MF before upload so `filament_ids` becomes the firmware
      short code (`GFL99` for PLA) across `project_settings.config`,
      `slice_info.config`, and the gcode header. Verified the rewrite is
      correct (no `P84c4869` left in the file), but HMS still fires. So
      *something else* in the firmware's mapping-table validation
      requires state we can't reach from MQTT — possibly a one-time
      "user confirmed slot" flag set via touchscreen, possibly something
      about RFID tray_uuid presence. Couldn't reproduce a working flow
      without cloud auth.
  - Worth retrying when one of these conditions changes:
    - Bambu firmware update loosens / changes the mapping table check.
    - We get a packet capture of BambuStudio's actual working LAN-mode
      send-to-printer payload (different from what we expected; the
      session attempted this but Studio's MQTT connection always kicks
      our sniffer due to single-client-per-credentials).
    - A community library (`bambulabs_api`, `pybambu`, `Bambu-Farm`,
      OrcaSlicer's BambuTunnel) figures it out — periodically re-check
      their `print_project_file` paths.
  - `BambUI` (https://github.com/fidoriel/BambUI) was evaluated and
    *doesn't* solve it — they sidestep entirely by hardcoding
    `use_ams: false`. Don't waste time on it again.
- **Smaller follow-ups worth doing without the auto-launch piece:**
  - Persist `task_id` → eavesdropped filename binding to disk (so a
    dashboard restart mid-print doesn't lose the name).
  - SD-card identifier (see next entry) — would tell staff which physical
    card holds which queue.

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

- `9fa20c4` — Pivot "Send to printer" → "Save to printer SD" (auto-launch
  deferred; see Auto-send entry above). Identify physical SD cards via
  hidden `.bambucli_sd_id.txt` marker file + sd_cards.json name store +
  /sd-cards admin page. Auto-fill the receipt's SD-card field from the
  resolved card name. Bump RECEIPT_WIDTH 28→32 for the generic 80mm
  ESC/POS clone (TM-T88V swap left receipts ~10% under-sized).
- `9aa832f` — P1S `project_file` minimal pybambu payload shape (fixes
  print_error 0x0500C010 on P1S firmware ≥ 01.08.03).
- `86eb2fb` — P1S webcam preview via legacy port-6000 custom protocol
  (X1C still uses RTSPS:322; P1S has no RTSPS exposed on LAN).
- `21de1e1` — FTPS data-channel TLS `close_notify` best-effort
  (fixes X1C `426 Failure reading network stream` on upload).
- `03d706d` — Receipt printer swap to generic 80mm ESC/POS clone.
- `5d83080` — Thumbnail font discovery on Windows/Linux (PIL
  `load_default()` fallback no longer ignores `size`).
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
