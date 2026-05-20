#!/usr/bin/env python3
"""BambuCLI order helper. Mechanical primitives for the /slice-orders skill.

Subcommands:
  extract <eml>                 unpack .eml into a work dir, print JSON summary
  slice  --workdir ...          invoke BambuStudio CLI, emit a plate 3MF + parsed metadata
  inspect <3mf>                 unzip a 3MF and report each placed object's absolute XY bounds

All subcommands print a single JSON object to stdout on success. Errors also go
to stdout as JSON with an "error" key and a non-zero exit code.
"""

from __future__ import annotations

import argparse
import email
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from email import policy
from pathlib import Path
from xml.etree import ElementTree as ET

def _fmt_date_dom(d: datetime) -> str:
    """Cross-platform replacement for strftime('%b %-d'), e.g. 'Apr 5'.
    Windows' strftime does not support glibc's %-d 'no leading zero' flag,
    so we compose the day component manually."""
    return f"{d:%b} {d.day}"


def _fmt_receipt_dt(d: datetime) -> str:
    """Cross-platform replacement for strftime('%b %-d, %Y  %-I:%M %p'),
    e.g. 'Apr 5, 2026  3:45 PM'. Same %-d / %-I portability story as above."""
    h = d.hour % 12 or 12
    return f"{d:%b} {d.day}, {d.year}  {h}:{d:%M %p}"


BASE_DIR = Path(__file__).resolve().parent
# Using OrcaSlicer (BambuStudio fork) because BambuStudio 02.04.00.70's CLI
# segfaults on X1C slice operations — even with its own bundled profiles —
# due to a missing cli_config.json machine_limits entry for X1 Carbon.
if sys.platform == "win32":
    SLICER_CLI = Path(r"C:\Program Files\OrcaSlicer\orca-slicer.exe")
    ORCA_BUNDLE = Path(r"C:\Program Files\OrcaSlicer\resources\profiles\BBL")
else:
    SLICER_CLI = Path("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")
    ORCA_BUNDLE = Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles/BBL")
PRINT_QUEUE = BASE_DIR / "printqueue"
WORK_DIR = PRINT_QUEUE / "work"
PROCESS_OVERLAY = BASE_DIR / "process_cli.json"
FILAMENT_JSON = BASE_DIR / "Generic PLA - No Aux Fan @Bambu Lab X1 Carbon 0.4 nozzle.json"

# Per-printer machine + process selection. Each printer has its own start/end
# gcode (X1C uses lidar/scanner commands the P1S can't run; P1S firmware checks
# the model_id in slice_info.config and rejects mismatches), so we load the
# matching bundled OrcaSlicer profile and write the right model_id at post-
# processing time. The process overlay (no_brim + tree supports + 15% grid
# infill + G92 E0) is shared across printers — same makerspace defaults.
PRINTERS: dict[str, dict] = {
    "x1c": {
        "label": "Bambu Lab X1 Carbon",
        "name": "Bambu Lab X1 Carbon 0.4 nozzle",
        "machine_json": ORCA_BUNDLE / "machine" / "Bambu Lab X1 Carbon 0.4 nozzle.json",
        "process_base": ORCA_BUNDLE / "process" / "0.24mm Draft @BBL X1C.json",
        "model_id": "BL-P001",
    },
    "p1s": {
        "label": "Bambu Lab P1S",
        "name": "Bambu Lab P1S 0.4 nozzle",
        # P1S and P1P share the same set of process presets in OrcaSlicer's
        # bundle (different machine, identical kinematics).
        "machine_json": ORCA_BUNDLE / "machine" / "Bambu Lab P1S 0.4 nozzle.json",
        "process_base": ORCA_BUNDLE / "process" / "0.24mm Draft @BBL P1P.json",
        "model_id": "C11",
    },
}
DEFAULT_PRINTER = "x1c"

# Backwards-compat module attributes — kept so external callers that imported
# these names directly (older versions of app.py) don't break.
MACHINE_JSON = PRINTERS[DEFAULT_PRINTER]["machine_json"]
PROCESS_BASE = PRINTERS[DEFAULT_PRINTER]["process_base"]
# Scratch dir for merged profile JSONs + OrcaSlicer's temporary 3MF output.
# Kept inside the project so everything is self-contained and portable;
# also avoids macOS sandbox friction writing to ~/Downloads.
SLICER_OUT = BASE_DIR / ".cache"

BED_X, BED_Y = 256.0, 256.0  # X1C printable area from machine.json
BED_TOL = 0.5                # tolerance for float noise, mm


def fail(msg: str, **extra) -> None:
    print(json.dumps({"error": msg, **extra}, indent=2))
    sys.exit(1)


def sanitize(s: str | None) -> str:
    if not s:
        return "unknown"
    return re.sub(r"[^A-Za-z0-9._-]", "_", s).strip("_")[:80] or "unknown"


# ---------- extract ----------

def extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_content()
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                return re.sub(r"<[^>]+>", " ", part.get_content())
        return ""
    return msg.get_content() or ""


