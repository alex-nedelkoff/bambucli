# BambuCLI — Glossary

Companion to `architecture.md`. Two definitions per term:

- **Plain** — what it actually does, no jargon.
- **Tech** — the precise technical definition, for when "plain" is too fuzzy
  to debug from.

Alphabetical by term.

---

### 3MF (3D Manufacturing Format)

- **Plain:** A modern 3D-printing file. It's actually a ZIP holding the 3D
  model, the slicer config, the gcode for each plate, and thumbnails — all
  in one bundle.
- **Tech:** A ZIP container format standardized by the 3MF Consortium. Inside:
  XML-based mesh data (`3D/3dmodel.model`), per-plate gcode + metadata
  (`Metadata/plate_N.gcode`, `plate_N.json`), thumbnails, and slicer-specific
  configs (`model_settings.config`, `slice_info.config`,
  `project_settings.config`). The successor to STL with rich metadata.

### AMS (Automatic Material System)

- **Plain:** Bambu's filament-changer accessory. Holds 4 spools so the
  printer can switch colours automatically mid-print.
- **Tech:** A 4-slot motorized filament loader connecting to Bambu printers
  via a dedicated cable. Firmware-controlled tool changes load and purge
  filament between slots during multi-colour prints. Explicitly out of scope
  for our pipeline — we slice each colour separately and pack into single-
  colour plates.

### Application Firewall (macOS ALF)

- **Plain:** macOS's built-in firewall. Decides which apps can accept
  incoming network connections, regardless of port.
- **Tech:** Application-Layer Firewall — intercepts socket-bind/listen calls
  and consults an allow/block list keyed by signed-binary path. Independent
  of `pf` (the packet-level firewall). Settings persist in
  `/Library/Preferences/com.apple.alf.plist`. Default-on for App Store
  installs, off on most Macs unless an admin enables it.

### bootout (`launchctl bootout`)

- **Plain:** Stop and unregister a service from launchd.
- **Tech:** Modern replacement for the legacy `launchctl unload`. Form:
  `sudo launchctl bootout <domain> <plist-path>`. Cleanly removes the
  service from the named domain — once bootout'd, the service won't restart
  until you `bootstrap` it again or reboot.

### bootstrap (`launchctl bootstrap`)

- **Plain:** Tell launchd to load a service into a specific lifecycle scope
  (system-wide, or per-user).
- **Tech:** Modern replacement for the legacy `launchctl load`. Form:
  `sudo launchctl bootstrap <domain> <plist-path>`. Critical because the
  domain decides when the service runs. `system` = always; `gui/<uid>` =
  only when that user is logged in. Legacy `load` lands in whatever domain
  the calling shell is in, which is usually wrong.

### bucket (our project-specific term)

- **Plain:** A group of STL files going on the same plate together — same
  customer, same colour, same scale.
