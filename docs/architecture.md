# BambuCLI — How it actually works

A reference doc for *you* (Alex) to read end-to-end and feel grounded on every
component. Written so that after reading it you can confidently demo, debug,
or talk through any layer in a meeting.

Companion doc: `docs/glossary.md` — every term in this document with a
plain + technical definition, alphabetically. When something here uses a
term that doesn't ring a bell, the glossary is the lookup.

This is internal-architecture material, not user-facing docs. If you license
BambuCLI to the library, ship them `SETUP.md` and `DEPLOY_README.md` — keep
this one to yourself.

---

## 1. The 30-second mental model

BambuCLI is three layers stacked on top of each other:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 3 — Skills (Claude Code)                                         │
│  /slice-orders          (local: parse .eml, drive layer 1 directly)     │
│  /slice-orders-bulk     (remote: parse .eml, drive layer 2 over Tailnet)│
└─────────────────────────────────────────────────────────────────────────┘
            │ subprocess                       │ HTTP (Tailscale)
            ▼                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 2 — Web app (app.py + FastAPI)                                   │
│  /submit /receipt /history /history/sync /orders/delete /inspect-3mf    │
│  Owns the ledger (orders.json/csv), staff-facing UI, intake flows       │
└─────────────────────────────────────────────────────────────────────────┘
            │ subprocess + in-process imports
            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — Slicer pipeline (slice_order.py)                             │
│  CLI: extract / slice / inspect / receipt subcommands                   │
│  Wraps OrcaSlicer CLI, does 3MF surgery, drives the receipt printer     │
└─────────────────────────────────────────────────────────────────────────┘
            │ subprocess
            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  /Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer  (CLI mode)     │
└─────────────────────────────────────────────────────────────────────────┘
```

Inputs are emails with STL attachments. Outputs are sliced 3MFs in
`printqueue/work/`, ledger entries in `printqueue/orders.json`, and (when
USB receipt printer is plugged in) physical thermal receipts.

The deployment surface is one Mac (`better-slicing` on the Tailnet) running
the web app as a system-domain LaunchDaemon, with the receipt printer plugged
into its USB. Staff hit it from any machine on the Tailnet.

---

## 2. Layer 1 — The slicer pipeline (`slice_order.py`)

One Python script, ~1700 lines, four CLI subcommands. Everything that touches
slicing or printing flows through here.

### 2.1 Subcommands

| Subcommand | Purpose |
|---|---|
| `extract <eml>` | Unzip an `.eml`, pull STL attachments, write headers + body to a workdir, return JSON |
| `slice --workdir … --customer … --color … --stls … --clones … --plate N --printer x1c\|p1s` | Slice one **bucket** (one printer, one color, all STLs in the bucket together). Returns JSON with output path, plate breakdown, warnings |
| `inspect <3mf-or-dir>` | Open a 3MF (or any 3MF in a directory) and dump per-plate metadata (time, mass, support flag, off-bed flag, objects) as JSON |
| `receipt <3mf> --customer … --card … --color … --send` | Render and (optionally) send a receipt to the USB printer for a given finalized order |

The web app shells out to `extract`, `slice`, `inspect`, and `receipt` via
`subprocess.run(...)`. The skill `/slice-orders` does the same. The skill
`/slice-orders-bulk` doesn't touch this script directly — it talks HTTP.

### 2.2 Profile inheritance — the trick that took the longest to figure out

OrcaSlicer is a fork of BambuStudio with a working CLI. Its profile JSONs use
an `"inherits": "<parent-name>"` field to chain (e.g. an X1C 0.24 Draft profile
inherits from `fdm_process_bbl_0.24`, which inherits from
`fdm_process_bbl_common`, which inherits from `fdm_process_common`).

**The OrcaSlicer GUI walks this chain. The CLI does not.** When you pass a
profile to the CLI via `--load-settings`, every key the leaf doesn't define
silently falls back to **Slic3r defaults** — meaning 60 mm/s outer wall
instead of 200 mm/s, 1000 mm/s² acceleration instead of 20000. The 3MF will
still print, just take ~2× longer at lower quality.

`_resolve_profile()` walks the chain manually, deepest-loses-merge, optionally
applies an overlay (e.g. `process_cli.json` for support tweaks), strips
`inherits`, sets `type`, and writes a self-contained merged JSON to
`.cache/_merged_<kind>.json`. **This must run for every profile kind**:
machine, process, and filament. Strip `inherits` *only*; preserve `setting_id`
and `instantiation: "true"` — the CLI uses `instantiation: true` to recognize
usable printers, and dropping it triggers a "process not compatible with
printer" error.

When the user (you) re-exports a preset from the GUI to update settings,
**the export is missing the top-level `"type"` field** (`"machine"` /
`"process"` / `"filament"`). Without it, OrcaSlicer CLI fails with
`unknown config type ... in load-settings`. The script auto-adds it during
merge, but if you ever pre-process by hand, remember.

### 2.3 The slice flow per call

Per `cmd_slice` invocation:

1. **Resolve profiles** — three calls to `_resolve_profile`, one each for
   machine, process, filament. Output to `.cache/`.
2. **Pick the printer** — `args.printer` ("x1c" or "p1s") indexes into the
   `PRINTERS` table at the top of `slice_order.py`. Each entry has the
   model's machine-profile path, model_id (`BL-P001` for X1C, `C11` for P1S),
   and bed type. The bed type matters — P1S textured plate needs different
   adhesion settings than X1C cool plate.
3. **Invoke the OrcaSlicer CLI**:
   ```
   OrcaSlicer --slice 0 --arrange 1 --orient 1 \
     --load-settings <merged_machine.json>;<merged_process.json> \
     --load-filaments <merged_filament.json> \
     --clone-objects "4,2"  \      # one count per input STL
     --outputdir <workdir>  \
     <stl_paths...>
   ```
   `--arrange 1` lets OrcaSlicer auto-pack onto the bed and spill to additional
   plates if too many objects. `--orient 1` enables auto-orient. `--slice 0`
   means "slice all plates" (counterintuitive: 0 = all, 1 = plate 1 only).
4. **Wait, capture stdout/stderr.** The CLI prints arrange decisions and
   timing to stdout; warnings go to stderr.
5. **Inspect the output 3MF** with `inspect_3mf` (see §2.5) to get per-plate
   time, mass, off-bed flags, support detection.
6. **Render label thumbnails** via `_make_printable` so the X1C SD-card
   browser shows a colour swatch + customer name + plate N-of-M instead of
   the default OrcaSlicer thumbnail.
7. **Strip the 3MF** via `_strip_to_print_file` — required for X1C firmware
   to accept multi-plate 3MFs (see §2.7).
8. **Rename and move** to `printqueue/work/<First>_<Mon D>_<mass>g_<HhMM>m.3mf`.
9. **Emit JSON** to stdout summarizing the result.

### 2.4 3MF anatomy — what's actually in a 3MF

A 3MF is a ZIP. The members the pipeline cares about:

```
[Content_Types].xml              boilerplate
_rels/.rels                      pkg-level relationships (boilerplate)
3D/3dmodel.model                 the 3D scene: <resources>+<build>, with parent
                                 object IDs that other files reference
