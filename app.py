#!/usr/bin/env python3
"""BambuCLI web intake. FastAPI front end wrapping slice_order.py.

Two intake flows:

  slice   — upload STL(s) + patron/colour/qty → slices via slice_order.py
  import  — upload an externally-sliced .3mf → inspects and tracks the order

Run from the BambuCLI folder:

    uvicorn app:app --host 0.0.0.0 --port 8000

Or, via the LaunchDaemon recipe in SETUP.md, at boot.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
# Direct imports for the multi-bucket slice path that needs to merge results
# in-process; subprocess remains the path for the standard slice/inspect/receipt
# calls so each invocation gets a fresh interpreter.
from slice_order import (  # noqa: E402
    _merge_3mfs, _make_printable, _strip_to_print_file,
    PRINTERS, DEFAULT_PRINTER,
)
from email_parser import parse_email  # noqa: E402
WORK_DIR = BASE_DIR / "printqueue" / "work"
LEDGER_JSON = BASE_DIR / "printqueue" / "orders.json"
SLICE_ORDER = BASE_DIR / "slice_order.py"
PRICE_PER_GRAM = 0.05  # CAD

app = FastAPI(title="Makerspace Print Intake")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------- helpers ----------

def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s).strip("_")[:80] or "unknown"


def _fmt_time(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    mins = rem // 60
    return f"{h}h{mins:02d}m" if h else f"{mins}m"


def _read_ledger() -> list[dict]:
    if not LEDGER_JSON.exists():
        return []
    try:
        return json.loads(LEDGER_JSON.read_text())
    except json.JSONDecodeError:
        return []


def _write_ledger(records: list[dict]) -> None:
    LEDGER_JSON.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_JSON.write_text(json.dumps(records, indent=2))


def _append_to_ledger(record: dict) -> None:
    records = _read_ledger()
    records.append(record)
    _write_ledger(records)


def _run(cmd: list[str]) -> dict:
    """Run a slice_order.py subcommand and parse its JSON stdout."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        detail = (r.stderr or "")[-2000:] or (r.stdout or "")[-2000:]
        raise HTTPException(500, f"pipeline failed (rc={r.returncode}): {detail}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise HTTPException(500, f"non-JSON output from {cmd[1:3]}: {r.stdout[:500]}")


def _slice(workdir: Path, customer: str, color: str,
           stls: list[str], clones: list[int], scale: float,
           printer: str = DEFAULT_PRINTER) -> dict:
    cmd = [
        "python3", str(SLICE_ORDER), "slice",
        "--workdir", str(workdir),
        "--customer", customer,
        "--color", color,
        "--stls", ",".join(stls),
        "--clones", ",".join(str(c) for c in clones),
        "--printer", printer,
    ]
    if scale and scale != 1.0:
        cmd += ["--scale", str(scale)]
    return _run(cmd)


def _inspect(path: Path) -> dict:
    return _run(["python3", str(SLICE_ORDER), "inspect", str(path)])


def _send_receipt(path: Path, customer: str, card: str, colors: str) -> dict:
    return _run([
        "python3", str(SLICE_ORDER), "receipt", str(path),
        "--customer", customer, "--card", card, "--color", colors,
        "--send",
    ])


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


@app.post("/history/sync")
async def history_sync() -> dict:
    """Scan printqueue/work/*.3mf and append a ledger row for any 3MF that
    isn't already tracked. Patron full name + library card aren't in the 3MF
    metadata, so backfill rows have first-name only (parsed from the filename
    convention) and an empty card field — staff can edit those manually if
    needed by editing printqueue/orders.json. Plate stats, file size, and
    detected per-plate colours come straight from inspect_3mf."""
    existing = {r.get("filename") for r in _read_ledger()}
    added: list[str] = []
    skipped: list[str] = []
    for path in sorted(WORK_DIR.glob("*.3mf")):
        if path.name in existing:
            continue
        parsed = _parse_convention_filename(path.name)
        if not parsed:
            skipped.append(path.name)
            continue
        try:
            inspection = _inspect(path)
        except Exception:
            skipped.append(path.name)
            continue
        plates = inspection["plates"]
        # Use file mtime as a stand-in for "when this order was processed"
        ts = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        # Auto-detected colours per plate (best-effort — slicer's filament hex
        # may not match what the patron actually asked for, especially for old
        # CLI slices that used a single default filament preset).
        plate_colors = [p.get("color_name") or "" for p in plates]

        record = _build_record(
            customer=parsed["first_name"],
            card="",
            colors=plate_colors,
            flow="backfill",
            output_3mf=path,
            inspection=inspection,
        )
        record["timestamp"] = ts
        _append_to_ledger(record)
        added.append(path.name)

    return {"added": added, "skipped": skipped, "added_count": len(added)}


@app.post("/orders/delete")
async def orders_delete(
    filename: str = Form(...),
    delete_file: bool = Form(False),
) -> dict:
    """Remove a row from the ledger. Optionally also delete the 3MF on disk."""
    records = _read_ledger()
    new = [r for r in records if r.get("filename") != filename]
    if len(new) == len(records):
        raise HTTPException(404, f"no ledger entry found for {filename}")
    _write_ledger(new)

    file_deleted = False
    if delete_file:
        path = WORK_DIR / filename
        if path.is_file():
            try:
                path.unlink()
                file_deleted = True
            except OSError as e:
                raise HTTPException(500, f"removed from ledger but failed to delete file: {e}")
    return {"deleted_ledger": True, "deleted_file": file_deleted, "filename": filename}


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request) -> HTMLResponse:
    """Read-only view of orders.json with filter + per-row reprint/download.
    Sorted newest first; aggregate stats at the top."""
    records = _read_ledger()
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    total_grams = round(sum(r.get("total_grams", 0) for r in records), 1)
    total_seconds = sum(r.get("total_time_seconds", 0) for r in records)
    total_price = round(sum(r.get("price_cad", 0) for r in records), 2)

    # File-on-disk check so we can disable the download button for 3MFs that
    # have been pruned by retention.
    for r in records:
        try:
            r["_file_exists"] = (WORK_DIR / r.get("filename", "")).is_file()
        except Exception:
            r["_file_exists"] = False
        # Display-friendly fields
        ts = r.get("timestamp", "")
        r["_short_date"] = ts[5:10].replace("-", "/") if len(ts) >= 10 else ts
        r["_short_time"] = ts[11:16] if len(ts) >= 16 else ""

    return templates.TemplateResponse(request, "history.html", {
        "records": records,
        "total_orders": len(records),
        "total_grams": total_grams,
        "total_time_label": _fmt_time(total_seconds),
        "total_price": total_price,
    })