def cmd_extract(eml_path: str) -> None:
    src = Path(eml_path).expanduser().resolve()
    if not src.exists():
        fail(f"EML not found: {src}")

    with src.open("rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)

    sender = msg.get("From", "") or ""
    subject = msg.get("Subject", "") or ""
    body = extract_body(msg)

    login = sender.split("<")[-1].strip(">")
    slug = sanitize(login.split("@")[0] if "@" in login else sender)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    wd = WORK_DIR / f"{stamp}-{slug}"
    wd.mkdir(parents=True, exist_ok=True)

    stls: list[str] = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn and fn.lower().endswith(".stl"):
            safe = sanitize(fn.rsplit(".", 1)[0]) + ".stl"
            (wd / safe).write_bytes(part.get_payload(decode=True) or b"")
            stls.append(safe)

    (wd / "body.txt").write_text(body)
    (wd / "source.txt").write_text(str(src))

    print(json.dumps({
        "workdir": str(wd),
        "from": sender,
        "subject": subject,
        "body": body.strip(),
        "stls": stls,
        "source_eml": str(src),
    }, indent=2))


# ---------- slice ----------

def _fmt_time(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    mins = rem // 60
    return f"{h}h{mins:02d}m" if h else f"{mins}m"


def _hex_to_rgb(hex_str: str, fallback=(96, 96, 96)) -> tuple[int, int, int]:
    if not hex_str or not hex_str.startswith("#") or len(hex_str) not in (4, 7):
        return fallback
    try:
        if len(hex_str) == 4:
            return tuple(int(c * 2, 16) for c in hex_str[1:])
        return tuple(int(hex_str[i:i+2], 16) for i in (1, 3, 5))
    except ValueError:
        return fallback


# Common filament colour names → representative hex. Used to paint the thumbnail
# swatch in the colour the patron asked for (not the filament preset's default).
COLOR_NAME_HEX = {
    "red": "#D32F2F", "dark red": "#8E1515", "bright red": "#F44336",
    "blue": "#1976D2", "dark blue": "#0D47A1", "light blue": "#64B5F6",
    "navy": "#0D1B3E", "teal": "#00796B", "cyan": "#00838F",
    "green": "#388E3C", "dark green": "#1B5E20", "light green": "#81C784",
    "forest green": "#1B5E20", "lime": "#AEEA00",
    "yellow": "#FBC02D", "gold": "#F9A825",
    "orange": "#F57C00", "amber": "#FF8F00",
    "purple": "#7B1FA2", "violet": "#512DA8", "magenta": "#C2185B",
    "pink": "#D81B60", "hot pink": "#E91E63",
    "black": "#202020", "white": "#F5F5F5",
    "grey": "#616161", "gray": "#616161", "silver": "#9E9E9E",
    "brown": "#6D4C41", "tan": "#A1887F",
}


def _name_to_hex(name: str) -> str:
    n = (name or "").strip().lower()
    if not n:
        return "#606060"
    if n in COLOR_NAME_HEX:
        return COLOR_NAME_HEX[n]
    for key, hex_val in COLOR_NAME_HEX.items():
        if key in n:
            return hex_val
    return "#606060"


def _hex_to_name(hex_str: str) -> str:
    """Reverse of _name_to_hex for displaying a slicer-embedded filament hex
    as a human colour. Picks the closest entry in COLOR_NAME_HEX by RGB
    distance. Returns title-cased name, or empty string if no input."""
    if not hex_str or not hex_str.startswith("#"):
        return ""
    target = _hex_to_rgb(hex_str)
    best_name, best_dist = None, float("inf")
    for name, hx in COLOR_NAME_HEX.items():
        rgb = _hex_to_rgb(hx)
        dist = sum((a - b) ** 2 for a, b in zip(target, rgb))
        if dist < best_dist:
            best_dist = dist
            best_name = name
    # Pick a more readable casing: "Light blue" → "Light Blue"
    return " ".join(w.capitalize() for w in (best_name or "").split())


def _load_font(size: int):
    # Probe a few well-known TTF paths across platforms. Without one of
    # these, PIL's load_default() returns a bitmap font that ignores the
    # `size` argument entirely — every thumbnail comes out with 10px
    # text regardless of how big we ask. The PIL 10+ load_default()
    # accepts a size kwarg as a last-ditch fallback; older PIL doesn't,
    # hence the try/except.
    from PIL import ImageFont
    candidates = (
        # Windows
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        # Linux (Debian/Ubuntu DejaVu, near-universal)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _render_label_png(
    size: tuple[int, int],
    customer_first: str,
    color_name: str,
    plate_idx: int,
    plate_total: int,
    time_label: str,
    mass_g: float,
    date_label: str,
    variant: str = "",
) -> bytes:
    """Render a print-label thumbnail: colour swatch with the requested filament
    colour at the top, then patron name + plate-of-count + time/mass. This
    replaces the conventional 3D render with information that's actually useful
    to desk staff when picking a file off the printer's SD card list.
    """
    from PIL import Image, ImageDraw
    import io

    w, h = size
    color_hex = _name_to_hex(color_name)
    color_rgb = _hex_to_rgb(color_hex)
    # Contrast colour for text on the swatch
    luminance = 0.299 * color_rgb[0] + 0.587 * color_rgb[1] + 0.114 * color_rgb[2]
    on_swatch = (20, 20, 20) if luminance > 140 else (245, 245, 245)

    img = Image.new("RGB", (w, h), (248, 249, 252))
    draw = ImageDraw.Draw(img)

    # Scale fonts to image size (512px reference → sizes below; small 128px uses
    # the same ratios for a compact label).
    scale = w / 512
    font_color = _load_font(max(12, int(64 * scale)))
    font_name = _load_font(max(10, int(56 * scale)))
    font_body = _load_font(max(9, int(32 * scale)))
    font_small = _load_font(max(8, int(22 * scale)))

    # Top colour swatch (≈45% of height) with "LOAD:" label + colour name
    swatch_h = int(h * 0.45)
    draw.rectangle([(0, 0), (w, swatch_h)], fill=color_rgb)
    label_text = (color_name or "UNKNOWN").strip().upper()
    if w >= 256:
        draw.text((w / 2, swatch_h * 0.32), "LOAD", fill=on_swatch,
                  font=font_small, anchor="mm")
    draw.text((w / 2, swatch_h * 0.62), label_text, fill=on_swatch,
              font=font_color, anchor="mm")

    if w < 256:
        # Compact label (small thumbnail): just patron name under the swatch.
        draw.text((w / 2, swatch_h + (h - swatch_h) / 2), customer_first,
                  fill=(30, 40, 60), font=font_name, anchor="mm")
    else:
        # Full label: patron, plate, time+mass, date.
        y = swatch_h + int(h * 0.08)
        draw.text((w / 2, y), customer_first, fill=(30, 40, 60),
                  font=font_name, anchor="mm")

        plate_line = (
            f"Plate {plate_idx} of {plate_total}" if plate_total > 1 else "Single plate"
        )
        y += int(h * 0.11)
        draw.text((w / 2, y), plate_line, fill=(90, 100, 120),
                  font=font_body, anchor="mm")

        y += int(h * 0.09)
        mass_str = f"{mass_g:.1f} g"
        draw.text((w / 2, y), f"{time_label}   •   {mass_str}",
                  fill=(60, 70, 90), font=font_body, anchor="mm")

        # Date bottom-centre
        draw.text((w / 2, h - int(h * 0.05)), date_label,
                  fill=(140, 150, 170), font=font_small, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _add_thumbnail_refs(model_settings_xml: bytes, plate_nums: list[int]) -> bytes:
    """Insert thumbnail_file / top_file / pick_file metadata into each <plate>
    in model_settings.config so the X1C firmware finds them.
    """
    root = ET.fromstring(model_settings_xml) if model_settings_xml else ET.Element("config")
    for plate_elem in root.findall("plate"):
        pid = None
        for meta in plate_elem.findall("metadata"):
            if meta.get("key") == "plater_id":
                try:
                    pid = int(meta.get("value", "0"))
                except ValueError:
                    pid = None
                break
        if pid is None or pid not in plate_nums:
            continue
        existing = {m.get("key") for m in plate_elem.findall("metadata")}
        thumb_keys = {
            "thumbnail_file": f"Metadata/plate_{pid}.png",
            "thumbnail_no_light_file": f"Metadata/plate_no_light_{pid}.png",
            "top_file": f"Metadata/top_{pid}.png",
            "pick_file": f"Metadata/pick_{pid}.png",
            "pattern_bbox_file": f"Metadata/plate_{pid}.json",
        }
        # Find index of first non-metadata child (model_instance, etc.) to
        # insert before — keeps the BambuStudio-native ordering of metadata-
        # first, instances-last, which the firmware parser may depend on.
        insert_idx = len(list(plate_elem))
        for i, child in enumerate(plate_elem):
            if child.tag != "metadata":
                insert_idx = i
                break
        for key, value in thumb_keys.items():
            if key in existing:
                continue
            meta = ET.Element("metadata")
            meta.set("key", key)
            meta.set("value", value)
            plate_elem.insert(insert_idx, meta)
            insert_idx += 1

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def _fix_slice_info(si_bytes: bytes, model_id: str = "BL-P001") -> bytes:
    """OrcaSlicer writes slice_info.config with printer_model_id="" and no
    extruder_type / nozzle_volume_type. Bambu firmware uses these fields to
    verify a print file matches the physical machine — a wrong/empty model_id
    is what triggers "current nozzle setting does not match the slicing file"
    on X1C and outright print refusal on P1S. `model_id` defaults to BL-P001
    (X1C); pass `C11` for P1S, etc.
    """
    if not si_bytes:
        return si_bytes
    root = ET.fromstring(si_bytes)
    for plate in root.findall("plate"):
        for meta in plate.findall("metadata"):
            if meta.get("key") == "printer_model_id":
                meta.set("value", model_id)
        existing = {m.get("key") for m in plate.findall("metadata")}
        insert_idx = 1  # right after <metadata key="index">
        for i, child in enumerate(plate):
            if child.tag == "metadata" and child.get("key") == "index":
                insert_idx = i + 1
                break
        for key, value in (("extruder_type", "0"), ("nozzle_volume_type", "0")):
            if key in existing:
                continue
            meta = ET.Element("metadata")
            meta.set("key", key)
            meta.set("value", value)
            plate.insert(insert_idx, meta)
            insert_idx += 1
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def _make_printable(
    path: Path,
    customer_first: str = "",
    color_name: str = "",
    date_label: str = "",
    plates_meta: list[dict] | None = None,
    printer_model_id: str = "BL-P001",
) -> None:
    """Inject thumbnails + metadata so the X1C printer accepts the 3MF from
    SD card / network transfer. Called after slicing because OrcaSlicer CLI
    on macOS can't render thumbnails itself (no OpenGL context) and without
    them the firmware hides the file from its print-queue UI. Also patches
    slice_info.config to set printer_model_id / extruder_type / nozzle_volume_type
    which OrcaSlicer leaves blank and the firmware uses to validate that the
    slice matches the physical nozzle.

    If `customer_first` + `color_name` + `plates_meta` are provided, thumbnails
    render as print-labels (colour swatch + patron + plate N-of-M + time/mass)
    instead of a schematic plate layout — more useful on the printer's file
    browser than a 3D preview would be.
    """
    tmp = path.with_suffix(".tmp.3mf")
    with zipfile.ZipFile(path, "r") as zin:
        members = zin.namelist()
        plate_nums = sorted({
            int(re.search(r"plate_(\d+)\.json", m).group(1))
            for m in members
            if re.fullmatch(r"Metadata/plate_\d+\.json", m)
        })
        plate_data = {
            pn: json.loads(zin.read(f"Metadata/plate_{pn}.json"))
            for pn in plate_nums
        }
        model_settings = (
            zin.read("Metadata/model_settings.config")
            if "Metadata/model_settings.config" in members
            else b""
        )
        slice_info = (
            zin.read("Metadata/slice_info.config")
            if "Metadata/slice_info.config" in members
            else b""
        )

        # Files we rewrite or generate — skip them in the copy pass so retrofits
        # on already-processed 3MFs don't produce duplicate zip entries.
        rewritten = {"Metadata/model_settings.config", "Metadata/slice_info.config"}
        generated_pat = re.compile(
            r"Metadata/(plate_\d+\.png|plate_\d+_small\.png|plate_no_light_\d+\.png|top_\d+\.png|pick_\d+\.png|cut_information\.xml)"
        )

        meta_by_plate = {m["plate"]: m for m in (plates_meta or []) if "plate" in m}
        plate_total = len(plate_nums)

        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for m in members:
                if m in rewritten or generated_pat.fullmatch(m):
                    continue
                zout.writestr(m, zin.read(m))

            for pn in plate_nums:
                pm = meta_by_plate.get(pn, {})
                time_label = pm.get("time_label", "")
                mass_g = float(pm.get("filament_g", 0.0) or 0.0)
                plate_color = pm.get("color_name", color_name)
                main_png = _render_label_png(
                    (512, 512), customer_first, plate_color, pn, plate_total,
                    time_label, mass_g, date_label,
                )
                small_png = _render_label_png(
                    (128, 128), customer_first, plate_color, pn, plate_total,
                    time_label, mass_g, date_label,
                )
                zout.writestr(f"Metadata/plate_{pn}.png", main_png)
                zout.writestr(f"Metadata/plate_{pn}_small.png", small_png)
                zout.writestr(f"Metadata/plate_no_light_{pn}.png", main_png)
                zout.writestr(f"Metadata/top_{pn}.png", main_png)
                zout.writestr(f"Metadata/pick_{pn}.png", main_png)

            if "Metadata/cut_information.xml" not in members:
                zout.writestr(
                    "Metadata/cut_information.xml",
                    '<?xml version="1.0" encoding="utf-8"?>\n<objects>\n</objects>\n',
                )

            zout.writestr(
                "Metadata/model_settings.config",
                _add_thumbnail_refs(model_settings, plate_nums),
            )
            if slice_info:
                zout.writestr(
                    "Metadata/slice_info.config",
                    _fix_slice_info(slice_info, model_id=printer_model_id),
                )

    tmp.replace(path)


def _prep_filament(filament_src: Path, printer_canonical_name: str) -> Path:
    """Make a runtime copy of the filament JSON whose `compatible_printers`
    list includes the target printer. The base filament preset is locked to
    the X1C — without this, slicing for P1S fails with "filament
    not compatible with printer" before any actual slicing happens."""
    SLICER_OUT.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", printer_canonical_name)
    out = SLICER_OUT / f"_filament_{safe}.json"
    with filament_src.open() as f:
        data = json.load(f)
    cp = list(data.get("compatible_printers") or [])
    if printer_canonical_name not in cp:
        cp.append(printer_canonical_name)
    data["compatible_printers"] = cp
    with out.open("w") as f:
        json.dump(data, f, indent=2)
    return out


def _resolve_profile(base: Path, kind: str, overlay_path: Path | None = None) -> Path:
    """Walk `inherits` chain from `base` up through the OrcaSlicer profile tree,
    merge every level (deepest loses), optionally apply an overlay on top, and
    return a path to a self-contained JSON. OrcaSlicer CLI doesn't resolve the
    inheritance chain when a profile is passed via --load-settings — without
    merging here, we fall back to Slic3r defaults (e.g. 60 mm/s walls and
    1000 mm/s² X/Y acceleration) instead of real X1C values.
    """
    profile_dir = base.parent
    chain: list[dict] = []
    current: Path | None = base
    seen: set[str] = set()
    while current is not None and current.exists():
        key = current.stem
        if key in seen:
            break
        seen.add(key)
        with current.open() as f:
            data = json.load(f)
        chain.append(data)
        parent_name = data.get("inherits")
        current = (profile_dir / f"{parent_name}.json") if parent_name else None

    merged: dict = {}
    for layer in reversed(chain):  # deepest first, so shallower overrides win
        merged.update(layer)

    if overlay_path is not None:
        with overlay_path.open() as f:
            merged.update(json.load(f))

    # Strip only the inherits chain pointer; leave setting_id/instantiation
    # intact (CLI uses `instantiation: true` to recognise usable printers, and
    # drops the process↔printer compatibility check without it).
    merged.pop("inherits", None)
    merged["type"] = kind

    out = SLICER_OUT / f"_merged_{kind}.json"
    SLICER_OUT.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        json.dump(merged, f, indent=2)
    return out


def cmd_slice(args) -> None:
    wd = Path(args.workdir).resolve()
    if not wd.is_dir():
        fail(f"workdir does not exist: {wd}")

    printer_id = (getattr(args, "printer", None) or DEFAULT_PRINTER).lower()
    if printer_id not in PRINTERS:
        fail(f"unknown printer '{printer_id}'. Choices: {', '.join(sorted(PRINTERS))}")
    printer_cfg = PRINTERS[printer_id]

    stl_names = [s.strip() for s in args.stls.split(",") if s.strip()]
    clones = [int(c) for c in args.clones.split(",") if c.strip()]
    if len(stl_names) != len(clones):
        fail("--stls and --clones must have the same count")
    stl_paths = [str((wd / n).resolve()) for n in stl_names]
    for p in stl_paths:
        if not Path(p).exists():
            fail(f"STL not found in workdir: {p}")

    # --clone-objects is one count per input file, e.g. "4,2" for 4 of stl1 + 2 of stl2.
    clone_arg = ",".join(str(c) for c in clones)

    # Use a temp name during slicing; rename to the final `firstname_date_mass_time`
    # form after we've read the actual time + mass out of the sliced 3MF.
    temp_name = f"_slicing_{sanitize(args.customer)}.3mf"
    SLICER_OUT.mkdir(parents=True, exist_ok=True)

    machine_merged = _resolve_profile(printer_cfg["machine_json"], "machine")
    process_merged = _resolve_profile(printer_cfg["process_base"], "process", PROCESS_OVERLAY)
    # Bundled P1P process declares compatibility only with "Bambu Lab P1P 0.4
    # nozzle"; OrcaSlicer's loader rejects it as incompatible with P1S unless
    # we widen the list. Same logic applies to filaments via _prep_filament.
    with process_merged.open() as _f:
        _pdata = json.load(_f)
    _cp = list(_pdata.get("compatible_printers") or [])
    if printer_cfg["name"] not in _cp:
        _cp.append(printer_cfg["name"])
        _pdata["compatible_printers"] = _cp
        with process_merged.open("w") as _f:
            json.dump(_pdata, _f, indent=2)
    filament_path  = _prep_filament(FILAMENT_JSON, printer_cfg["name"])
    cmd = [
        str(SLICER_CLI),
        "--load-settings", f"{machine_merged};{process_merged}",
        "--load-filaments", str(filament_path),
        "--arrange", "1",
        "--orient", "1",
        # --slice 0 = slice every plate the arranger produced. If arrange spills
        # parts across multiple plates (common for large orders), --slice 1
        # would silently leave plates 2+ unsliced — the 3MF still "looks right"
        # but has no gcode for the extra plates.
        "--slice", "0",
        "--clone-objects", clone_arg,
        "--outputdir", str(SLICER_OUT),
        "--export-3mf", temp_name,
    ]
    if getattr(args, "scale", 1.0) and args.scale != 1.0:
        cmd += ["--scale", str(args.scale)]
    cmd += stl_paths

    result = subprocess.run(cmd, capture_output=True, text=True)

    src_3mf = SLICER_OUT / temp_name
    if not src_3mf.exists():
        fail(
            "slicer did not produce a 3MF",
            stderr=(result.stderr or "")[-2000:],
            stdout=(result.stdout or "")[-500:],
            returncode=result.returncode,
        )

    staged_3mf = wd / temp_name
    shutil.move(str(src_3mf), str(staged_3mf))
    # OrcaSlicer may also drop loose .gcode files alongside the 3MF; clean them up.
    for g in SLICER_OUT.glob("plate_*.gcode"):
        g.unlink()

    # Inspect first so we can feed per-plate time/mass into the label thumbnails.
    inspection = inspect_3mf(staged_3mf)
    plates = inspection["plates"]
    total_time_s = sum(p.get("prediction_seconds", 0) for p in plates)
    total_mass_g = round(sum(p.get("weight_grams", 0.0) for p in plates), 1)
    any_outside = any(p.get("outside", False) for p in plates)
    any_over_5h = any((p.get("prediction_seconds", 0) > 5 * 3600) for p in plates)

    # Final name: firstname_{Mon D}_{total_mass}g_{H}h{MM}m.3mf.
    # When arrange splits across multiple plates, the filename reports the
    # sum — the 3MF itself contains one plate per piece.
    first_name_raw = args.customer.strip().split()[0] if args.customer.strip() else "Unknown"
    first_name = sanitize(first_name_raw)
    today = _fmt_date_dom(datetime.now())

    _make_printable(
        staged_3mf,
        customer_first=first_name_raw,
        color_name=args.color,
        date_label=today,
        plates_meta=[
            {
                "plate": p.get("plate"),
                "time_label": _fmt_time(p.get("prediction_seconds", 0)),
                "filament_g": p.get("weight_grams", 0.0),
            }
            for p in plates
        ],
        printer_model_id=printer_cfg["model_id"],
    )
    # Strip down to BambuStudio print-file layout. Required for multi-plate
    # 3MFs to show up on the X1C SD browser; harmless for single-plate.
    _strip_to_print_file(staged_3mf)
    h, rem = divmod(int(total_time_s), 3600)
    mins = rem // 60
    time_file = f"{h}h{mins:02d}m"
    mass_str = f"{total_mass_g:.1f}g"
    final_name = f"{first_name}_{today}_{mass_str}_{time_file}.3mf"

    # Final 3MF lands directly in printqueue/work/ (one level up from the
    # per-order workdir) so desk staff can grab every order's output from a
    # single folder. Evidence (body.txt, STLs, temp files) stays in the workdir.
    # Collision: if two plates collide on mass/time, disambiguate with _2, _3, ...
    final_3mf = WORK_DIR / final_name
    n = 2
    while final_3mf.exists():
        final_3mf = WORK_DIR / f"{first_name}_{today}_{mass_str}_{time_file}_{n}.3mf"
        n += 1
    shutil.move(str(staged_3mf), str(final_3mf))

    print(json.dumps({
        "output_3mf": str(final_3mf),
        "customer": args.customer,
        "color": args.color,
        "printer": printer_id,
        "plate_count": len(plates),
        "total_time_seconds": total_time_s,
        "total_time_label": _fmt_time(total_time_s),
        "total_filament_g": total_mass_g,
        "any_over_5h": any_over_5h,
        "any_outside_bed": any_outside,
        "plates": [
            {
                "plate": p.get("plate"),
                "time_label": _fmt_time(p.get("prediction_seconds", 0)),
                "time_seconds": p.get("prediction_seconds", 0),
                "filament_g": p.get("weight_grams", 0.0),
                "over_5h": p.get("prediction_seconds", 0) > 5 * 3600,
                "outside": p.get("outside", False),
                "support_used": p.get("support_used", False),
                "objects": p.get("objects", []),
                "warnings": p.get("warnings", []),
            }
            for p in plates
        ],
    }, indent=2))


# ---------- inspect ----------
# BambuStudio writes authoritative layout data in the 3MF itself:
#   Metadata/plate_N.json        bbox_all + per-object bboxes (x_min, y_min, x_max, y_max)
#   Metadata/slice_info.config   outside flag, prediction (s), weight (g), support_used per plate
# So we just read those — no need to reconstruct transforms from 3dmodel.model.


def _read_3mf_member(src: Path, name: str) -> bytes | None:
    """Read a member from either a .3mf zip file or an already-unzipped directory."""
    if src.is_dir():
        p = src / name
        return p.read_bytes() if p.exists() else None
    with zipfile.ZipFile(src, "r") as z:
        try:
            return z.read(name)
        except KeyError:
            return None


def _list_3mf_members(src: Path) -> list[str]:
    if src.is_dir():
        return [
            str(p.relative_to(src)).replace("\\", "/")
            for p in src.rglob("*")
            if p.is_file()
        ]
    with zipfile.ZipFile(src, "r") as z:
        return z.namelist()


def read_printer_model_id(path: Path) -> str | None:
    """Pull the printer_model_id (e.g. "BL-P001" for X1C, "C11" for P1S) out of
    a 3MF's slice_info.config. Returns None if the file isn't a sliced 3MF or
    the metadata is missing. Used by the web app's send-to-printer flow to
    decide which physical printers a given file is compatible with."""
    si_bytes = _read_3mf_member(path, "Metadata/slice_info.config")
    if not si_bytes:
        return None
    try:
        root = ET.fromstring(si_bytes)
    except ET.ParseError:
        return None
    for plate in root.findall("plate"):
        for meta in plate.findall("metadata"):
            if meta.get("key") == "printer_model_id":
                v = (meta.get("value") or "").strip()
                if v:
                    return v
    return None


def inspect_3mf(path: Path) -> dict:
    """Return structured placement + slicing info for every plate in a 3MF.

    Each plate entry has: bbox_all, per-object bboxes, outside flag (from
    slice_info.config — authoritative for the "arrange pushed something off-bed"
    case), support_used, prediction_seconds, weight_grams, bed_type.
    """
    members = _list_3mf_members(path)
    plate_jsons = sorted(
        m for m in members
        if re.fullmatch(r"Metadata/plate_\d+\.json", m)
    )

    slice_info_by_plate: dict[int, dict[str, str]] = {}
    filament_colors_by_plate: dict[int, list[str]] = {}
    filaments_by_plate: dict[int, list[dict]] = {}
    filament_grams_by_plate: dict[int, float] = {}
    warnings_by_plate: dict[int, list[dict]] = {}
    si_bytes = _read_3mf_member(path, "Metadata/slice_info.config")
    if si_bytes:
        try:
            root = ET.fromstring(si_bytes)
            for plate in root.findall("plate"):
                meta_map = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
                try:
                    pidx = int(meta_map.get("index", "0"))
                except ValueError:
                    continue
                slice_info_by_plate[pidx] = meta_map
                # Full per-filament breakdown for AMS multi-material plates:
                # one <filament id="N"> per AMS slot used on the plate.
                # plate_N.json's filament_colors is sometimes empty, so
                # slice_info.config is the authoritative fallback.
                filaments = []
                for f in plate.findall("filament"):
                    hex_val = f.get("color", "") or ""
                    if not hex_val:
                        continue
                    try:
                        used_g = float(f.get("used_g", "0") or 0)
                    except ValueError:
                        used_g = 0.0
                    filaments.append({
                        "slot": f.get("id", ""),
                        "type": f.get("type", ""),
                        "color_hex": hex_val,
                        "color_name": _hex_to_name(hex_val),
                        "used_g": round(used_g, 1),
                    })
                filaments_by_plate[pidx] = filaments
                filament_colors_by_plate[pidx] = [fl["color_hex"] for fl in filaments]
                # Weight fallback: OrcaSlicer leaves plate `weight` empty and filament
                # `used_g` at 0. Compute grams from used_m (meters) using PLA density.
                # 1.75mm filament × 1.24 g/cm³ ≈ 2.98 g per meter.
                total_g = 0.0
                for fil in plate.findall("filament"):
                    try:
                        used_g = float(fil.get("used_g", "0") or 0)
                    except ValueError:
                        used_g = 0.0
                    if used_g > 0:
                        total_g += used_g
                        continue
                    try:
                        used_m = float(fil.get("used_m", "0") or 0)
                    except ValueError:
                        used_m = 0.0
                    total_g += used_m * 2.98
                filament_grams_by_plate[pidx] = total_g
                warnings_by_plate[pidx] = [
                    {
                        "msg": w.get("msg"),
                        "level": w.get("level"),
                        "code": (w.get("error_code") or "").strip(),
                    }
                    for w in plate.findall("warning")
                ]
        except ET.ParseError:
            pass

    plates: list[dict] = []
    for pj in plate_jsons:
        m = re.search(r"plate_(\d+)\.json", pj)
        if not m:
            continue
        pnum = int(m.group(1))
        data_bytes = _read_3mf_member(path, pj)
        if not data_bytes:
            continue
        try:
            data = json.loads(data_bytes)
        except json.JSONDecodeError:
            continue

        objects = []
        for obj in data.get("bbox_objects", []):
            b = obj.get("bbox") or [None] * 4
            if len(b) == 4 and all(v is not None for v in b):
                xmin, ymin, xmax, ymax = b
                off_bed = (
                    xmin < -BED_TOL or ymin < -BED_TOL
                    or xmax > BED_X + BED_TOL or ymax > BED_Y + BED_TOL
                )
                objects.append({
                    "name": obj.get("name"),
                    "bbox": {
                        "x_min": round(xmin, 2), "y_min": round(ymin, 2),
                        "x_max": round(xmax, 2), "y_max": round(ymax, 2),
                    },
                    "area_mm2": round(obj.get("area", 0), 2),
                    "off_bed": off_bed,
                })

        si = slice_info_by_plate.get(pnum, {})
        outside_authoritative = si.get("outside", "false") == "true"
        off_bed_derived = any(o["off_bed"] for o in objects)
        filament_colors = (
            data.get("filament_colors")
            or filament_colors_by_plate.get(pnum, [])
            or []
        )
        primary_hex = filament_colors[0] if filament_colors else ""

        plates.append({
            "plate": pnum,
            "bed_type": data.get("bed_type"),
            "bbox_all": data.get("bbox_all"),
            "objects": objects,
            "outside": outside_authoritative or off_bed_derived,
            "outside_source": "slice_info" if outside_authoritative else ("bbox" if off_bed_derived else "none"),
            "support_used": si.get("support_used", "false") == "true",
            "prediction_seconds": int(si.get("prediction", "0") or 0),
            "weight_grams": round(
                float(si.get("weight") or 0) or filament_grams_by_plate.get(pnum, 0.0),
                1,
            ),
            "filament_colors": filament_colors,
            "color_hex": primary_hex,
            "color_name": _hex_to_name(primary_hex) if primary_hex else "",
            # Full per-AMS-slot breakdown for multi-material plates.
            "filaments": filaments_by_plate.get(pnum, []),
            "warnings": warnings_by_plate.get(pnum, []),
        })

    return {"plates": plates}


def cmd_inspect(path: str) -> None:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        fail(f"path not found: {p}")
    print(json.dumps(inspect_3mf(p), indent=2))


# ---------- merge multi-plate 3MFs ----------
# OrcaSlicer's arrange won't split one color's items across plates by print
# time (only by geometry), so when a color bucket exceeds the 5h cap we have
# to slice in chunks and combine the chunks into a single multi-plate 3MF
# afterwards. This also handles true multi-colour orders: slice each colour
# separately, merge into one file with per-plate colour labels.


def _max_root_object_id(threedmodel_xml: str) -> int:
    """3dmodel.model's <resources> lists parent/assembly objects by numeric id.
    These are the ids the <build><item objectid="N"/> elements reference, and
    they collide between separately-sliced 3MFs (each source starts its parent
    ids at 2). We need to find each source's max id so we can offset the next
    source cleanly."""
    ids = [int(m) for m in re.findall(r'<object id="(\d+)"', threedmodel_xml)]
    return max(ids) if ids else 1


def _renumber_root_object_ids(threedmodel_xml: str, offset: int) -> str:
    """Renumber every <object id="N"> and <item objectid="N"> reference in
    3dmodel.model by `offset`. Side models referenced via <component objectid>
    use their own internal ids and are NOT touched."""
    if offset == 0:
        return threedmodel_xml
    threedmodel_xml = re.sub(
        r'(<object id=")(\d+)(")',
        lambda m: f'{m.group(1)}{int(m.group(2)) + offset}{m.group(3)}',
        threedmodel_xml,
    )
    threedmodel_xml = re.sub(
        r'(<item objectid=")(\d+)(")',
        lambda m: f'{m.group(1)}{int(m.group(2)) + offset}{m.group(3)}',
        threedmodel_xml,
    )
    return threedmodel_xml


def _extract_build_and_resources(threedmodel_xml: str) -> tuple[str, str]:
    """Pull the inside of <resources>...</resources> and the full <build>
    element out of a source 3dmodel.model so we can concatenate them."""
    res = re.search(r"<resources>(.*?)</resources>", threedmodel_xml, re.DOTALL)
    build = re.search(r"<build[^>]*>(.*?)</build>", threedmodel_xml, re.DOTALL)
    return (res.group(1).strip() if res else "",
            build.group(1).strip() if build else "")


def _merge_3mfs(sources: list[tuple[Path, str]], out_path: Path) -> None:
    """Merge N (possibly multi-plate) 3MFs into one combined multi-plate 3MF.

    Each source is (3mf_path, color_name). Plates appear in the merged output
    in source order; within a source, in source-plate-number order. The colour
    is applied to every plate from that source — for true per-plate colour you
    can attach those after via _make_printable's plates_meta argument.

    For each source we maintain an object-id offset to keep the parent ids in
    3D/3dmodel.model unique across sources; the offset is applied to all
    object-id references inside that source's plate blocks too.
    """
    merged_resources: list[str] = []
    merged_builds: list[str] = []
    merged_side_models: dict[str, bytes] = {}
    offset = 0

    # Per-source bookkeeping (object blocks, source-level offset)
    per_source: list[dict] = []
    # One entry per OUTPUT plate (multi-plate sources contribute multiple)
    per_plate: list[dict] = []

    # Header bits copied from the first source once
    content_types: bytes = b""
    rels_root: bytes = b""
    project_settings: bytes = b""
    model_settings_rels: bytes = b""
    cut_info: bytes = b""
    first_slice_info_header: str = ""

    for i, (src, color) in enumerate(sources):
        with zipfile.ZipFile(src, "r") as zin:
            members = zin.namelist()
            threedmodel = zin.read("3D/3dmodel.model").decode("utf-8")
            model_settings_xml = zin.read("Metadata/model_settings.config").decode("utf-8")
            slice_info_xml = zin.read("Metadata/slice_info.config").decode("utf-8")
            # Side mesh models (3D/Objects/*.model) — copy verbatim; their
            # filenames are unique per source STL so they don't collide.
            for m in members:
                if m.startswith("3D/Objects/"):
                    merged_side_models[m] = zin.read(m)
            if i == 0:
                content_types = zin.read("[Content_Types].xml")
                rels_root = zin.read("_rels/.rels")
                project_settings = zin.read("Metadata/project_settings.config")
                if "Metadata/_rels/model_settings.config.rels" in members:
                    model_settings_rels = zin.read("Metadata/_rels/model_settings.config.rels")
                if "Metadata/cut_information.xml" in members:
                    cut_info = zin.read("Metadata/cut_information.xml")
                # Header preamble of slice_info.config (everything before the
                # first <plate>) — copied from source 0 since these fields are
                # client/version metadata, not per-plate data.
                hdr = re.search(r"<config>(.*?)<plate>", slice_info_xml, re.DOTALL)
                first_slice_info_header = hdr.group(1) if hdr else "\n"

            # Discover every plate in this source by enumerating plate_N.json.
            source_plate_nums = sorted({
                int(re.search(r"plate_(\d+)\.json", m).group(1))
                for m in members
                if re.fullmatch(r"Metadata/plate_\d+\.json", m)
            })

            # Read each plate's per-plate files in one zip session.
            plate_files: dict[int, dict] = {}
            for pn in source_plate_nums:
                plate_files[pn] = {
                    "plate_gcode":     zin.read(f"Metadata/plate_{pn}.gcode"),
                    "plate_md5":       zin.read(f"Metadata/plate_{pn}.gcode.md5"),
                    "plate_json":      zin.read(f"Metadata/plate_{pn}.json"),
                    "plate_png":       zin.read(f"Metadata/plate_{pn}.png")            if f"Metadata/plate_{pn}.png"            in members else b"",
                    "plate_small_png": zin.read(f"Metadata/plate_{pn}_small.png")      if f"Metadata/plate_{pn}_small.png"      in members else b"",
                    "plate_nl_png":    zin.read(f"Metadata/plate_no_light_{pn}.png")   if f"Metadata/plate_no_light_{pn}.png"   in members else b"",
                    "top_png":         zin.read(f"Metadata/top_{pn}.png")              if f"Metadata/top_{pn}.png"              in members else b"",
                    "pick_png":        zin.read(f"Metadata/pick_{pn}.png")             if f"Metadata/pick_{pn}.png"             in members else b"",
                }

        # Offset this source's parent object ids so they don't collide with
        # other sources. Same offset applies to the source's plate blocks too.
        renumbered = _renumber_root_object_ids(threedmodel, offset)
        src_max_new_id = _max_root_object_id(renumbered)
        resources_inner, build_inner = _extract_build_and_resources(renumbered)
        merged_resources.append(resources_inner)
        merged_builds.append(build_inner)

        # Apply the offset to model_settings.config object ids as well, then
        # extract the source's <object> root blocks (these are the assembly
        # definitions; they're per-source, not per-plate).
        obj_pattern = re.compile(r'(<object\s+id=")(\d+)(")', re.DOTALL)
        ms_xml_offset = obj_pattern.sub(
            lambda m: f'{m.group(1)}{int(m.group(2)) + offset}{m.group(3)}',
            model_settings_xml,
        )
        source_object_blocks = [
            om.group(0)
            for om in re.finditer(r"<object\s[^>]*>.*?</object>", ms_xml_offset, re.DOTALL)
        ]

        # Extract every <plate> block keyed by plater_id so we can pair it back
        # to its source plate number when building per_plate entries below.
        plate_blocks_in_ms: dict[int, str] = {}
        for pm in re.finditer(r"<plate>(.*?)</plate>", ms_xml_offset, re.DOTALL):
            inner = pm.group(1)
            id_match = re.search(r'<metadata\s+key="plater_id"\s+value="(\d+)"', inner)
            if id_match:
                plate_blocks_in_ms[int(id_match.group(1))] = inner

        plate_blocks_in_si: dict[int, str] = {}
        for pm in re.finditer(r"<plate>(.*?)</plate>", slice_info_xml, re.DOTALL):
            inner = pm.group(1)
            id_match = re.search(r'<metadata\s+key="index"\s+value="(\d+)"', inner)
            if id_match:
                plate_blocks_in_si[int(id_match.group(1))] = inner

        per_source.append({
            "source_idx": i,
            "color": color,
            "offset": offset,
            "object_blocks": source_object_blocks,
        })

        # One per_plate entry per source plate, in source-plate order.
        for pn in source_plate_nums:
            per_plate.append({
                "source_idx": i,
                "color": color,
                "object_id_offset": offset,
                "source_plate_num": pn,
                "ms_plate_inner": plate_blocks_in_ms.get(pn, ""),
                "si_plate_inner": plate_blocks_in_si.get(pn, ""),
                **plate_files[pn],
            })

        offset = src_max_new_id  # next source starts past this one

    # Build the unified 3dmodel.model — same per-source approach as before.
    merged_3dmodel = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US"'
        ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
        ' xmlns:BambuStudio="http://schemas.bambulab.com/package/2021"'
        ' xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
        ' requiredextensions="p">\n'
        ' <metadata name="Application">BambuCLI-merge/1.0</metadata>\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        f' <metadata name="CreationDate">{datetime.now().strftime("%Y-%m-%d")}</metadata>\n'
        ' <resources>\n  ' + '\n  '.join(merged_resources) + '\n </resources>\n'
        ' <build>\n  ' + '\n  '.join(merged_builds) + '\n </build>\n'
        '</model>\n'
    )

    # Rewrite 3D/_rels/3dmodel.model.rels — union of all side-model references.
    rels_entries = []
    for rid, name in enumerate(sorted(merged_side_models.keys()), start=1):
        path_in_pkg = "/" + name
        rels_entries.append(
            f'<Relationship Target="{path_in_pkg}" Id="rel-{rid}" '
            f'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        )
    merged_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels_entries) +
        '</Relationships>'
    ).encode("utf-8")

    # Merge model_settings.config:
    #   - Union all <object> blocks across sources (deduped via per_source which
    #     has them once per source, not per plate)
    #   - Emit one <plate> block per output plate, with plater_id + file paths
    #     remapped from the source plate number to the new global index
    ms_object_blocks: list[str] = []
    for ps in per_source:
        ms_object_blocks.extend(ps["object_blocks"])

    ms_plate_blocks: list[str] = []
    si_plate_blocks: list[str] = []

    for new_idx, pp in enumerate(per_plate, start=1):
        off = pp["object_id_offset"]
        source_pn = pp["source_plate_num"]

        ms_inner = pp["ms_plate_inner"]
        if ms_inner:
            # Offset object_id refs inside this plate's model_instance blocks.
            inner = re.sub(
                r'(<metadata\s+key="object_id"\s+value=")(\d+)(")',
                lambda m: f'{m.group(1)}{int(m.group(2)) + off}{m.group(3)}',
                ms_inner,
            )
            # Renumber plater_id → new global index
            inner = re.sub(
                r'(<metadata\s+key="plater_id"\s+value=")\d+(")',
                rf'\g<1>{new_idx}\g<2>',
                inner,
            )
            # Rewrite file paths: source's plate_<source_pn>.X → plate_<new_idx>.X
            for key in ("gcode_file", "thumbnail_file", "thumbnail_no_light_file",
                        "top_file", "pick_file", "pattern_bbox_file"):
                inner = re.sub(
                    rf'(<metadata\s+key="{key}"\s+value="Metadata/)([^"]+?)([._]){source_pn}(\.\w+)(")',
                    rf'\g<1>\g<2>\g<3>{new_idx}\g<4>\g<5>',
                    inner,
                )
            ms_plate_blocks.append(f"<plate>{inner}</plate>")

        si_inner = pp["si_plate_inner"]
        if si_inner:
            si_renumbered = re.sub(
                r'(<metadata\s+key="index"\s+value=")\d+(")',
                rf'\g<1>{new_idx}\g<2>',
                si_inner,
            )
            si_plate_blocks.append(f"<plate>{si_renumbered}</plate>")

    merged_model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        + "\n".join(ms_object_blocks) + "\n"
        + "\n".join(ms_plate_blocks) + "\n"
        + '<assemble>\n</assemble>\n'
        + '</config>\n'
    ).encode("utf-8")

    merged_slice_info = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config>' + first_slice_info_header.split("<config>", 1)[-1]
        + "\n".join(si_plate_blocks) + "\n"
        + '</config>\n'
    ).encode("utf-8")

    # Write the merged 3MF — per-plate files renumbered to the new global index.
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("[Content_Types].xml", content_types)
        zout.writestr("_rels/.rels", rels_root)
        zout.writestr("3D/3dmodel.model", merged_3dmodel)
        zout.writestr("3D/_rels/3dmodel.model.rels", merged_rels)
        for name, data in merged_side_models.items():
            zout.writestr(name, data)
        for new_idx, pp in enumerate(per_plate, start=1):
            zout.writestr(f"Metadata/plate_{new_idx}.gcode",     pp["plate_gcode"])
            zout.writestr(f"Metadata/plate_{new_idx}.gcode.md5", pp["plate_md5"])
            zout.writestr(f"Metadata/plate_{new_idx}.json",      pp["plate_json"])
            # Thumbnails (if the source had them — _make_printable will
            # regenerate label-style ones afterwards anyway, but copying
            # whatever's there means a partial output is still valid).
            if pp["plate_png"]:
                zout.writestr(f"Metadata/plate_{new_idx}.png",            pp["plate_png"])
            if pp["plate_small_png"]:
                zout.writestr(f"Metadata/plate_{new_idx}_small.png",      pp["plate_small_png"])
            if pp["plate_nl_png"]:
                zout.writestr(f"Metadata/plate_no_light_{new_idx}.png",   pp["plate_nl_png"])
            if pp["top_png"]:
                zout.writestr(f"Metadata/top_{new_idx}.png",              pp["top_png"])
            if pp["pick_png"]:
                zout.writestr(f"Metadata/pick_{new_idx}.png",             pp["pick_png"])
        zout.writestr("Metadata/model_settings.config", merged_model_settings)
        zout.writestr("Metadata/slice_info.config", merged_slice_info)
        zout.writestr("Metadata/project_settings.config", project_settings)
        if model_settings_rels:
            zout.writestr("Metadata/_rels/model_settings.config.rels", model_settings_rels)
        if cut_info:
            zout.writestr("Metadata/cut_information.xml", cut_info)