- **Tech:** A `(scale, colour)` keyed grouping in `app.py:submit`. Each
  bucket gets one OrcaSlicer call; the slicer arranges all members onto
  plates (potentially multiple if they don't fit on one). Multi-bucket
  orders get their per-bucket 3MFs merged via `_merge_3mfs` into one final
  multi-plate output.

### DYLD_FALLBACK_LIBRARY_PATH

- **Plain:** An environment variable telling macOS where else to look for
  dynamic libraries if the default search fails.
- **Tech:** Consumed by macOS's dynamic linker (`dyld`). Colon-separated
  directories searched as a fallback when a library load fails through the
  primary `@rpath`/`@executable_path`/system paths. Critical because Homebrew
  installs to `/opt/homebrew/lib` (Apple Silicon) or `/usr/local/lib` (Intel),
  neither of which is on dyld's default search list. Set in our LaunchDaemon
  plist's `EnvironmentVariables` so `libusb` resolves at daemon-start time.

### Epson TM-T88V

- **Plain:** A common thermal receipt printer model. The one currently
  USB-attached to the host iMac.
- **Tech:** Epson's mid-tier USB/Ethernet thermal POS printer. Vendor ID
  `0x04B8`, product ID `0x0202`. 80 mm paper width (~42 chars at default
  font), 250 mm/s print speed, autocutter, ESC/POS protocol.

### escpos / ESC/POS

- **Plain:** The command language thermal receipt printers understand —
  "make this bold," "centre," "cut paper," etc.
- **Tech:** Epson Standard Code for Point Of Sale. A binary command
  protocol — control sequences embedded inline with text — for thermal
  receipt printers. The Python `python-escpos` library wraps it with a
  Pythonic API; we call methods like `p.set(bold=True)`, `p.text(...)`,
  `p.cut()`. Most receipt printers (Epson, Star, Bixolon) speak it.

### FastAPI

- **Plain:** The Python framework `app.py` is built on. Handles HTTP
  routing, request parsing, response generation.
- **Tech:** ASGI-compliant Python web framework built on Starlette + Pydantic.
  Uses type hints for input validation and auto-generated OpenAPI docs.
  Async-first; runs under an ASGI server like uvicorn. Form/file handling
  comes from Starlette + python-multipart.

### gcode

- **Plain:** The actual instructions sent to a 3D printer: "move here at
  this speed, extrude this much, heat to this temperature."
- **Tech:** Plain-text command stream. Each line is one instruction.
  G-codes are motion (`G0` rapid, `G1` linear with optional E for
  extrusion). M-codes are misc machine commands (`M104` set hotend temp,
  `M106` fan, etc.). The printer's firmware motion planner consumes lines
  sequentially, computing trapezoidal speed profiles based on per-axis
  accel and jerk.

### Homebrew (`brew`)

- **Plain:** The most common third-party package manager for macOS. Apt /
  yum / dnf equivalent.
- **Tech:** Community-maintained package manager. Installs to `/opt/homebrew/`
  on Apple Silicon, `/usr/local/` on Intel. `brew install <pkg>` builds and
  links from a "formula" recipe. `brew services` is a launchctl wrapper for
  managing daemons (we use it for headless Tailscale).

### inheritance chain (slicer profiles)

- **Plain:** Slicer profiles can be based on other profiles, layering small
  tweaks on top of a base. A vendor-specific PLA profile inherits from a
  generic PLA profile, which inherits from a generic filament profile.
- **Tech:** Linked list of profile JSONs traversed via the `inherits` field.
  OrcaSlicer's GUI walks the chain at load time and presents a flattened
  view; **the CLI does not walk it** when profiles are passed via
  `--load-settings`. Unset fields silently fall back to Slic3r defaults.
  `slice_order.py:_resolve_profile` walks the chain manually, deepest-loses-
  merges, writes a self-contained JSON to `.cache/`.

### kickstart (`launchctl kickstart`)

- **Plain:** Restart a launchd service.
- **Tech:** `sudo launchctl kickstart -k <service-target>`. Sends SIGTERM
  to the running instance, then re-launches. The `-k` means "kill if
  running, then start fresh." Most common form for us:
  `sudo launchctl kickstart -k system/com.makerspace.bambucli` after a
  `git pull` on the host.

### launchctl

- **Plain:** The terminal command for talking to launchd.
- **Tech:** CLI utility for interacting with launchd. Subcommands:
  `bootstrap` (load into a domain), `bootout` (unload), `kickstart`
  (restart), `print` (show service state), `enable`/`disable`,
  `list` (legacy listing). The right tool for *almost* every "service
  on macOS" question.

### launchd

- **Plain:** macOS's "background-services manager." The system process that
  decides what runs in the background, when to start it, and what to do if
  it crashes.
- **Tech:** PID 1 init process on macOS. Replaces SysV init / cron /
  inetd / atd / xinetd as a single unified service supervisor. Reads service
  definitions from `/Library/LaunchDaemons/`, `/Library/LaunchAgents/`,
  `~/Library/LaunchAgents/`, etc. Manages start, restart-on-crash,
  scheduled launches, and resource limits.

### LaunchAgent

- **Plain:** A program macOS auto-starts in the background — but only when
  a specific user is logged in. Dies on logout.
- **Tech:** A launchd service registered to a user's GUI session domain
  (`~/Library/LaunchAgents/`), tied to that user's login state and running
  as their UID. Good for per-user background processes (file syncers,
  mailbox watchers). Not appropriate for shared services like ours that
  must survive logout.

### LaunchDaemon

- **Plain:** A program macOS automatically starts at boot and keeps running
  in the background, even when nobody is logged in. This is what runs the
  BambuCLI web app on the host.
- **Tech:** A launchd service registered in the system domain via a plist
  in `/Library/LaunchDaemons/`. Owned by root; runs as a chosen UserName
  specified in the plist. Independent of any GUI login session — survives
  reboot, logout, fast user switching.

### libusb

- **Plain:** A library that lets programs talk to USB devices directly,
  without needing a custom driver.
- **Tech:** Cross-platform C library exposing USB device endpoints to
  userspace. On macOS we install it via Homebrew (`brew install libusb`).
  python-escpos uses it (via pyusb's libusb1 backend) to bulk-write to the
  Epson printer over USB.

### MagicDNS

- **Plain:** A Tailscale feature that lets you reach your devices by short
  hostname (`better-slicing`) instead of remembering its 100.x.x.x IP.
- **Tech:** Tailscale's per-tailnet DNS resolver. Mapping is maintained
  centrally in the coordination service; clients install Tailscale's
  resolver as a system DNS server (or split-DNS), and lookups for
  tailnet-internal names resolve to the right peer's tailnet IP.

### model_id

- **Plain:** A short code that identifies which Bambu printer model a 3MF
  was sliced for. The printer rejects 3MFs sliced for a different model.
- **Tech:** String embedded in `slice_info.config`'s `<plate>` block as
  `printer_model_id`. Known values: `BL-P001` = X1 Carbon, `C11` = P1S.
  The printer firmware checks it on SD-card load and refuses incompatible
  files. We set it via the machine profile.

### multipart/form-data

- **Plain:** The HTTP format used when uploading files alongside other form
  fields (the Content-Type that browsers pick when you submit a form with
  a file input).
- **Tech:** RFC 7578 Content-Type. Body is divided into named parts
  separated by a generated boundary string; each part has its own headers
  (Content-Disposition, Content-Type) and payload. `host_submit.py` builds
  one of these by hand; FastAPI parses it via python-multipart.

### OrcaSlicer

- **Plain:** The slicer we use. A community fork of BambuStudio with a
  working CLI.
- **Tech:** Open-source slicer based on the PrusaSlicer/Slic3r codebase
  family, with extended printer support. Distinguishing feature for us: its
  CLI works, whereas BambuStudio's CLI segfaults on every X1C slice in
  every recent release. We invoke
  `/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer` from
  `slice_order.py`.

### plist (property list)

- **Plain:** An XML config file. macOS uses them everywhere — for app
  preferences, system settings, service definitions.
- **Tech:** A structured XML (or binary) document conforming to Apple's
  PLIST 1.0 DTD. Contains a tree of dictionaries, arrays, strings, numbers,
  booleans, dates, and data blobs. LaunchDaemons and LaunchAgents are
  defined as plists. Tools: `plutil` (validate, convert), `defaults`
  (read/write user prefs), text editors for hand-editing.

### profile (machine / process / filament)

- **Plain:** Three kinds of slicer-settings file. Machine = printer specs;
  Process = print quality settings; Filament = material specs.
- **Tech:** JSON files containing hundreds of keyed parameters. Three roles:
  - **Machine** — build volume, max velocities, max accelerations, retraction
    defaults, printer firmware quirks. Per-printer-model.
  - **Process** — layer height, perimeter count, infill pattern + density,
    support config, speeds, cooling. Per print-quality target.
  - **Filament** — temperature, density, retraction overrides, fan curves,
    flow ratio. Per-material.
  Each profile inherits from a parent via the `inherits` field; see
  *inheritance chain*.

### Slic3r

- **Plain:** The grandparent slicer that OrcaSlicer, BambuStudio, and
  PrusaSlicer all descend from. Released ~2011.
- **Tech:** Open-source slicing engine. PrusaSlicer forked from it and added
  extensive features; BambuStudio forked PrusaSlicer; OrcaSlicer forked
  BambuStudio. Slic3r's built-in defaults (60 mm/s outer wall, 1000 mm/s²
  acceleration) are what *every* downstream slicer falls back to when
  settings are missing — which is why the inheritance chain matters.

### socketfilterfw

- **Plain:** The command-line tool to manage the macOS Application Firewall.
  You use it to allow or block specific apps.
- **Tech:** `/usr/libexec/ApplicationFirewall/socketfilterfw`. CLI for the
  ALF. Common subcommands:
  - `--listapps` — show current allow/block list
  - `--unblockapp <path>` — whitelist a binary
  - `--blockapp <path>` — block a binary
  - `--getglobalstate` / `--setglobalstate` — toggle the firewall on/off
  Used in our pipeline to whitelist the Python.app framework binary so
  uvicorn can accept inbound connections from MacBook clients.

### STL (Stereolithography)

- **Plain:** The most common 3D-model file. A list of triangles describing
  the surface — no colour, no metadata.
- **Tech:** ASCII or binary file format encoding a 3D mesh as a triangle
  soup, with per-triangle normals but no shared vertices, no units, no
  colour, no metadata. Universally supported but informationally minimal
  — anything richer (colour, multi-part assembly, slicer hints) needs 3MF
  or a more modern format.

### system domain (vs user GUI domain)

- **Plain:** Two different "scopes" launchd uses. **System domain** runs
  always, no matter who's logged in. **GUI domain** runs only while a
  specific user is logged in.
- **Tech:** launchd domain types:
  - `system` — root-owned, machine-wide. Survives logout, boot loop,
    everything except shutdown.
  - `gui/<uid>` — per-user GUI session. Dies when that user logs out.
  - `user/<uid>` — per-user session, not GUI-bound.
  - `pid/<pid>`, etc. — niche scopes for sub-processes.
  Where you `bootstrap` a service decides its lifecycle. Our service is
  `bootstrap`'d into `system` so it stays up regardless of GUI login state.

### Tailnet

- **Plain:** Your private Tailscale network — the set of devices that can
  see each other.
- **Tech:** A logically isolated set of nodes within Tailscale's coordination
  service, identified by a tailnet name. ACLs, DNS (MagicDNS), exit nodes,
  subnet routes — all scoped per-tailnet. A user can belong to multiple
  tailnets; admins manage one.

### Tailscale

- **Plain:** A service that creates a private network between your devices
  over the public internet. Like a personal VPN that "just works" — no
  firewall holes, no port forwarding.
- **Tech:** WireGuard-based mesh VPN with a central coordination server for
  peer discovery, key exchange, and ACL enforcement. Each device gets a
  stable IP in the 100.64.0.0/10 range and a hostname. Connections are
  peer-to-peer (with NAT traversal) when possible; relayed via DERP
  servers when not. Our deployment uses the Homebrew CLI (`brew install
  tailscale`) — not the GUI app — so it survives logout on the host.

### USB vendor / product ID

- **Plain:** Two short numbers that uniquely identify a USB device. Like
  the make and model of the device, encoded into a number.
- **Tech:** 16-bit identifiers in the USB device descriptor. Vendor IDs are
  centrally assigned to manufacturers by USB-IF (Epson is `0x04B8`).
  Product IDs are per-device within a vendor's namespace (TM-T88V is
  `0x0202`). Together they let libusb address a specific device on the bus
  without ambiguity. Look up unknown devices via
  `system_profiler SPUSBDataType` on macOS.