3D/Objects/*.model               per-STL mesh bodies (one file per source STL)
3D/_rels/3dmodel.model.rels      list of which Objects/*.model files exist
Metadata/project_settings.config the slicer config snapshot (printer, filament,
                                 process — used by GUIs to re-open the file)
Metadata/model_settings.config   per-plate + per-object slicer state. Contains
                                 root <object id="..."> blocks (one per source
                                 STL) AND <plate>...</plate> blocks (one per
                                 sliced plate). Plates reference objects via
                                 <metadata key="object_id" value="..."/>
Metadata/slice_info.config       per-plate slicing results (time, weight,
                                 outside_bed flag, support_used flag, objects
                                 on each plate, model_id)
Metadata/plate_N.gcode           the actual gcode for plate N — what the
                                 printer executes
Metadata/plate_N.gcode.md5       checksum of the gcode (printer firmware
                                 verifies this)
Metadata/plate_N.json            machine-readable plate metadata mirror of
                                 slice_info.config
Metadata/plate_N.png             plate thumbnail shown in the printer's UI
Metadata/plate_N_small.png       smaller version
Metadata/plate_no_light_N.png    same image without lighting
Metadata/top_N.png               top-down preview
Metadata/pick_N.png              "pick" preview (for AMS color picking, mostly)
```

`inspect_3mf` reads `slice_info.config` for the authoritative per-plate stats
(OrcaSlicer leaves the `weight` field blank, so we derive grams from
`used_m × 2.98 g/m` for PLA at 1.75 mm — see §9). The `outside_bed` flag in
slice_info is the only reliable off-bed signal — `--arrange 1` will silently
place objects partially off the bed if they don't fit, so you can't catch it
pre-slice.

### 2.5 `inspect_3mf` — read-only dive into the 3MF

Returns a dict shaped like:

```json
{
  "plates": [
    {
      "plate": 1,
      "prediction_seconds": 11640,
      "weight_grams": 62.1,
      "outside": false,
      "support_used": true,
      "color_name": "Red",
      "objects": [{"file": "dragon_v2.stl", "count": 4}]
    }
  ],
  "any_outside_bed": false,
  "any_over_5h": false
}
```

Used by both the web app (`/submit`, `/inspect-3mf`, `/history/sync`) and
the receipt renderer to know what's on each plate.

### 2.6 The two merge functions — these are the trickiest pieces

**`_merge_3mfs(sources, out_path)`** combines N already-sliced 3MFs into one
multi-plate 3MF. Used by the per-file-scale flow in app.py when a single order
has STLs at different scales (each scale becomes its own slicer call, so you
get N intermediate 3MFs and need to merge them).

The challenge: every source's `model_settings.config` and
`Metadata/plate_N.json` were written with their own object IDs and plate
numbering. If you naively concatenate, IDs collide and plate numbers overlap.

What the function does:
1. Walks each source, reads its `Metadata/plate_*.json` to discover **how many
   plates that source has** (it can be multi-plate — that was the recent fix).
2. Computes an `offset` per source = max object ID seen in previous sources,
   so source 2's IDs start above source 1's.
3. Reads each source's `<plate>` blocks from `model_settings.config` (keyed by
   `plater_id`) and from `slice_info.config` (keyed by `index`).
4. Builds a flat `per_plate` list: one entry per output plate, in source order
   then source-plate order.
5. In the write pass, iterates `per_plate` with `enumerate(start=1)` and
   renumbers `plater_id`, `<plate>` index, and per-plate file paths
   (`Metadata/plate_<source_pn>.gcode` → `Metadata/plate_<new_idx>.gcode`)
   on the fly.
6. Writes one merged 3MF with `<resources>` + `<build>` unioned across sources,
   one `<object>` block per source's STL set, and one `<plate>` block per
   output plate.

The most error-prone part is the file-path rewrite regex inside plate blocks
— there are six metadata keys that reference plate-numbered files
(`gcode_file`, `thumbnail_file`, `thumbnail_no_light_file`, `top_file`,
`pick_file`, `pattern_bbox_file`) and each has the form
`Metadata/<prefix><sep><source_pn>.<ext>`. The regex captures the prefix and
suffix, swaps just the number.

**`_strip_to_print_file(path)`** converts a slicer-output 3MF into the
"print file" shape that BambuStudio's "Send to Printer" produces. The X1C
firmware's SD-card browser **rejects multi-plate 3MFs that carry mesh data**
— it expects:
- empty `3D/3dmodel.model` (no objects in `<resources>` or `<build>`)
- no `3D/Objects/*.model`
- `model_settings.config` with `<plate>` blocks but no root `<object>` blocks
- gcode + thumbnails + manifest only

The function does that surgery: empties 3dmodel.model, drops Objects/, strips
root `<object>` blocks from model_settings.config, leaves plate blocks intact.
Result: ~80% smaller file, accepted by the printer. Run unconditionally on
every slice output (single-plate files don't strictly need it, but it's
harmless and saves space).

### 2.7 `_make_printable` — labelled thumbnails

The X1C SD browser shows a thumbnail per plate. Default OrcaSlicer thumbnails
are useless to staff (greyscale plate top-down, no labels). `_make_printable`
overwrites each plate's thumbnail with a Pillow-rendered PNG showing:

- a coloured square (the filament colour swatch — derived from the colour
  name via a small built-in name→hex table)
- customer first name in big text
- plate label like "Plate 2 / 5"
- date, total time, total mass

So when staff pulls the SD card, they can pick the right plate visually
without scrolling through file names.

This is also why the receipt and the 3MF stay consistent — same colour
mapping, same labelling convention.

---

## 3. Layer 2 — The web app (`app.py`)

FastAPI on port 8000. Templates in `templates/`, CSS in `static/`. Backed by
two storage files:

- `printqueue/orders.json` — canonical ledger, list-of-records JSON, written
  whole-file via `_write_ledger`
- `printqueue/orders.csv` — derived view, regenerated on the fly by
  `/ledger.csv`

### 3.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Intake form (templates/index.html) |
| POST | `/submit` | The big one — slice flow OR import flow |
| GET | `/history` | Order history page (templates/history.html) |
| POST | `/history/sync` | Backfill ledger from any 3MFs in `printqueue/work/` not already tracked |
| POST | `/orders/delete` | Remove a row from the ledger; optionally delete the 3MF too |
| POST | `/inspect-3mf` | Upload a 3MF, get plate-by-plate JSON back (used by import-flow JS to show plate count + colours before submit) |
| POST | `/receipt` | Print the thermal receipt for a given finalized order |
| GET | `/download/{name}` | Stream a 3MF file for AirDrop / SD copy |
| GET | `/ledger.csv` | Render the ledger as CSV for spreadsheet tools |

### 3.2 `/submit` — the heart of the app

Two intake flows multiplexed by the `intake` form field:

**`intake=slice`** — patron uploads STLs:
1. Save uploads to a workdir under `printqueue/work/<timestamp>-<slug>-web/`.
2. Parse `colors`, `quantity`, `scale` form fields — each is comma-separated;
   single value broadcasts to all files; mismatched lengths → 400.
3. **Bucket by `(scale, colour)`** — same scale + same colour pack together.
4. If one bucket: one slicer call, single output 3MF, ledger entry, done.
5. If multiple buckets: slice each bucket separately (the per-file-scale path)
   → list of intermediate 3MFs → `_merge_3mfs` to combine → strip → final
   ledger entry. Bucket outputs are deleted after merge to keep `work/` tidy.

**`intake=import`** — patron uploads pre-sliced 3MFs (e.g. they used Bambu
Studio at home, want it logged in the ledger and printed):
1. Save uploads.
2. For each file: inspect, rename to convention, write ledger row.
3. `import_metadata` form field carries per-file colour info (since 3MFs
   don't always self-identify colours), so the receipt has accurate colour
   names.

**Response format:** by default returns `templates/result.html` (the
post-slice summary page). Pass `format=json` to get the orders payload as
JSON instead — used by `/slice-orders-bulk`.

### 3.3 `/receipt`

Form fields: `output_3mf` (path on host), `customer`, `card`, `colors`. Calls
`_send_receipt` → which shells out to `slice_order.py receipt --send`. Returns
JSON with success flag + price. If the USB printer is unplugged, the
subprocess fails with a clean traceback returned as HTTP 500.

### 3.4 The ledger record shape

Built by `_build_record`:

```json
{
  "timestamp": "2026-04-28T22:18:42",
  "flow": "slice",                      // or "import"
  "printer": "x1c",
  "customer": "John Smith",
  "card": "21221012345678",
  "colors": ["Red", "Red"],             // one per plate, used by receipt
  "output_3mf": "/Users/.../printqueue/work/John_Apr 28_62.1g_3h14m.3mf",
  "filename": "John_Apr 28_62.1g_3h14m.3mf",
  "plate_count": 2,
  "total_grams": 62.1,
  "total_time_seconds": 11640,
  "total_time_label": "3h14m",
  "price_cad": 3.11,
  "any_over_5h": false,
  "any_outside_bed": false,
  "plates": [ {plate, time_seconds, time_label, filament_g, over_5h,
              outside, support_used, objects}, ... ]
}
```

Filenames follow `<First>_<Mon D>_<grams>g_<H>h<MM>m.3mf` so staff can sort
the work directory by date / size / time. `_parse_convention_filename`
reverse-parses this for the history-sync backfill case.

### 3.5 The web UI templates

- `index.html` — the intake form. Two tabs (slice / import), file picker,
  per-file colour pickers, scale and quantity, printer toggle. Uses
  `/inspect-3mf` AJAX in the import path to show plate counts before submit.
- `result.html` — post-submit summary. Per-order block with plate breakdown,
  warnings (over-5h, off-bed), download buttons, a "print receipt" button
  per order (which POSTs to `/receipt`).
- `history.html` — paginated table of past orders. Click a row to expand the
  per-plate detail. Has a delete button per row and a "sync from disk" button
  that hits `/history/sync`.
- `base.html` — layout/header/styles.
- `static/style.css` — single CSS file, hand-written, no framework.

---

## 4. Receipt printing

### 4.1 Hardware

Epson TM-T88V, USB-attached to the host iMac. Vendor ID `0x04B8`, product ID
`0x0202`. `system_profiler SPUSBDataType` confirms IDs if a future model is
swapped in.

### 4.2 Software stack

`python-escpos` + `pyusb` + libusb (Homebrew). The escpos library speaks the
TM-T88V's command set so bold / double-height / centre / cut all happen in
hardware — much cleaner than dumping plain ASCII. Receipts are 42 chars wide
(set by `RECEIPT_WIDTH` constant).

The libusb backend on macOS doesn't find Homebrew's `libusb` automatically.
`_send_to_tm_t88v` prepends `/opt/homebrew/lib` to `DYLD_FALLBACK_LIBRARY_PATH`
at the start of the function so the import succeeds regardless of how the
script was invoked. The LaunchDaemon plist sets the same env var so daemon-
run subprocesses see it too.

### 4.3 Receipt layout

```
==========================================
Makerspace @ McLean Branch · 3D Print
==========================================

              JOHN SMITH

  Card:  2122·1012·3456·78
  Date:  Apr 28, 2026  2:18 PM
------------------------------------------
Plate  Colour   Grams  Hr  Min  Completed
    1  Red       62.1   3   14    [ ]
       dragon_v2.stl x 4
       cube.stl x 2
------------------------------------------
  Total:      $3.11
  Total mass: 62.1 g
  Total time: 3h14m

==========================================
  Order taken by:     Alex
  Charged in Koha                [ ]
  Order completed by: _________________
  SD card:            _________________
  Printer:            1    2    3

  Customer contacted for pickup  [ ]
  Order picked up                [ ]
==========================================
```

Single-plate orders show the file list above the plate row; multi-plate
orders inline the file list under each plate row. The checkboxes are workflow
markers staff fill in physically after pickup.

---

## 5. Deployment

### 5.1 LaunchDaemon (system domain)

Installed by `install_service.sh`. The plist lives at
`/Library/LaunchDaemons/com.makerspace.bambucli.plist`. Two non-obvious bits:

**Bootstrap into the system domain, not user GUI.** macOS's legacy
`launchctl load` puts services in whichever domain the calling shell is in,
which is usually the user GUI domain — meaning **the daemon dies on logout**.
We use `sudo launchctl bootstrap system <plist>` instead, which always lands
it in the long-running system domain. That's why the host stays up after you
log out.

**Log file ownership is a trap.** `/var/log/` is root-only by default. If
you specify `StandardOutPath` / `StandardErrorPath` in the plist without
pre-creating the log files with the daemon-user as owner, launchd can't open
the redirect paths and the daemon exits with **config error 78** before
Python ever runs. The install script `sudo touch` + `sudo chown $USER`s the
log paths to head this off.

**Restart command on the host:**
```
sudo launchctl kickstart -k system/com.makerspace.bambucli
```

### 5.2 macOS Application Firewall

The firewall, if enabled, blocks the Python framework binary from accepting
inbound connections. `socketfilterfw --listapps` will show
`/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/...
/Resources/Python.app` as blocked. Unblock with:
```
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp <path>
```
This is a one-time fix; the install script doesn't currently do it because
the firewall isn't on by default on every Mac. Symptom of forgetting:
`curl: (56) Recv failure: Connection reset by peer` from any other machine.

### 5.3 Tailscale

Two ways to run Tailscale on macOS:

- **Tailscale.app (GUI from App Store)** — convenient but **dies on logout**
  because it runs as a user agent. No good for a host that should stay
  reachable when nobody's logged in.
- **Homebrew CLI daemon** — `brew install tailscale` + `sudo brew services
  start tailscale`. Runs as root, survives logout. This is what the host
  uses.

After install, run `sudo tailscale up --operator=$(whoami)` so future
`tailscale` commands don't need sudo.

If you set up the GUI version first, the `tailscale` binary lives inside the
.app bundle (`/Applications/Tailscale.app/Contents/MacOS/Tailscale`), not on
PATH. Symlink it: `sudo ln -s … /usr/local/bin/tailscale`. Or just install
the Homebrew version, which puts it on PATH.

The host's Tailnet name is `better-slicing`. The web UI URL from any other
Tailnet member: `http://better-slicing:8000`.

### 5.4 What lives where on the host

```
/Users/<user>/Documents/makerspace/BambuCLI/   # repo, daemon WorkingDirectory
  app.py                                       # uvicorn entrypoint
  slice_order.py                               # subprocess target
  printqueue/work/                             # output 3MFs + per-order workdirs
  printqueue/orders.json                       # ledger (PII — gitignored)
  printqueue/processed/                        # archived .eml files (gitignored)
  .cache/_merged_*.json                        # resolved profiles (gitignored)
  templates/, static/                          # web UI assets

/Library/LaunchDaemons/com.makerspace.bambucli.plist
/var/log/bambucli.log
/var/log/bambucli.err
```

Patron PII never leaves the host. The ledger is gitignored at the repo level;
every directory containing patron data (`printqueue/`, `.cache/`) is in
`.gitignore`. If you ever need to share the repo, none of that goes with it.

---

## 6. Layer 3 — The Claude Code skills

### 6.1 `/slice-orders` — local pipeline

Lives at `~/.claude/skills/slice-orders/SKILL.md`. Single SKILL.md file, no
helper scripts.

**What it does:** scans `BambuCLI/printqueue/` for `.eml` files. For each:
1. Calls `slice_order.py extract <eml>` to unpack STLs and get the body.
2. Claude (the model) reads the body and parses customer / card / per-STL
   colour and quantity. Has explicit ambiguity protocol: stop and ask if
   anything is unclear.
3. Buckets by colour, calls `slice_order.py slice` per bucket.
4. Retries on `over_5h` / `outside_bed` by reducing clones; caps at 3
   retries per bucket.
5. Archives the `.eml` to `printqueue/processed/`.
6. Reports a summary.

This skill is the simplest path: parse + slice locally, no ledger entry, no
receipt, no host involvement. Useful for testing or for non-customer-facing
slicing (your own personal prints).

### 6.2 `/slice-orders-bulk` — host-driven

Lives at `~/.claude/skills/slice-orders-bulk/`. SKILL.md plus three Python
helpers:
- `host_check.py` — reachability ping
- `host_submit.py` — multipart POST to host's `/submit?format=json`
- `host_receipt.py` — form POST to host's `/receipt`

Same parsing protocol as `/slice-orders`, but instead of slicing locally,
bundles each email's STLs into a multipart upload and POSTs to the host. The
host runs OrcaSlicer, writes to its own `printqueue/work/`, appends to
**its** ledger, returns the order records as JSON. The skill then chains a
`/receipt` call per order to print the physical receipt over the host's USB.

Configurable host URL via `BAMBUCLI_HOST` env var or
`~/.bambucli-bulk-host` file (currently set to `http://better-slicing:8000`).

This is the production bulk path: one command from the MacBook produces
sliced 3MFs, ledger entries, **and** physical receipts on the host's
thermal printer — without ever touching the host directly.

---

## 7. End-to-end walkthrough — one email, bulk path

A patron emails `john.smith@example.com → makerspace@ajax.ca`:
*Hi! Could you print 4 of dragon_v2.stl and 2 of cube.stl in red? My card is
2122-1012-3456-78. Thanks, John.*

Attachments: `dragon_v2.stl`, `cube.stl`. Email is dropped into
`/Users/alex/Documents/makerspace/BambuCLI/printqueue/john.eml` on the
MacBook.

User on MacBook runs `/slice-orders-bulk` in Claude Code:

1. Skill reads `~/.bambucli-bulk-host` → `http://better-slicing:8000`.
2. Skill runs `host_check.py` — verifies host reachable. ✓
3. For `john.eml`:
   - Calls `slice_order.py extract john.eml` locally. STLs land in
     `printqueue/work/20260428-220000-johnsmith/`.
   - Claude reads the body, parses: customer = "John Smith", card =
     "21221012345678", colours = `[Red, Red]`, quantities = `[4, 2]`.
   - One colour bucket (everything is Red).
   - Skill calls `host_submit.py` with all four args + the two STL paths.
4. `host_submit.py` builds a multipart POST to
   `http://better-slicing:8000/submit?format=json`. Body has `intake=slice`,
   customer, card, colours CSV, quantity CSV, both STL files, `format=json`.
5. **On the host:**
   - FastAPI saves the STLs to its own
     `printqueue/work/20260428-220015-john-web/`.
   - Buckets the files: one bucket `(scale=1.0, colour=Red)` with both STLs.
   - One bucket → single slicer call. Resolves profiles (X1C machine + Draft
     process + Generic PLA filament), invokes OrcaSlicer with
     `--clone-objects "4,2" --arrange 1 --orient 1 --slice 0`.
   - OrcaSlicer arranges: 4 dragons + 2 cubes don't fit on one bed → it
     spills to plate 2. Slices both plates. Writes the 3MF.
   - `_make_printable` rewrites both plate thumbnails with John-Red labels.
   - `_strip_to_print_file` strips it to print-file shape.
   - File renamed to `John_Apr 28_62.1g_3h14m.3mf` and moved to
     `printqueue/work/`.
   - `_build_record` builds the ledger entry with both plate stats.
   - `_append_to_ledger` writes to `orders.json`.
   - `/submit` returns JSON with the orders array.
6. **Back on the MacBook:** skill reads the JSON. One order, two plates,
   under 5h each, on-bed. ✓
7. Skill calls `host_receipt.py` with `output_3mf`, customer, card, colours.
8. Host's `/receipt` endpoint runs `slice_order.py receipt --send` on the
   already-finalized 3MF. The script:
   - Re-inspects the 3MF for plate breakdown.
   - Opens USB to the TM-T88V (vendor `0x04B8`, product `0x0202`).
   - Renders the labelled receipt and cuts.
9. Skill archives `john.eml` → `printqueue/processed/20260428-220000-john.eml`
   on the MacBook.
10. Skill reports: "John Smith (2122·1012·3456·78) — Red — 2 plates, 3h14m,
    62.1g, $3.11. Receipt printed."

Total wall time: ~30 seconds on the MacBook side; the slice itself takes
~10 seconds for two small plates. The desk staff sees: a new ledger row, a
fresh thermal receipt sitting in the printer tray, and a 3MF ready to copy
to an SD card.

---

## 8. File inventory — what's in the repo

| Path | What it is |
|---|---|
| `slice_order.py` | Layer 1, the slicer pipeline |
| `app.py` | Layer 2, the FastAPI web app |
| `templates/`, `static/` | Web UI |
| `Bambu Lab X1 Carbon 0.4 nozzle.json` | OrcaSlicer-exported X1C machine profile |
| `Bambu Lab P1S 0.4 nozzle - Copy.json` | P1S machine profile |
| `process.json`, `process_cli.json` | Process profile + small CLI overlay (auto-supports + G92 reset) |
| `filament.json` | Default filament fallback (rarely used; per-printer files take precedence) |
| `Generic PLA - No Aux Fan @Bambu Lab X1 Carbon 0.4 nozzle.json` | PLA filament profile (X1C-tuned). Aux fan stays OFF because the chamber's aux fan is asymmetrically placed (left side only) and causes uneven curling on large flat prints. **`filament_max_volumetric_speed = 21 mm³/s`** matches Bambu PLA Basic — keeping it lower (e.g. 12, the no-aux-fan reference value) actively *triggers* the X1C's mid-print throttle to silent because the firmware leaves no margin around the ceiling. See lore note in §9. |
| `machine.json` | Aggregate machine profile (legacy; superseded by per-printer files) |
| `0.20mm Standard …`, `0.24 Draft …` etc. | OrcaSlicer-bundled process presets — the `inherits` chain points to these |
| `install.sh` | Homebrew + Python deps + OrcaSlicer trust-prompt |
| `install_service.sh` | LaunchDaemon (system domain) |
| `install_agent.sh` | LaunchAgent (per-user) — alternative to daemon |
| `slice.sh` | Legacy bash wrapper. Predates `slice_order.py`. Kept for reference; not used. |
| `SETUP.md` | Onboarding doc — slicer-only context, predates the web app |
| `DEPLOYMENT.md` | Full deployment notes — security, retention, multi-user |
| `DEPLOY_README.md` | 5-step quickstart |
| `TAILSCALE.md` | Real-world deployment lessons (firewall, daemon domain, GUI vs CLI Tailscale) |
| `BambuAPI/` | Early-exploration third-party Bambu API clone. Gitignored. Not part of pipeline. |
| `printqueue/work/` | Per-order workdirs + final 3MFs. Gitignored. |
| `printqueue/orders.json`, `orders.csv` | Ledger. Gitignored (PII). |
| `printqueue/processed/` | Archived `.eml`s. Gitignored. |
| `.cache/` | Resolved profile JSONs. Gitignored. |
| `docs/architecture.md` | This document. |
| `docs/BambuCLI_meeting_prep.md` | Meeting prep. Gitignored. |

---

## 9. Lore / gotchas worth remembering

- **OrcaSlicer, not BambuStudio.** BambuStudio's CLI segfaults on every X1C
  slice in its own bundled profiles (missing `machine_limits` for X1C). Don't
  "fix" the pipeline to use BambuStudio. If a future BambuStudio (02.05+)
  fixes its CLI, you can revisit.
- **Strip `inherits`, keep `setting_id` + `instantiation`.** Stripping all
  three breaks the printer↔process compatibility check.
- **GUI-exported profiles miss `"type"`.** Add it before merging or the CLI
  fails opaquely.
- **`--arrange 1` lies about bed fit.** Off-bed signal comes from
  `slice_info.config`'s `outside` flag, post-slice only.
- **No CLI flag for thumbnails on macOS** — needs an OpenGL context. The
  3MF still prints fine, but Bambu Studio shows it as "needs slicing"
  cosmetically. Doesn't matter for the SD-card flow.
- **PLA mass formula:** `used_m × 2.98 g/m`. Holds for 1.75 mm × 1.24 g/cm³
  PLA. If you switch to PETG/TPU/ABS, that constant changes.
- **Future investigation: OrcaSlicer-specific throttle triggers.** The
  Apr 29 debug session looked at why X1C prints drop to silent mid-print
  even with volumetric speed set correctly. Three OrcaSlicer-vs-BambuStudio
  output differences popped on the same model:
  - **`accel_to_decel_enable = 1`** in Orca, `0` in Bambu. Enabled, Orca
    applies a 50% asymmetric multiplier between accel and decel ramps —
    which makes the firmware planner handle motion profiles where
    accelerating is faster than decelerating. Some Bambu users report this
    correlates with motion-monitor false positives.
  - **Part-cooling fan cycling: 297× in Orca vs 2× in Bambu** for the same
    model. Orca's layer-time-based cooling logic (driven by `min_layer_time`
    / `slow_down_for_layer_cooling`) toggles the fan rapidly. Rapid melt-
    zone cooling-rate changes produce small flow variances which the X1C's
    flow-inconsistency monitor may flag.
  - **M204 (acceleration-set) frequency: ~7200 in Orca vs ~3900 in Bambu.**
    Orca oscillates between {2000, 5000, 10000} accel values constantly
    (~5800 transitions); Bambu uses larger blocks of one value at a time.
  We didn't change anything here yet — current setup works well enough at
  21 mm³/s. If throttling reappears as a recurring problem, the surgical
  fix is `accel_to_decel_enable: 0` in `process_cli.json`. Falling back
  options: cap accel-tier oscillation, then disable layer-time cooling.
  Full analysis sat in chat history Apr 29 — re-derive from the
  `debug_apr29/` Jar 3MFs if needed (same approach: M-code histograms +
  `M204 S*` distribution on Orca vs Bambu output of the same model).
- **`filament_max_volumetric_speed` is the throttle trigger — but in the
  *opposite* direction from what looks intuitive.** The slicer caps
  commanded speeds at this value AND embeds it in the 3MF as metadata.
  The X1C firmware reads that metadata and treats it as the *safety
  ceiling* with very little margin around it. Real per-segment flow has
  natural jitter (extruder slip, viscosity changes, layer-change pressure
  spikes) — even when the slicer planned for 11 mm³/s, real flow can
  briefly hit 13. If the ceiling is set tight (e.g. 12), the firmware
  treats those spikes as violations and drops to silent mode (50%) to
  pull flow back under the ceiling. There's no "safety margin" allowance.
  Setting the ceiling **higher** (21, matching Bambu PLA Basic) gives the
  printer headroom and stops the panic-throttle.
  - **We use 21 mm³/s** to match Bambu PLA Basic, even though the profile
    is named "Generic PLA - No Aux Fan." The "no aux fan" part is real —
    on this X1C the aux fan is asymmetrically placed (left side only), so
    it's left off to avoid one-sided cooling and curling on large flat
    prints. We accept the resulting cooling tradeoff (slightly worse
    overhangs) but keep the volumetric ceiling at 21 because **lowering
    it does not help — it actively triggers throttling**.
  - **Don't drop this back to 12 to "be safe."** That's the trap. The
    ceiling has to be set above where real-world flow jitter peaks, not
    at the average. Bambu's "Generic PLA - No Aux Fan" preset uses 12
    because it expects matching aux-fan-off prints to also run at lower
    process speeds; we don't change process speeds, so the ceiling has to
    move up instead.
- **`--clone-objects "4,2"`** takes one count per file, not (index, count)
  pairs. A common mistake when reading the help.
- **`--curr-bed-type` isn't in the help.** Set via `curr_bed_type` in a
  preset JSON instead.
- **The X1C model_id is `BL-P001`. The P1S is `C11`.** These embed in
  `slice_info.config` and the printer firmware checks them. Sending an X1C
  3MF to a P1S printer (or vice versa) makes the SD browser refuse the file.
- **Tailscale GUI dies on logout.** Use Homebrew CLI tailscale on the host.
- **LaunchDaemon must use `bootstrap system`, not legacy `load`.** The latter
  lands in the user GUI domain and dies on logout.
- **`/var/log/` is root-only.** Pre-create log files with daemon-user
  ownership or the daemon errors with config error 78 before Python runs.
- **macOS firewall** silently blocks the Python.app framework binary on a
  fresh Mac. Symptom: `curl: (56) Recv failure: Connection reset by peer`.
  Fix with `socketfilterfw --unblockapp`.
- **Stale local uvicorn on the MacBook competes with the host.** `lsof -i
  :8000` and kill if anything's listening. Has happened more than once.
- **3MF members are case-sensitive in some contexts.** When matching paths
  inside the zip, always use the exact strings: `3D/3dmodel.model`,
  `Metadata/model_settings.config`, etc.
- **Don't touch the workdir layout in `printqueue/work/`.** The skill, the
  web app, and the final filename collision logic all assume
  `printqueue/work/<final-3mf>` lives next to `printqueue/work/<workdir>/`.
  Restructuring this breaks both intake paths.

---

## 10. Where the surface area is bigger than it looks

If you ever quote this work or pitch it to another library:

- The slicer pipeline alone is 1700 lines and represents the bulk of the
  hard problems (profile inheritance, 3MF surgery, merge logic, USB receipts).
- The web app is another 600 lines and pulls everything together with two
  intake flows + ledger + history.
- The deployment story (LaunchDaemon, Tailscale, firewall) is its own
  category of expertise — somebody who can write the slicer code from
  scratch may still spend a week getting it to run reliably on a shared
  iMac with patron-data security boundaries.
- The skills layer is the integration with Claude Code that makes the
  email-parsing piece "free" — most automation pipelines stop at "manual
  data entry from email" because parsing arbitrary patron prose is hard.

That's the value. If a vendor were quoting a similar deliverable from
scratch, they'd be three months in before they had something this
production-ready, and they'd ship without the email-parsing layer.