# ---------- strip to BambuStudio print-file format ----------
# Bambu X1C firmware rejects multi-plate 3MFs that carry mesh data — it expects
# the "print file" flavour that BambuStudio's GUI produces when you hit Send to
# Printer: empty 3dmodel.model, no 3D/Objects/, no <object> blocks in
# model_settings.config, just gcode + thumbnails + plate manifest. Single-plate
# files with full mesh data seem to work, but stripping is harmless and also
# shrinks the file ~80% so we do it for every slice output.


def _strip_to_print_file(path: Path) -> None:
    """Convert a slicer-output 3MF into the BambuStudio "print file" shape so
    the X1C firmware's SD-card browser accepts it."""
    tmp = path.with_suffix(".tmp.3mf")
    with zipfile.ZipFile(path, "r") as zin:
        members = zin.namelist()

        ms_xml = (zin.read("Metadata/model_settings.config").decode("utf-8")
                  if "Metadata/model_settings.config" in members else "")
        # Strip <object>...</object> blocks (standalone object defs at the
        # root), leaving only <plate>...</plate> blocks intact.
        ms_stripped = re.sub(
            r"\s*<object\s[^>]*>.*?</object>\s*",
            "",
            ms_xml,
            flags=re.DOTALL,
        )

        # Rewrite 3dmodel.model with empty <resources> and self-closing <build/>.
        # Keep the original <metadata> block (app name, UUIDs) because the
        # firmware may check it.
        dgm_xml = zin.read("3D/3dmodel.model").decode("utf-8") if "3D/3dmodel.model" in members else ""
        dgm_stripped = re.sub(
            r"<resources>.*?</resources>",
            "<resources>\n </resources>",
            dgm_xml,
            flags=re.DOTALL,
        )
        dgm_stripped = re.sub(
            r"<build[^>]*>.*?</build>",
            "<build/>",
            dgm_stripped,
            flags=re.DOTALL,
        )

        skip = {
            "3D/3dmodel.model",
            "Metadata/model_settings.config",
            "3D/_rels/3dmodel.model.rels",
        }
        skip_prefix = "3D/Objects/"

        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for m in members:
                if m in skip or m.startswith(skip_prefix):
                    continue
                zout.writestr(m, zin.read(m))
            zout.writestr("3D/3dmodel.model", dgm_stripped)
            zout.writestr("Metadata/model_settings.config", ms_stripped)

    tmp.replace(path)


