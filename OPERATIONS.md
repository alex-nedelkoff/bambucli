# OPERATIONS — McLean's Makerspace 3D Print Intake

Operator/maintainer runbook for the print-intake web app running on the
makerspace kiosk PC. This is the practical "how do I keep it running / fix it"
doc. For code architecture see [docs/architecture.md](docs/architecture.md).

> **Platform note:** the production deployment is **Windows 11**. This runbook is
> the Windows-accurate source of truth for running and fixing the app. (Some
> internal design notes in `docs/architecture.md` predate the Windows move and
> still show macOS paths — treat this file as authoritative for operations.)
>
> **Branch note:** this is the `main` (dev trunk) layout, where the deploy
> scripts live at the repo root. The production machine runs the reorganized
> `mcleans` snapshot, where the same scripts live under `deploy\` — adjust the
> paths below accordingly if you're on that branch.

---

## 1. What it is, in one paragraph

A FastAPI + Jinja2 web app that takes a 3D-print order (upload an STL or a patron
`.eml`, pick colour/quantity), slices it with **OrcaSlicer's CLI**, prints a
thermal receipt, and sends the job to one of three Bambu Lab printers. A
background observer watches the printers over MQTT and logs every job. Staff use
it in a browser; patrons never touch it directly.

- **Language/stack:** Python 3.12, FastAPI, uvicorn, Jinja2, paho-mqtt, Pillow.
- **Entry point:** `app.py` (web) + `printer_dashboard.py` (dashboard/MQTT) +
  `slice_order.py` (slicer/receipt CLI, run as a subprocess).

---

## 2. The machine

| | |
|---|---|
| Hostname | `guten-slice` (reachable on the LAN as `guten-slice.local`) |
| OS | Windows 11 Pro |
| App directory | `C:\Users\alex\bambucli` |
| Python venv | `C:\Users\alex\bambucli\.venv` (Python 3.12) |
| Web URL (local) | http://localhost:8000 |
| Web URL (LAN) | http://guten-slice.local:8000 |

Firewall: an inbound rule **"BambuCLI (uvicorn 8000)"** (TCP 8000, Private+Domain)
is already in place so other devices on the makerspace LAN can reach it.

---

## 3. How it runs (and how to restart it)

The web app runs as a **Windows Scheduled Task** named **`BambuCLI uvicorn`**,
registered to start **at boot as SYSTEM** (no login required). It launches
`start-uvicorn.cmd`, which activates `.venv` and runs uvicorn on port 8000.

A second SYSTEM service, **`Ollama`** (see §7), provides the local LLM for `.eml`
parsing.

### Restarting the web app — the reliable way

`Stop-ScheduledTask` alone does **not** work: it kills the task's `cmd.exe`
wrapper but the child `python.exe` keeps holding port 8000, so a restart becomes
a no-op. The reliable restart is to **re-run the registration script elevated**,
which force-kills whatever is on :8000 first:

```powershell
# From an ELEVATED PowerShell (Run as Administrator), at the repo root:
powershell -ExecutionPolicy Bypass -File register-task.ps1
```

Confirm the restart took effect:

```powershell
# New listener PID = restart worked
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select OwningProcess
Invoke-WebRequest http://localhost:8000/api/printers -UseBasicParsing | Select -Expand StatusCode  # 200
```

### When you must restart

- After editing `printers.json` (printer config is read **once** at startup — no
  hot reload).
- After changing any `.py` web file (`app.py`, `printer_dashboard.py`, etc.).
- **Not** needed for slicing/colour changes: `slice_order.py` runs as a fresh
  subprocess per slice and re-reads its files every time.

---

## 4. Dependencies

Dependencies are pinned in **`requirements.txt`** (frozen from the production
`.venv`, Python 3.12.10). Rebuild the environment with:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> ⚠️ **`websockets` is required**, not optional. Without it the dashboard's live
> WebSocket (`/ws/printers`) silently never connects ("No supported WebSocket
> library detected") and the dashboard freezes on its page-load snapshot — camera
> frames and temps stop updating. This was the original "camera doesn't update"
> bug. (It's in `requirements.txt`, so a clean install includes it.)

---

## 5. Printers

Configured in **`printers.json`** at the repo root. This file is **gitignored**
(it holds per-printer LAN access codes) — it lives only on the machine, not in
git. There is no add-printer UI; edit the file and restart (§3).

Three printers:

| id | model | IP |
|---|---|---|
| `p1s` | Bambu Lab P1S | 172.18.149.22 |
| `x1c-p2` | Bambu Lab X1C | 172.18.149.33 |
| `x1c-p3` | Bambu Lab X1C | 172.18.149.25 |

Each entry: `{id, label, model, ip, access_code, serial, webcam_enabled}`. They
connect over MQTT 8883 + FTPS 990.

**The master copy of printer credentials** (SN / IP / access code) is in
`C:\Users\alex\Documents\Printer Secrets.txt`. Keep that file with the machine on
handoff — it's how you rebuild `printers.json` if it's ever lost.

### Cameras

Camera path differs by model and is auto-selected:

- **X1C** → RTSPS on port 322. Requires **"LAN Mode Liveview" enabled** on the
  printer's screen (Settings). If an X1C camera is blank, check this first.
- **P1S** → custom TLS protocol on TCP 6000. Requires **LAN-Only Mode** enabled.

The dashboard also refreshes each camera image on an 8-second front-end timer, so
frames advance even when the MQTT feed is quiet.

---

## 6. Receipt printer

ESC/POS thermal printer, enumerates as **"USB Receipt Printer"** (VID `0x0FE6`,
PID `0x811E`).

> ⚠️ It is **not** a normal Windows printer. Its driver was swapped from
> `usbprint` to **WinUSB using Zadig**, so it's reachable only via `pyusb`
> (python-escpos). Don't "fix" it by reinstalling a Windows printer driver — that
> breaks it. If receipts stop printing after a Windows update reverts the driver,
> re-run Zadig and re-select WinUSB for that device.

Test a receipt:

```powershell
.venv\Scripts\python.exe slice_order.py receipt <file.3mf> --customer "Test" --card 11111111111111 --color Red --send
# {"sent": true} = printed
```

---

## 7. Patron `.eml` intake + Ollama

The "from patron `.eml`" intake option (`/intake/email`) extracts STL attachments
and asks a **local Ollama LLM** to structure the patron's prose into
customer/card/colour/quantity. Regex card-extraction is the fallback if Ollama is
unreachable (the UI then shows *"Ollama unreachable — regex fallback only"*).

- **Daemon:** Ollama runs as a Windows **service `Ollama`** (auto-start, SYSTEM),
  listening on `127.0.0.1:11434`. Model: **`llama3.2:1b`**.
- **Install location:** `C:\Users\alex\AppData\Local\Programs\Ollama\ollama.exe`;
  models under `C:\Users\alex\.ollama\models`.
- **Logs:** `C:\ProgramData\Ollama\ollama.{out,err}.log`.

Manage it:

```powershell
Get-Service Ollama                 # status
(Invoke-WebRequest http://127.0.0.1:11434/api/version -UseBasicParsing).Content   # daemon alive?
```

To rebuild the service from scratch (needs `nssm` — `winget install NSSM.NSSM`):

```powershell
# Elevated PowerShell, at the repo root:
powershell -ExecutionPolicy Bypass -File register-ollama-service.ps1
```

> The 1B model is reliable on name/card/colour but weak on quantities — staff
> should glance at the quantity column and adjust in the form. Swapping to
> `llama3.2:3b` (pull it, change `OLLAMA_MODEL` in `email_parser.py`) improves
> this at the cost of a slightly slower parse.

---

## 8. Slicer (OrcaSlicer)

The app slices with **OrcaSlicer 2.3.2** at
`C:\Program Files\OrcaSlicer\orca-slicer.exe`. The machine/process **base presets
come from OrcaSlicer's own install** (`resources\profiles\BBL`), not from this
repo. The only slicer config files this repo supplies are:

- `process_cli.json` — shared makerspace process overlay (no-brim, tree supports,
  15% grid infill, G92 E0).
- `Generic PLA - No Aux Fan @Bambu Lab X1 Carbon 0.4 nozzle.json` — filament base.

> Everyday filament selection / **AMS slot mapping** is done on the printer's
> touchscreen at print time — the slicer declares the AMS topology so the screen
> offers it. This works on the OrcaSlicer path.

### OrcaSlicer vs BambuStudio (the `USE_BAMBUSTUDIO` flag)

`slice_order.py` has `USE_BAMBUSTUDIO = False`. The two slicer CLIs are mutually
exclusive and each wins one feature:

- **OrcaSlicer (current):** AMS slot mapping works; **no** touchscreen
  "skip-objects" data.
- **BambuStudio:** emits skip-objects data; but defaults filament to the
  **external spool**, which **breaks AMS**.

AMS is the everyday workflow, so OrcaSlicer was chosen. The BambuStudio code path
is preserved behind the flag. **Don't flip it** without re-reading the trade-off
in the code comments around `slice_order.py` line 68–95 — and note it needs a
GPU/login session, not the SYSTEM task.

---

## 9. Where the data lives

All runtime/persistent state is **gitignored** (lives only on the machine):

| Path | What |
|---|---|
| `printqueue/orders.json` | Patron-facing order ledger (the source of truth for jobs/grams) |
| `printqueue/jobs.db` | SQLite — every job the MQTT observer has seen |
| `printqueue/feedback.db` | SQLite — patron feedback entries |
| `printqueue/work/` | Per-intake STL workdirs (patron data — MFIPPA-protected) |
| `printers.json` | Printer IPs + access codes |
| `sd_cards.json` | SD-card UUID → friendly-name map |
| `filaments.json` | Staff filament colour inventory (on-hand + custom swatch hexes) |
| `filaments.bak.json` | Auto-backup mirror of the above (recovered on corruption) |
| `snapshots/` | Live camera frames (overwritten each loop) |

> **Patron data is privacy-protected (MFIPPA).** `printqueue/` contains library
> card numbers, names, and email bodies — never commit it, never share it.

Importing historical orders: drop a `orders.csv` (in `/ledger.csv` schema) at the
repo root and run `python import_history.py` once — it merges into
`orders.json` without duplicating existing rows.

---

## 10. Troubleshooting quick reference

| Symptom | Likely cause / fix |
|---|---|
| Dashboard cameras/temps frozen | `websockets` missing from `.venv` (§4), or printer LAN-liveview/LAN-only off (§5). |
| One X1C camera blank | Enable "LAN Mode Liveview" on that printer. |
| `.eml` intake says unavailable / "regex fallback only" | `Ollama` service down — `Get-Service Ollama`; restart it or re-run the service script (§7). |
| Receipts not printing | WinUSB driver reverted — re-run Zadig (§6). |
| Edited `printers.json`, no effect | Restart the web app (§3) — config is read once at startup. |
| Restart "didn't work" (same PID on :8000) | You used `Stop-ScheduledTask`; use `register-task.ps1` elevated instead (§3). |
| Page looks unstyled on a tablet/Mac | Browser cached old CSS — the app cache-busts via `?v=`, so a normal reload fixes it; hard-refresh once if needed. |

---

## 11. Handoff notes & known gaps

- **Repo ownership:** this branch lives under a personal GitHub account. To keep
  receiving updates after the original maintainer leaves, move the repo to a
  makerspace/library GitHub org (or a successor's account) and re-point the
  machine's `origin`. As-is, the machine runs fine from its local copy without
  any git remote.
- **Dependencies** are pinned in `requirements.txt` (frozen from the production
  `.venv`, Python 3.12.10) — see §4 to rebuild the environment.
- Branch layout: `main` is the dev trunk (full history). The production machine
  runs the reorganized `mcleans` snapshot (deploy scripts under `deploy\`, docs
  under `docs/`). Ongoing development happens on `dev`/`main`.