@app.post("/inspect-3mf")
async def inspect_3mf_endpoint(file: UploadFile = File(...)) -> dict:
    """Inspect an uploaded .3mf without committing it to the order pipeline.
    Used by the import flow to preview plate count + per-plate colours before
    the user submits.
    """
    import tempfile, os
    if not file.filename or not file.filename.lower().endswith(".3mf"):
        raise HTTPException(400, "expected a .3mf file")
    fd, tmp = tempfile.mkstemp(suffix=".3mf")
    try:
        os.close(fd)
        Path(tmp).write_bytes(await file.read())
        result = _inspect(Path(tmp))
    finally:
        try: os.unlink(tmp)
        except OSError: pass

    plates = []
    for p in result.get("plates", []):
        plates.append({
            "plate": p.get("plate"),
            "color_hex": p.get("color_hex", ""),
            "color_name": p.get("color_name", ""),
            "filaments": p.get("filaments", []),
            "time_seconds": p.get("prediction_seconds", 0),
            "time_label": _fmt_time(p.get("prediction_seconds", 0)),
            "filament_g": p.get("weight_grams", 0.0),
            "objects": [o.get("name", "") for o in p.get("objects", [])],
        })
    return {"plates": plates}


@app.post("/intake/email")
async def intake_email(eml: UploadFile = File(...)) -> JSONResponse:
    """Parse a patron .eml: extract STL attachments + body, ask Ollama to
    structure the prose. Returns form-ready field values that the index
    page's slice tab pre-fills with. The STLs land in a workdir under
    `printqueue/work/`; the client follows up with GETs to
    `/intake/email/file/<workdir>/<filename>` to re-attach them to the
    eventual /submit POST.
    """
    if not eml.filename or not eml.filename.lower().endswith(".eml"):
        raise HTTPException(400, "expected a .eml file")

    # Stash the upload in a temp file so cmd_extract can open it by path.
    import os, tempfile
    fd, tmp = tempfile.mkstemp(suffix=".eml")
    try:
        os.close(fd)
        Path(tmp).write_bytes(await eml.read())
        extracted = _run(["python3", str(SLICE_ORDER), "extract", tmp])
    finally:
        try:
            Path(tmp).unlink()
        except OSError:
            pass

    workdir = Path(extracted["workdir"])
    body = extracted.get("body", "") or ""
    stls: list[str] = extracted.get("stls", []) or []
    from_header = extracted.get("from", "") or ""

    parsed = parse_email(body, stls, from_header)

    # Workdir token = just the directory name (already unique per intake).
    # We re-validate it on the file-fetch endpoint so a malicious client
    # can't traverse out of WORK_DIR.
    workdir_token = workdir.name

    # Compute file sizes for the UI's table preview.
    files_info = []
    for s in stls:
        path = workdir / s
        files_info.append({
            "name": s,
            "size": path.stat().st_size if path.exists() else 0,
        })

    return JSONResponse({
        "workdir_token": workdir_token,
        "from": from_header,
        "subject": extracted.get("subject", ""),
        "body": body.strip(),
        "files": files_info,
        "parsed": parsed,
    })