### uvicorn

- **Plain:** The web server that actually runs the FastAPI app. FastAPI is
  the framework; uvicorn is the engine.
- **Tech:** Lightning-fast ASGI server based on uvloop (faster event loop)
  and httptools (C HTTP parser). Hosts the FastAPI app, accepts incoming
  HTTP connections, dispatches to async handler functions. Our LaunchDaemon
  plist invokes it as `uvicorn app:app --host 0.0.0.0 --port 8000`.

### VPN (Virtual Private Network)

- **Plain:** A way to make remote computers feel like they're on the same
  local network — even if they're across the country.
- **Tech:** A network architecture that tunnels traffic between endpoints
  through an encrypted overlay, presenting them with private-IP routing
  as if they were on the same LAN. Hub-and-spoke (traditional corporate
  VPN) or mesh (Tailscale, Nebula) architectures both qualify. Tailscale
  is mesh: peers connect directly when they can.

### workdir / work directory

- **Plain:** A scratch folder where the slicer dumps everything related to
  one specific print order — the source email, the STL files, intermediate
  3MFs, debug artifacts.
- **Tech:** `printqueue/work/<timestamp>-<sender-slug>/`. Created per intake
  by `cmd_extract` (CLI flow) or `submit` (web flow). Contains:
  - the source `.eml`'s body (`body.txt`)
  - the extracted STL attachments
  - any per-bucket intermediate 3MFs (deleted after merge in the multi-
    bucket case)
  Final 3MFs are moved up one level to `printqueue/work/` (no subdir) so
  desk staff can find them by date in one folder. Workdirs aren't auto-
  cleaned — they're evidence if something went wrong.