# ---------- split multi-plate 3MF into single-plate 3MFs ----------
# Bambu X1C firmware doesn't reliably show multi-plate 3MFs in its SD card file
# browser (even though they're valid 3MF format). Bambu Studio's "Send to
# Printer" flow splits multi-plate projects into per-plate files on the fly;
# we do the same here so the SD-card workflow stays simple.


def _split_plates(src: Path, out_dir: Path, base_name: str) -> list[Path]:
    """Split a multi-plate 3MF into N single-plate 3MFs.

    Writes `{base_name}_plate{N}.3mf` into `out_dir` for each plate, copying
    only that plate's gcode + thumbnails + slice_info entry + its objects from
    the original. Returns the list of written paths in plate order.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(src, "r") as zin:
        members = zin.namelist()
        plate_nums = sorted({
            int(re.search(r"plate_(\d+)\.json", m).group(1))
            for m in members
            if re.fullmatch(r"Metadata/plate_\d+\.json", m)
        })
        if len(plate_nums) <= 1:
            # Already single-plate; just copy as-is to keep the caller simple.
            dest = out_dir / f"{base_name}.3mf"
            dest.write_bytes(src.read_bytes())
            return [dest]

        # Parse files we need to rewrite per plate
        threedmodel = zin.read("3D/3dmodel.model").decode("utf-8")
        model_settings_xml = zin.read("Metadata/model_settings.config").decode("utf-8")
        slice_info_xml = zin.read("Metadata/slice_info.config").decode("utf-8")
        rels_dgm = (zin.read("3D/_rels/3dmodel.model.rels")
                    if "3D/_rels/3dmodel.model.rels" in members else b"")
        content_types = zin.read("[Content_Types].xml")
        rels_root = zin.read("_rels/.rels")
        project_settings = (zin.read("Metadata/project_settings.config")
                            if "Metadata/project_settings.config" in members else b"")
        model_settings_rels = (zin.read("Metadata/_rels/model_settings.config.rels")
                               if "Metadata/_rels/model_settings.config.rels" in members else b"")
        cut_info = (zin.read("Metadata/cut_information.xml")
                    if "Metadata/cut_information.xml" in members else b"")

        ms_root = ET.fromstring(model_settings_xml)
        si_root = ET.fromstring(slice_info_xml)
        # Collect every <object> block's raw XML keyed by id, so we can pick
        # only the ones referenced by the plate being extracted.
        object_blocks: dict[str, str] = {}
        for om in re.finditer(r'<object\s+id="(\d+)"[^>]*>.*?</object>',
                              model_settings_xml, re.DOTALL):
            object_blocks[om.group(1)] = om.group(0)

        plate_ms_xmls: dict[int, str] = {}
        for p in ms_root.findall("plate"):
            pid_meta = p.find("metadata[@key='plater_id']")
            if pid_meta is None:
                continue
            pid = int(pid_meta.get("value", "0"))
            plate_ms_xmls[pid] = ET.tostring(p, encoding="unicode")

        plate_si_xmls: dict[int, str] = {}
        for p in si_root.findall("plate"):
            idx_meta = p.find("metadata[@key='index']")
            if idx_meta is None:
                continue
            idx = int(idx_meta.get("value", "0"))
            plate_si_xmls[idx] = ET.tostring(p, encoding="unicode")

        si_header_match = re.search(r"<config>(.*?)<plate>", slice_info_xml, re.DOTALL)
        si_header_inner = si_header_match.group(1) if si_header_match else "\n"

    for pn in plate_nums:
        # Which object ids does this plate reference?
        used_ids: list[str] = re.findall(
            r'<metadata\s+key="object_id"\s+value="(\d+)"',
            plate_ms_xmls.get(pn, ""),
        )
        used_objects = [object_blocks[i] for i in used_ids if i in object_blocks]

        # Rewrite plate XML to refer to plate_1 (single-plate output renumbers)
        ms_plate_xml = plate_ms_xmls[pn]
        ms_plate_xml = re.sub(
            r'(<metadata\s+key="plater_id"\s+value=")\d+(")',
            r'\g<1>1\g<2>', ms_plate_xml)
        for key in ("gcode_file", "thumbnail_file", "thumbnail_no_light_file",
                    "top_file", "pick_file", "pattern_bbox_file"):
            ms_plate_xml = re.sub(
                rf'(<metadata\s+key="{key}"\s+value="Metadata/)([^"]+?)([._]){pn}(\.\w+)(")',
                rf'\g<1>\g<2>\g<3>1\g<4>\g<5>', ms_plate_xml)

        si_plate_xml = plate_si_xmls.get(pn, "")
        si_plate_xml = re.sub(
            r'(<metadata\s+key="index"\s+value=")\d+(")',
            r'\g<1>1\g<2>', si_plate_xml)

        new_ms = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
            + "\n".join(used_objects) + "\n"
            + ms_plate_xml + "\n"
            + '<assemble>\n</assemble>\n</config>\n'
        ).encode("utf-8")

        new_si = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<config>' + si_header_inner.split("<config>", 1)[-1]
            + si_plate_xml + "\n"
            + '</config>\n'
        ).encode("utf-8")

        # Filter the 3dmodel.model <build> to items that reference this plate's
        # objects. Simpler than rebuilding — strip non-matching <item> lines.
        build_re = re.compile(r"<build[^>]*>(.*?)</build>", re.DOTALL)
        dgm_copy = threedmodel
        bm = build_re.search(dgm_copy)
        if bm:
            build_inner = bm.group(1)
            kept_items = []
            for im in re.finditer(r'<item\s+objectid="(\d+)"[^>]*/>', build_inner):
                if im.group(1) in used_ids:
                    kept_items.append(im.group(0))
            new_build_inner = "\n  " + "\n  ".join(kept_items) + "\n "
            new_build = bm.group(0).replace(bm.group(1), new_build_inner)
            dgm_copy = build_re.sub(new_build, dgm_copy)

        dest = out_dir / f"{base_name}_plate{pn}.3mf"
        with zipfile.ZipFile(src, "r") as zin, \
             zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
            zout.writestr("[Content_Types].xml", content_types)
            zout.writestr("_rels/.rels", rels_root)
            zout.writestr("3D/3dmodel.model", dgm_copy)
            if rels_dgm:
                zout.writestr("3D/_rels/3dmodel.model.rels", rels_dgm)
            # Side models — copy them all (simpler than filtering; the unused
            # ones are dead weight but the printer ignores them).
            for m in zin.namelist():
                if m.startswith("3D/Objects/"):
                    zout.writestr(m, zin.read(m))
            # This plate's per-plate files, renumbered to plate_1
            for ext in ("gcode", "gcode.md5", "json"):
                src_name = f"Metadata/plate_{pn}.{ext}"
                if src_name in zin.namelist():
                    zout.writestr(f"Metadata/plate_1.{ext}", zin.read(src_name))
            for src_name, dst_name in (
                (f"Metadata/plate_{pn}.png",          "Metadata/plate_1.png"),
                (f"Metadata/plate_{pn}_small.png",    "Metadata/plate_1_small.png"),
                (f"Metadata/plate_no_light_{pn}.png", "Metadata/plate_no_light_1.png"),
                (f"Metadata/top_{pn}.png",            "Metadata/top_1.png"),
                (f"Metadata/pick_{pn}.png",           "Metadata/pick_1.png"),
            ):
                if src_name in zin.namelist():
                    zout.writestr(dst_name, zin.read(src_name))
            zout.writestr("Metadata/model_settings.config", new_ms)
            zout.writestr("Metadata/slice_info.config", new_si)
            if project_settings:
                zout.writestr("Metadata/project_settings.config", project_settings)
            if model_settings_rels:
                zout.writestr("Metadata/_rels/model_settings.config.rels", model_settings_rels)
            if cut_info:
                zout.writestr("Metadata/cut_information.xml", cut_info)
        written.append(dest)
    return written


# ---------- receipt ----------

RECEIPT_WIDTH = 32  # Empirical printable width.
# Pre-swap (TM-T88V): 48 spilled by ~20, 32 spilled by ~4, 28 fit.
# Post-swap (generic 80mm ESC/POS clone, profile="default"): 28 left a
# ~10% margin on both sides. The clone uses a slightly narrower char
# pitch at Font B than the Epson did, so 32 fills the same physical
# width that 28 did before. Bump again if a future printer differs.


def _box_label(label: str, width: int = RECEIPT_WIDTH, indent: int = 2) -> str:
    """Format '{indent}Label ..... [ ]' padded to `width`. Used by both
    the text-preview and ESC/POS print paths so the two layouts stay in
    sync. Caller appends \\n as appropriate."""
    pad = max(1, width - indent - len(label) - 3)
    return f"{' ' * indent}{label}{' ' * pad}[ ]"


def _signature_line(label: str = "Completed by:", width: int = RECEIPT_WIDTH) -> str:
    """Format '  Label ___________' padded to `width`. No longer used
    for staff-handoff lines (those are circle-able initials now) — kept
    available for any external callers."""
    prefix = f"  {label} "
    underscores = max(4, width - len(prefix))
    return f"{prefix}{'_' * underscores}"


# Initials staff circle on the printed receipt at hand-off. Order
# matches the web dropdown so a visual scan lines up across surfaces.
# Mapping (kept in a comment because it's organizational, not code):
#   AB = Aspen   AN = Alex   AP = Amanda   SA = Sheila   WA = Waren
STAFF_INITIALS: list[str] = ["AB", "AN", "AP", "SA", "WA"]


def _circle_initials_line(indent: int = 2) -> str:
    """Render a row of staff initials with enough horizontal breathing
    room for a pen to circle one. 4 spaces between initials keeps the
    line under RECEIPT_WIDTH while staying clearly pen-circleable."""
    return f"{' ' * indent}{'    '.join(STAFF_INITIALS)}"


PRICE_PER_GRAM = 0.05  # CAD, library's current rate
DEFAULT_ORDER_TAKEN_BY = "Alex"  # CLI / API fallback when no staff is picked


def _format_card(raw: str) -> str:
    """Library cards are 14 digits; display grouped 6-4-4 per the library's
    own receipt convention (the leading 6 digits are the branch/system code,
    the trailing 8 identify the patron)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 14:
        return f"{digits[0:6]} {digits[6:10]} {digits[10:14]}"
    return raw.strip()