@app.get("/intake/email/file/{workdir_token}/{filename}")
async def intake_email_file(workdir_token: str, filename: str) -> FileResponse:
    """Serve an STL file extracted by /intake/email back to the client so
    JS can re-attach it to the /submit multipart upload."""
    # Defensive: refuse anything that tries to traverse, and pin the path
    # under WORK_DIR so a crafted token can't escape.
    if "/" in workdir_token or ".." in workdir_token:
        raise HTTPException(400, "bad workdir token")
    if "/" in filename or ".." in filename or not filename.lower().endswith(".stl"):
        raise HTTPException(400, "bad filename")
    path = (WORK_DIR / workdir_token / filename).resolve()
    if not str(path).startswith(str(WORK_DIR.resolve())):
        raise HTTPException(400, "path escapes workdir")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, f"{filename} not found")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
    )


def _build_record(
    *,
    customer: str,
    card: str,
    colors: list[str],
    flow: str,
    output_3mf: Path,
    inspection: dict,
    printer: str = DEFAULT_PRINTER,
) -> dict:
    plates = inspection["plates"]
    total_time = sum(p.get("prediction_seconds", 0) for p in plates)
    total_mass = round(sum(p.get("weight_grams", 0.0) for p in plates), 1)
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "flow": flow,
        "printer": printer,
        "customer": customer,
        "card": card,
        "colors": colors,
        "output_3mf": str(output_3mf),
        "filename": output_3mf.name,
        "plate_count": len(plates),
        "total_grams": total_mass,
        "total_time_seconds": total_time,
        "total_time_label": _fmt_time(total_time),
        "price_cad": round(total_mass * PRICE_PER_GRAM, 2),
        "any_over_5h": any(p.get("prediction_seconds", 0) > 5 * 3600 for p in plates),
        "any_outside_bed": any(p.get("outside", False) for p in plates),
        "plates": [
            {
                "plate": p.get("plate"),
                "time_seconds": p.get("prediction_seconds", 0),
                "time_label": _fmt_time(p.get("prediction_seconds", 0)),
                "filament_g": p.get("weight_grams", 0.0),
                "over_5h": p.get("prediction_seconds", 0) > 5 * 3600,
                "outside": p.get("outside", False),
                "support_used": p.get("support_used", False),
                "objects": p.get("objects", []),
            }
            for p in plates
        ],
    }