### X1C (X1 Carbon) / P1S

- **Plain:** Two Bambu Lab printer models the pipeline supports. X1C is the
  higher-end one with lidar and vibration calibration; P1S is the lighter
  consumer model.
- **Tech:**
  - **X1 Carbon** — enclosed CoreXY, lidar + vibration + flow calibration,
    model_id `BL-P001`, max accel 20000 mm/s², up to 500 mm/s top speed.
  - **P1S** — enclosed CoreXY, lighter calibration suite, model_id `C11`,
    lower max accels and top speeds. Profile lives at
    `Bambu Lab P1S 0.4 nozzle - Copy.json`.
  Both share the same 256×256×250 mm build volume.

### ZIP archive

- **Plain:** A compressed bundle of files. Same format as `.zip` files
  everyone knows.
- **Tech:** PKZIP-format container. Used everywhere as transport for
  structured documents — DOCX, JAR, ODT, **3MF**. Python's stdlib
  `zipfile` module is what we use to read/write 3MFs in `slice_order.py`.

---

## Terms NOT in this glossary (because they're standard developer knowledge)

JSON, regex, subprocess, HTTP method, environment variable, symlink, SSH,
git, FastAPI route, ASGI, Pydantic, async/await, multipart parsing — if
you're already comfortable in a Python web stack, none of these need a
glossary entry. If you want them defined for someone less technical (the
manager meeting?), pull a few into a one-pager separately.