def _files_for_plate(plate: dict) -> list[tuple[str, int]]:
    """Aggregate objects on one plate into (name, count) pairs, stripping the
    `_N` clone suffix OrcaSlicer appends to each copy."""
    counts: dict[str, int] = {}
    for obj in plate.get("objects", []) or []:
        name = obj.get("name", "") or ""
        base = re.sub(r"\.stl_\d+$", ".stl", name)
        counts[base] = counts.get(base, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _aggregate_files(plates: list[dict]) -> list[tuple[str, int]]:
    """OrcaSlicer names cloned objects like `Foo.stl_1`, `Foo.stl_2`. Strip the
    trailing `_N` suffix on clones so we can tally copies of each source STL
    regardless of which plate they landed on.
    """
    counts: dict[str, int] = {}
    for p in plates:
        for obj in p.get("objects", []):
            name = obj.get("name", "") or ""
            # Strip common trailing clone suffix if present
            base = re.sub(r"\.stl_\d+$", ".stl", name)
            base = base.removesuffix(".stl") if hasattr(base, "removesuffix") else base
            counts[base] = counts.get(base, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _render_receipt_text(
    customer: str,
    card: str,
    colors: list[str],          # per-plate; length 1 broadcasts to all plates
    total_time_s: int,
    total_mass_g: float,
    price: float,
    when: datetime,
    plates: list[dict],
    sd_card: str = "",
    order_taken_by: str = "",
    sliced_filename: str = "",
) -> str:
    w = RECEIPT_WIDTH
    sep = "=" * w
    thin = "-" * w

    lines: list[str] = []
    lines.append(sep)
    lines.append("Makerspace @ McLean Branch · 3D Print".center(w))
    lines.append(sep)
    lines.append("")
    # Customer name — big/bold on hardware, upper+centred in preview
    lines.append(customer.upper().center(w))
    lines.append("")
    lines.append(f"Card: {_format_card(card)}")
    lines.append(f"Date: {_fmt_receipt_dt(when)}")
    if sliced_filename:
        # Truncate to fit width: "Source: " prefix (8) + filename. The
        # 3MF name is what staff types into Koha to find the order later
        # and what shows up on the printer's touchscreen, so worth
        # surfacing prominently.
        avail = w - len("Source: ")
        sf = sliced_filename if len(sliced_filename) <= avail else sliced_filename[: avail - 1] + "…"
        lines.append(f"Source: {sf}")
    lines.append(_box_label("Needs waiver signed", indent=0))
    lines.append(thin)

    # Broadcast single colour to all plates if only one provided
    plate_colors = colors if len(colors) == len(plates) else [colors[0]] * len(plates)

    # File list (single-plate shows files up front; multi-plate nests them
    # under each plate row inside the table so each file stays associated
    # with the plate it prints on).
    if len(plates) == 1:
        for name, qty in _files_for_plate(plates[0]):
            count = f"x {qty}"
            avail = w - 10 - len(count) - 1
            display = name if len(name) <= avail else name[: avail - 1] + "…"
            lines.append(f"  File: {display:<{avail}} {count}")
        lines.append("")

    # Plate table — same headers whether single- or multi-plate.
    # Column widths sum to RECEIPT_WIDTH (currently 32):
    #   #(1) + sp + Colour(10) + sp + Grams(5) + sp + Hr(2) + sp + Min(3) + sp×3 + Done(4)
    lines.append(
        f"{'#':<1} {'Colour':<10} {'Grams':>5} {'Hr':>2} {'Min':>3}   {'Done':<4}"
    )
    for i, p in enumerate(plates):
        pn = p.get("plate", i + 1)
        secs = p.get("prediction_seconds", 0) or 0
        h = secs // 3600
        mins = (secs % 3600) // 60
        grams = p.get("weight_grams", 0.0) or 0.0
        pc = (plate_colors[i] or "")[:10]
        lines.append(
            f"{pn:>1} {pc:<10} {grams:>5.1f} {h:>2} {mins:>3}   {'[ ]':>4}"
        )
        if len(plates) > 1:
            for fname, qty in _files_for_plate(p):
                # Layout: 7-space indent + display + " x " + qty == w
                suffix = f" x {qty}"
                avail = w - 7 - len(suffix)
                display = fname if len(fname) <= avail else fname[: avail - 1] + "…"
                lines.append(f"       {display}{suffix}")
            lines.append("")  # blank line between plates

    lines.append(thin)
    lines.append(f"  Total:      ${price:.2f}")
    lines.append(f"  Total mass: {total_mass_g:.1f} g")
    lines.append(f"  Total time: {_fmt_time(total_time_s)}")
    lines.append("")
    lines.append(sep)
    lines.append(f"  Order taken by: {order_taken_by or DEFAULT_ORDER_TAKEN_BY}")
    lines.append(_box_label("Charged in Koha"))
    # "Completed by:" now uses circle-able initials instead of an
    # underline so staff can sign off without a pen-on-the-line.
    lines.append("  Completed by:")
    lines.append("")
    lines.append(_circle_initials_line())
    lines.append("")
    if sd_card.strip():
        # Dashboard knew which physical card the file was saved to — show
        # it prominently instead of the manually-circled-after-the-fact
        # R1/R2/R3/B1 row that used to live here.
        lines.append(f"  SD card:  {sd_card.strip()}")
    else:
        lines.append(f"  SD card:  R1  R2  R3  B1")
    lines.append("")
    lines.append(f"  Printer:  1  2  3")
    lines.append("")
    lines.append(_box_label("Notified for pickup"))
    lines.append(_box_label("Order picked up"))
    # Authorising-staff initials for the hand-off itself. Blank lines
    # bracket the row so the pen has clear vertical space to circle.
    lines.append("")
    lines.append(_circle_initials_line())
    lines.append("")
    lines.append(sep)
    return "\n".join(lines) + "\n"


def _send_to_tm_t88v(
    customer: str, card: str, colors: list[str],
    total_time_s: int, total_mass_g: float, price: float,
    when: datetime,
    plates: list[dict],
    sd_card: str = "",
    order_taken_by: str = "",
    sliced_filename: str = "",
) -> None:
    """Push the receipt to an Epson TM-T88V over USB. Uses python-escpos so
    the heavy formatting (bold/size/centre/cut) happens in hardware, which
    looks cleaner than dumping plain ASCII.

    Deps: `pip3 install python-escpos pyusb` + `brew install libusb`.
    """
    # pyusb's libusb1 backend needs libusb-1.0 findable by the OS linker.
    import os
    _pyusb_backend = None
    if sys.platform == "darwin":
        # Homebrew puts it at /opt/homebrew/lib, which isn't on DYLD by default.
        brew_lib = "/opt/homebrew/lib"
        if os.path.isdir(brew_lib):
            existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
            if brew_lib not in existing.split(":"):
                os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = (
                    f"{brew_lib}:{existing}" if existing else brew_lib
                )
    elif sys.platform == "win32":
        # libusb-package ships libusb-1.0.dll and a backend factory that loads
        # it explicitly — ctypes.util.find_library() can't find DLLs added via
        # os.add_dll_directory(), so the explicit backend is required here.
        try:
            import libusb_package
            _pyusb_backend = libusb_package.get_libusb1_backend()
        except ImportError:
            fail("libusb-package not installed. Run: pip install libusb-package")

    try:
        from escpos.printer import Usb
    except ImportError:
        fail("python-escpos not installed. Run: pip install python-escpos pyusb")

    # Generic 80mm ESC/POS thermal (enumerates as "USB 80Series2" on Windows).
    # Same ESC/POS command set as the prior TM-T88V; only the USB IDs and
    # python-escpos profile change. If a replacement printer is swapped in,
    # check IDs with `system_profiler SPUSBDataType` on macOS, or Device
    # Manager / `Get-PnpDevice -Class USB` on Windows.
    PRINTER_VID = 0x0FE6
    PRINTER_PID = 0x811E
    try:
        usb_args = {"idVendor": PRINTER_VID, "idProduct": PRINTER_PID}
        if _pyusb_backend is not None:
            usb_args["backend"] = _pyusb_backend
        # No vendor-specific profile — the "default" profile in python-escpos
        # is the safe baseline for generic ESC/POS clones (48 cpl Font A on
        # 80mm). RECEIPT_WIDTH=28 fits comfortably either way.
        p = Usb(usb_args=usb_args, timeout=0, profile="default")
    except Exception as e:
        fail(f"could not open USB printer (VID=0x{PRINTER_VID:04X} PID=0x{PRINTER_PID:04X}): {e}")

    # Use Font B (9x17) for body text — ~64 cpl on 80mm paper and a less
    # 1:2-stretched aspect than Font A (12x24). set() is incremental in
    # python-escpos 3.x so font='b' here sticks for all subsequent text.
    p.set(font="b")

    sep = "=" * RECEIPT_WIDTH
    thin = "-" * RECEIPT_WIDTH

    # Top banner — one line, centred, bold
    p.set(font="b", align="center", bold=True)
    p.text(sep + "\n")
    p.text("Makerspace @ McLean Branch · 3D Print\n")
    p.text(sep + "\n")
    p.set(font="b", align="left", bold=False)
    p.text("\n")

    # Customer name — double-WIDTH + bold, centred. double_width with Font B
    # gives ~18x17 dots (squarish) instead of the 1:4 stretch you get with
    # double_height. Most important info, eye-catching but not column-stealing.
    p.set(font="b", align="center", bold=True, double_width=True)
    p.text(customer.upper() + "\n")
    p.set(font="b", align="left", bold=False, double_width=False)
    p.text("\n")

    p.text(f"Card: {_format_card(card)}\n")
    p.text(f"Date: {_fmt_receipt_dt(when)}\n")
    if sliced_filename:
        avail = RECEIPT_WIDTH - len("Source: ")
        sf = sliced_filename if len(sliced_filename) <= avail else sliced_filename[: avail - 1] + "…"
        p.text(f"Source: {sf}\n")
    p.text(_box_label("Needs waiver signed", indent=0) + "\n")
    p.text(thin + "\n")

    plate_colors = colors if len(colors) == len(plates) else [colors[0]] * len(plates)

    if len(plates) == 1:
        for name, qty in _files_for_plate(plates[0]):
            count = f"x {qty}"
            avail = RECEIPT_WIDTH - 10 - len(count) - 1
            display = name if len(name) <= avail else name[: avail - 1] + "…"
            p.text(f"  File: {display:<{avail}} {count}\n")
        p.text("\n")

    p.set(font="b", bold=True)
    # Column widths sum to RECEIPT_WIDTH (32). See _render_receipt_text for
    # the breakdown — both paths kept in sync so the staff-facing preview
    # matches what actually prints.
    p.text(
        f"{'#':<1} {'Colour':<10} {'Grams':>5} {'Hr':>2} {'Min':>3}   {'Done':<4}\n"
    )
    p.set(font="b", bold=False)
    for i, pl in enumerate(plates):
        pn = pl.get("plate", i + 1)
        secs = pl.get("prediction_seconds", 0) or 0
        h = secs // 3600
        mins = (secs % 3600) // 60
        grams = pl.get("weight_grams", 0.0) or 0.0
        pc = (plate_colors[i] or "")[:10]
        p.text(
            f"{pn:>1} {pc:<10} {grams:>5.1f} {h:>2} {mins:>3}   {'[ ]':>4}\n"
        )
        if len(plates) > 1:
            for fname, qty in _files_for_plate(pl):
                suffix = f" x {qty}"
                avail = RECEIPT_WIDTH - 7 - len(suffix)
                display = fname if len(fname) <= avail else fname[: avail - 1] + "…"
                p.text(f"       {display}{suffix}\n")
            p.text("\n")  # blank line between plates

    p.text(thin + "\n")
    p.set(font="b", bold=True)
    p.text(f"  Total:      ${price:.2f}\n")
    p.set(font="b", bold=False)
    p.text(f"  Total mass: {total_mass_g:.1f} g\n")
    p.text(f"  Total time: {_fmt_time(total_time_s)}\n")
    p.text("\n")
    p.text(sep + "\n")
    p.text(f"  Order taken by: {order_taken_by or DEFAULT_ORDER_TAKEN_BY}\n")
    p.text(_box_label("Charged in Koha") + "\n")
    # Circle-able initials replace the old "Completed by: ___________"
    # signature line. Blank lines bracket the initials so the pen has
    # vertical room to draw a clean circle.
    p.text("  Completed by:\n")
    p.text("\n")
    p.text(_circle_initials_line() + "\n")
    p.text("\n")
    if sd_card.strip():
        # Dashboard auto-fill: print the resolved card name bold so staff
        # sees it at a glance instead of having to circle on a checklist.
        p.set(font="b", bold=True)
        p.text(f"  SD card:  {sd_card.strip()}\n")
        p.set(font="b", bold=False)
    else:
        p.text(f"  SD card:  R1  R2  R3  B1\n")
    p.text("\n")
    p.text(f"  Printer:  1  2  3\n")
    p.text("\n")
    p.text(_box_label("Notified for pickup") + "\n")
    p.text(_box_label("Order picked up") + "\n")
    # Hand-off authorisation initials. Blank lines mirror the "Completed
    # by" block above for a consistent circle-able layout.
    p.text("\n")
    p.text(_circle_initials_line() + "\n")
    p.text("\n")
    p.text(sep + "\n\n\n")
    p.cut()


def cmd_receipt(args) -> None:
    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        fail(f"3MF not found: {path}")

    inspection = inspect_3mf(path)
    plates = inspection["plates"]
    total_time_s = sum(p.get("prediction_seconds", 0) for p in plates)
    total_mass_g = round(sum(p.get("weight_grams", 0.0) for p in plates), 1)

    price = round(total_mass_g * PRICE_PER_GRAM, 2)
    when = datetime.now()

    # --color accepts a single colour ("Purple") that broadcasts to all plates
    # or a comma-separated list ("White,White,Black") matching plate order for
    # multi-colour orders.
    colors = [c.strip() for c in args.color.split(",") if c.strip()]
    if not colors:
        fail("--color cannot be empty")

    common_kwargs = dict(
        customer=args.customer, card=args.card, colors=colors,
        total_time_s=total_time_s, total_mass_g=total_mass_g,
        price=price, when=when,
        plates=plates,
        sd_card=getattr(args, "sd_card", "") or "",
        order_taken_by=getattr(args, "order_taken_by", "") or "",
        # The 3MF the receipt was rendered from — staff use this name
        # to find the job in Koha / on the printer touchscreen.
        sliced_filename=path.name,
    )

    if args.send:
        _send_to_tm_t88v(**common_kwargs)
        print(json.dumps({
            "sent": True,
            "price_cad": price,
            "plate_count": len(plates),
        }, indent=2))
    else:
        print(_render_receipt_text(**common_kwargs), end="")


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("extract", help="unpack an EML into a work dir")
    ex.add_argument("eml")

    sl = sub.add_parser("slice", help="slice a bucket of STLs for one plate")
    sl.add_argument("--workdir", required=True)
    sl.add_argument("--customer", required=True)
    sl.add_argument("--color", required=True)
    sl.add_argument("--stls", required=True, help="comma-separated STL filenames (inside workdir)")
    sl.add_argument("--clones", required=True, help="comma-separated clone counts matching --stls")
    sl.add_argument("--scale", type=float, default=1.0,
                    help="uniform scale factor applied to every STL (default 1.0 = original size)")
    sl.add_argument("--printer", default=DEFAULT_PRINTER,
                    choices=sorted(PRINTERS.keys()),
                    help=f"target printer (default: {DEFAULT_PRINTER}); choices: {', '.join(sorted(PRINTERS.keys()))}")

    ins = sub.add_parser("inspect", help="unzip a 3MF and report per-object absolute bounds")
    ins.add_argument("path")

    rc = sub.add_parser("receipt", help="render (or print) an 80mm receipt for a sliced 3MF")
    rc.add_argument("path", help="path to the sliced 3MF")
    rc.add_argument("--customer", required=True)
    rc.add_argument("--card", required=True, help="library card number (spaces allowed)")
    rc.add_argument("--color", required=True)
    rc.add_argument("--sd-card", default="",
                    help="resolved SD-card label from the dashboard (e.g. R1/R2/R3/B1). "
                         "When set, prints prominently instead of the manual-circle row.")
    rc.add_argument("--order-taken-by", default="",
                    help="staff first name to print on the 'Order taken by:' line. "
                         f"Defaults to '{DEFAULT_ORDER_TAKEN_BY}' when empty.")
    rc.add_argument("--send", action="store_true",
                    help="send to the USB TM-T88V instead of printing preview to stdout")

    args = ap.parse_args()
    if args.cmd == "extract":
        cmd_extract(args.eml)
    elif args.cmd == "slice":
        cmd_slice(args)
    elif args.cmd == "inspect":
        cmd_inspect(args.path)
    elif args.cmd == "receipt":
        cmd_receipt(args)


if __name__ == "__main__":
    main()