def _convention_filename(customer: str, total_g: float, total_s: int, when: datetime) -> str:
    first = _sanitize(customer.strip().split()[0] if customer.strip() else "unknown")
    today = when.strftime("%b %-d")
    h, rem = divmod(int(total_s), 3600)
    mins = rem // 60
    return f"{first}_{today}_{total_g:.1f}g_{h}h{mins:02d}m.3mf"


# Reverse of _convention_filename. Used to backfill ledger rows from existing
# 3MFs whose patron info was never written to orders.json (e.g. anything
# sliced through the CLI before the web app existed).
_CONVENTION_RE = re.compile(
    r"^(?P<first>[A-Za-z][A-Za-z'-]*?)"
    r"_(?P<date>[A-Za-z]{3}\s+\d{1,2})"
    r"_(?P<mass>[\d.]+)g"
    r"_(?P<h>\d+)h(?P<m>\d+)m"
    r"(?:_\d+)?\.3mf$"
)


def _parse_convention_filename(name: str) -> dict | None:
    m = _CONVENTION_RE.match(name)
    if not m:
        return None
    return {
        "first_name": m.group("first"),
        "date_label": m.group("date"),
        "mass_g":  float(m.group("mass")),
        "time_s":  int(m.group("h")) * 3600 + int(m.group("m")) * 60,
    }


@app.post("/submit")
async def submit(
    request: Request,
    intake: str = Form(...),
    customer: str = Form(...),
    card: str = Form(...),
    # `colors` is required for slice flow (validated below) but empty for import,
    # so it has to be optional at the schema level.
    colors: str = Form(""),
    quantity: str = Form("1"),
    # Comma-separated list of per-file scales; a single value broadcasts to
    # every uploaded STL.
    scale: str = Form("1.0"),
    printer: str = Form(DEFAULT_PRINTER),
    import_metadata: str = Form(""),
    files: list[UploadFile] = File(...),
    # Programmatic clients (slice-orders-bulk skill, scripts) pass format=json
    # to skip the result.html template and get the order records directly.
    format: str = Form(""),
):
    if printer not in PRINTERS:
        raise HTTPException(400, f"unknown printer '{printer}'. Choices: {', '.join(sorted(PRINTERS))}")
    files = [f for f in files if f.filename]
    if not files:
        raise HTTPException(400, "at least one file required")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    first = customer.strip().split()[0] if customer.strip() else "unknown"
    workdir = WORK_DIR / f"{stamp}-{_sanitize(first).lower()}-web"
    workdir.mkdir(parents=True, exist_ok=True)

    # Save uploads to the workdir
    saved: list[Path] = []
    for f in files:
        base = f.filename.rsplit(".", 1)[0] if "." in f.filename else f.filename
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        safe = _sanitize(base) + (f".{ext}" if ext else "")
        dest = workdir / safe
        dest.write_bytes(await f.read())
        saved.append(dest)

    orders: list[dict] = []

    if intake == "slice":
        if not all(p.suffix.lower() == ".stl" for p in saved):
            raise HTTPException(400, "slice flow requires .stl files only")

        colors_str = (colors or "").strip()
        if not colors_str:
            raise HTTPException(400, "colours required for slice flow")
        color_list = [c.strip() for c in colors_str.split(",") if c.strip()]
        if len(color_list) == 1:
            color_list = color_list * len(saved)
        elif len(color_list) != len(saved):
            raise HTTPException(400,
                f"colour count ({len(color_list)}) must match file count ({len(saved)})")

        qty_list = [int(q.strip()) for q in quantity.split(",") if q.strip()]
        if len(qty_list) == 1 and len(saved) > 1:
            qty_list = qty_list * len(saved)
        if len(qty_list) != len(saved):
            raise HTTPException(400,
                f"quantity count ({len(qty_list)}) must match file count ({len(saved)})")

        scale_list = [float(s.strip()) for s in (scale or "1.0").split(",") if s.strip()]
        if len(scale_list) == 1:
            scale_list = scale_list * len(saved)
        if len(scale_list) != len(saved):
            raise HTTPException(400,
                f"scale count ({len(scale_list)}) must match file count ({len(saved)})")

        # Group files by (scale, colour). Each bucket gets one OrcaSlicer
        # invocation. If only one bucket: standard single-call path. If
        # multiple: slice each separately, merge resulting 3MFs into one
        # multi-plate output so the patron walks away with a single file.
        buckets: list[tuple[tuple[float, str], list[tuple[str, int]]]] = []
        bucket_idx_for: dict[tuple[float, str], int] = {}
        for i, p in enumerate(saved):
            key = (scale_list[i], color_list[i])
            if key not in bucket_idx_for:
                bucket_idx_for[key] = len(buckets)
                buckets.append((key, []))
            buckets[bucket_idx_for[key]][1].append((p.name, qty_list[i]))

        if len(buckets) == 1:
            (s, single_color), items = buckets[0]
            stl_names = [it[0] for it in items]
            qtys = [it[1] for it in items]
            result = _slice(workdir, customer, single_color, stl_names, qtys, s, printer)
            out_path = Path(result["output_3mf"])
            inspection = _inspect(out_path)
            record = _build_record(
                customer=customer, card=card, colors=color_list, flow="slice",
                output_3mf=out_path, inspection=inspection, printer=printer,
            )
            _append_to_ledger(record)
            orders.append(record)
        else:
            # Slice each bucket and remember which colour drove it, so the
            # final merged output can render per-plate colour labels.
            bucket_outputs: list[tuple[Path, str]] = []
            plate_colors_seq: list[str] = []
            for (s, bucket_color), items in buckets:
                stl_names = [it[0] for it in items]
                qtys = [it[1] for it in items]
                result = _slice(workdir, customer, bucket_color, stl_names, qtys, s, printer)
                bucket_path = Path(result["output_3mf"])
                bucket_outputs.append((bucket_path, bucket_color))
                bucket_plates = _inspect(bucket_path)["plates"]
                plate_colors_seq.extend(bucket_color for _ in bucket_plates)

            when = datetime.now()
            tmp_merged = WORK_DIR / f"_merging_{when.strftime('%H%M%S')}.3mf"
            _merge_3mfs(bucket_outputs, tmp_merged)

            merged_inspection = _inspect(tmp_merged)
            mplates = merged_inspection["plates"]
            total_time = sum(p.get("prediction_seconds", 0) for p in mplates)
            total_mass = round(sum(p.get("weight_grams", 0.0) for p in mplates), 1)

            plates_meta = [
                {
                    "plate": p.get("plate"),
                    "time_label": _fmt_time(p.get("prediction_seconds", 0)),
                    "filament_g": p.get("weight_grams", 0.0),
                    "color_name": plate_colors_seq[i] if i < len(plate_colors_seq) else "",
                }
                for i, p in enumerate(mplates)
            ]
            first_name_raw = customer.strip().split()[0] if customer.strip() else "Unknown"
            today_label = when.strftime("%b %-d")
            _make_printable(
                tmp_merged,
                customer_first=first_name_raw,
                color_name=color_list[0] if color_list else "",
                date_label=today_label,
                plates_meta=plates_meta,
                printer_model_id=PRINTERS[printer]["model_id"],
            )
            _strip_to_print_file(tmp_merged)

            final_name = _convention_filename(customer, total_mass, total_time, when)
            final_path = WORK_DIR / final_name
            n = 2
            while final_path.exists():
                stem = final_name.rsplit(".3mf", 1)[0]
                final_path = WORK_DIR / f"{stem}_{n}.3mf"
                n += 1
            tmp_merged.rename(final_path)

            # Drop the per-bucket sources — they're consolidated into final_path
            for bp, _ in bucket_outputs:
                if bp.exists() and bp != final_path:
                    try:
                        bp.unlink()
                    except OSError:
                        pass

            final_inspection = _inspect(final_path)
            record = _build_record(
                customer=customer, card=card, colors=plate_colors_seq, flow="slice",
                output_3mf=final_path, inspection=final_inspection, printer=printer,
            )
            _append_to_ledger(record)
            orders.append(record)

    elif intake == "import":
        if not all(p.suffix.lower() == ".3mf" for p in saved):
            raise HTTPException(400, "import flow requires .3mf files only")

        # import_metadata is a JSON list, one entry per uploaded file, with
        # { "filename": "...", "colors": ["White","Black"] } where the colors
        # array length matches the file's plate count.
        try:
            metadata = json.loads(import_metadata) if import_metadata else []
        except json.JSONDecodeError:
            metadata = []
        meta_by_name = {m.get("filename", ""): m for m in metadata if isinstance(m, dict)}

        when = datetime.now()
        for src_path in saved:
            # Move into work/ root, then rename to convention based on inspection.
            staging = WORK_DIR / src_path.name
            shutil.copy(src_path, staging)
            inspection = _inspect(staging)
            plates = inspection["plates"]
            total_time = sum(p.get("prediction_seconds", 0) for p in plates)
            total_mass = round(sum(p.get("weight_grams", 0.0) for p in plates), 1)

            final_name = _convention_filename(customer, total_mass, total_time, when)
            final_path = WORK_DIR / final_name
            n = 2
            while final_path.exists() and final_path != staging:
                stem = final_name.rsplit(".3mf", 1)[0]
                final_path = WORK_DIR / f"{stem}_{n}.3mf"
                n += 1
            if staging != final_path:
                staging.rename(final_path)

            file_meta = meta_by_name.get(src_path.name, {})
            colors_for_file = file_meta.get("colors") or []
            # Fall back to detected per-plate colour names if metadata is missing
            if not colors_for_file:
                colors_for_file = [p.get("color_name") or "" for p in plates]

            record = _build_record(
                customer=customer, card=card, colors=colors_for_file, flow="import",
                output_3mf=final_path, inspection=inspection,
            )
            _append_to_ledger(record)
            orders.append(record)

    else:
        raise HTTPException(400, f"unknown intake flow: {intake}")

    total_grams = round(sum(o["total_grams"] for o in orders), 1)
    total_time = sum(o["total_time_seconds"] for o in orders)
    total_price = round(sum(o["price_cad"] for o in orders), 2)

    payload = {
        "orders": orders,
        "customer": customer,
        "card": card,
        "total_grams": total_grams,
        "total_time_seconds": total_time,
        "total_time_label": _fmt_time(total_time),
        "total_price": total_price,
        "is_multi": len(orders) > 1,
    }
    if format.lower() == "json":
        return JSONResponse(payload)
    return templates.TemplateResponse(request, "result.html", payload)


@app.post("/receipt")
async def print_receipt(
    output_3mf: str = Form(...),
    customer: str = Form(...),
    card: str = Form(...),
    colors: str = Form(...),
) -> dict:
    path = Path(output_3mf)
    if not path.exists():
        raise HTTPException(404, f"3MF no longer exists: {output_3mf}")
    result = _send_receipt(path, customer, card, colors)
    return {
        "sent": True,
        "filename": path.name,
        "price_cad": result.get("price_cad"),
    }


@app.get("/download/{name}")
async def download(name: str) -> FileResponse:
    path = WORK_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, f"{name} not found")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=name,
    )


@app.get("/ledger.csv")
async def ledger_csv() -> Response:
    records = _read_ledger()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Timestamp", "Customer", "Library Card", "Flow", "Printer", "Output File",
        "Plates", "Total Grams", "Total Time", "Price (CAD)", "Colours",
    ])
    for r in records:
        w.writerow([
            r.get("timestamp", ""),
            r.get("customer", ""),
            r.get("card", ""),
            r.get("flow", ""),
            r.get("printer", ""),
            r.get("filename", ""),
            r.get("plate_count", ""),
            r.get("total_grams", ""),
            r.get("total_time_label", ""),
            r.get("price_cad", ""),
            ",".join(r.get("colors", [])),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="orders.csv"'},
    )
