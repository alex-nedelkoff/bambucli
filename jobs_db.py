"""Persistent log of every print job the dashboard observes.

The MQTT printer dashboard already merges live state from each printer's
`device/<serial>/report` topic, but that state is in-memory only — restart
the service and prior runs vanish. This module shadows those observations
into a SQLite file so we can:

  - audit jobs that came in outside the web intake (Bambu Studio sends,
    SD-card prints started from the touchscreen, Handy)
  - reconcile against the patron-facing ledger in printqueue/orders.json
    to flag unattributed prints
  - keep a wall-clock duration history for printer utilisation stats

`record_observation()` is called from the MQTT callback thread on every
report frame; it dedupes per (printer_id, task_id) so the row count
tracks unique jobs, not message volume. Writes go through a single
connection guarded by a threading.Lock — SQLite serialises writers
internally anyway, the lock just shields paho's threaded callbacks
from each other.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "printqueue" / "jobs.db"
ORDERS_JSON = BASE_DIR / "printqueue" / "orders.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    subtask_name TEXT,
    filename TEXT,
    gcode_state TEXT,
    outcome TEXT,
    print_error INTEGER DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    last_seen TEXT NOT NULL,
    duration_seconds INTEGER,
    prediction_seconds INTEGER,
    total_layers INTEGER,
    capture_source TEXT,
    matched_order_filename TEXT,
    predicted_grams REAL,
    actual_grams REAL,
    last_percent INTEGER,
    UNIQUE(printer_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_subtask_name ON jobs(subtask_name);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_capture_source ON jobs(capture_source);
"""

# Idempotent ALTER TABLE for DBs created by an earlier version of the
# schema. SQLite has no `ADD COLUMN IF NOT EXISTS`, so we ask sqlite for
# the existing columns and only add the ones missing.
_MIGRATIONS: list[tuple[str, str]] = [
    ("predicted_grams", "ALTER TABLE jobs ADD COLUMN predicted_grams REAL"),
    ("actual_grams",    "ALTER TABLE jobs ADD COLUMN actual_grams REAL"),
    ("last_percent",    "ALTER TABLE jobs ADD COLUMN last_percent INTEGER"),
]


def _apply_migrations(c: sqlite3.Connection) -> None:
    cols = {row[1] for row in c.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, ddl in _MIGRATIONS:
        if col not in cols:
            c.execute(ddl)
    c.commit()


_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
# Cache of orders.json filenames + their flow, refreshed on a small TTL so
# the observer can derive capture_source without re-reading the file on
# every MQTT frame.
_orders_cache: dict[str, dict] = {}
_orders_cache_mtime: float = 0.0


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because paho's network thread, the
        # uvicorn worker, and FastAPI's request thread all hit the same
        # connection. The module-level _lock serialises them.
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10.0)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        _apply_migrations(_conn)
    return _conn


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _refresh_orders_cache() -> None:
    """Reload orders.json into _orders_cache when its mtime advances.
    Keeps the per-frame capture_source lookup fast — observation runs
    on the MQTT thread for every report, so we can't afford to parse
    the JSON each call."""
    global _orders_cache, _orders_cache_mtime
    try:
        mtime = ORDERS_JSON.stat().st_mtime
    except FileNotFoundError:
        _orders_cache = {}
        _orders_cache_mtime = 0.0
        return
    if mtime <= _orders_cache_mtime:
        return
    try:
        records = json.loads(ORDERS_JSON.read_text())
    except (json.JSONDecodeError, OSError):
        return
    cache: dict[str, dict] = {}
    for r in records:
        fn = r.get("filename")
        if isinstance(fn, str) and fn:
            cache[fn] = r
    _orders_cache = cache
    _orders_cache_mtime = mtime


def _derive_source_and_match(filename: str | None) -> tuple[str, str | None]:
    """Returns (capture_source, matched_order_filename).

    Sources:
      - 'local-slice' / 'local-import' — matches an orders.json record
      - 'studio-send' — caller hints (subtask_name caught via the
        request-topic eavesdrop), but no orders.json match
      - 'sd-card-or-other' — neither
    The matched filename mirrors the orders.json `filename` so the UI
    can deeplink without re-running the lookup."""
    if not filename:
        return ("sd-card-or-other", None)
    _refresh_orders_cache()
    rec = _orders_cache.get(filename)
    if rec:
        flow = (rec.get("flow") or "").lower()
        if flow == "slice":
            return ("local-slice", filename)
        if flow == "import":
            return ("local-import", filename)
        return ("local-other", filename)
    return ("sd-card-or-other", None)


_WORK_DIR = BASE_DIR / "printqueue" / "work"
# Per-filename grams cache. Inspecting a 3MF reads the zip from disk;
# we don't want to do it on every MQTT frame, so the first lookup
# memoises the answer (None when unresolvable).
_grams_cache: dict[str, float | None] = {}


def _grams_for_file(filename: str | None) -> float | None:
    """Best-effort filament-grams prediction for a job. First the
    orders.json record (already cached) — that's the slicer's prediction
    summed across plates from when the order was processed. Falls back
    to inspecting the 3MF on disk if it's still in printqueue/work/, so
    untracked / SD-card prints can still get a grams figure as long as
    the source file is around. Returns None when nothing resolves."""
    if not filename:
        return None
    if filename in _grams_cache:
        return _grams_cache[filename]
    _refresh_orders_cache()
    rec = _orders_cache.get(filename)
    if rec and rec.get("total_grams") is not None:
        _grams_cache[filename] = float(rec["total_grams"])
        return _grams_cache[filename]
    # Fallback: inspect the 3MF on disk. Imported here to avoid a top-
    # level circular import — slice_order doesn't import jobs_db, but
    # both share BASE_DIR.
    path = _WORK_DIR / filename
    if not path.exists():
        _grams_cache[filename] = None
        return None
    try:
        from slice_order import inspect_3mf  # noqa: PLC0415
        ins = inspect_3mf(path)
        total = round(sum(p.get("weight_grams", 0.0) for p in ins.get("plates", [])), 1)
        _grams_cache[filename] = float(total) if total else None
    except Exception:
        _grams_cache[filename] = None
    return _grams_cache[filename]


def _classify_outcome(gcode_state: str | None, print_error: int | None) -> str:
    s = (gcode_state or "").upper()
    if print_error and int(print_error) != 0:
        return "failed"
    if s in ("FINISH", "FINISHED", "SUCCESS"):
        return "finished"
    if s in ("FAILED", "FAILED_FINISH"):
        return "failed"
    if s in ("CANCEL", "CANCELLED"):
        return "cancelled"
    if s in ("RUNNING", "PREPARE", "PAUSE", "PAUSED"):
        return "running"
    return "unknown"


def record_observation(
    printer_id: str,
    *,
    task_id: str | int | None,
    subtask_name: str | None,
    gcode_state: str | None,
    print_error: int | None = 0,
    prediction_seconds: int | None = None,
    total_layers: int | None = None,
    captured_filename: str | None = None,
    mc_percent: int | None = None,
) -> None:
    """Upsert a row for this (printer_id, task_id) observation.

    Idempotent under repeated MQTT frames — the dashboard fires this on
    every report, and most reports carry no material change. We always
    bump last_seen but only write started_at / finished_at on the
    relevant transitions, so the row records the true wall-clock span
    of the print rather than whatever frame happened to arrive last.

    `task_id == 0` (or missing/empty) means the printer is idle with no
    active job — those frames are dropped at the entry so the table
    only contains real jobs. `captured_filename` is consulted as a
    fallback when the report's own subtask_name is empty, which is the
    common case on X1C: the firmware drops subtask_name from steady-
    state reports, but the dashboard's /request eavesdrop already
    captured the name from the original project_file command.
    """
    if task_id is None:
        return
    tid = str(task_id).strip()
    if not tid or tid == "0":
        return

    name = (subtask_name or "").strip() or (captured_filename or "").strip() or None
    filename = f"{name}.3mf" if name and not name.endswith(".3mf") else name
    source, matched = _derive_source_and_match(filename)
    outcome = _classify_outcome(gcode_state, print_error)
    predicted_grams = _grams_for_file(filename)
    # Coerce mc_percent to a clean 0-100 int. Bambu sometimes sends it
    # as a string; -1 or > 100 has been seen during firmware boot.
    pct: int | None = None
    if mc_percent is not None:
        try:
            v = int(mc_percent)
            if 0 <= v <= 100:
                pct = v
        except (TypeError, ValueError):
            pass
    now = _now_iso()

    with _lock:
        c = _connect()
        cur = c.execute(
            "SELECT id, started_at, finished_at, subtask_name, capture_source, "
            "matched_order_filename, prediction_seconds, total_layers, "
            "predicted_grams, actual_grams, last_percent "
            "FROM jobs WHERE printer_id = ? AND task_id = ?",
            (printer_id, tid),
        )
        row = cur.fetchone()

        # Only set started_at the first time we see the job in a
        # running-ish state; only set finished_at the first time it
        # leaves running into a terminal state.
        is_running = (gcode_state or "").upper() in ("RUNNING", "PREPARE", "PAUSE", "PAUSED")
        is_terminal = outcome in ("finished", "failed", "cancelled")

        if row is None:
            started_at = now if is_running else None
            finished_at = now if is_terminal else None
            duration = None
            if started_at and finished_at:
                duration = 0
            actual = None
            if predicted_grams and is_terminal:
                # Scale by final percent for partial prints; full
                # weight for completed jobs.
                actual = round(predicted_grams * ((pct or 100) / 100.0), 1)
            c.execute(
                "INSERT INTO jobs ("
                "printer_id, task_id, subtask_name, filename, gcode_state, "
                "outcome, print_error, started_at, finished_at, last_seen, "
                "duration_seconds, prediction_seconds, total_layers, "
                "capture_source, matched_order_filename, "
                "predicted_grams, actual_grams, last_percent"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    printer_id, tid, name, filename, gcode_state,
                    outcome, int(print_error or 0), started_at, finished_at, now,
                    duration, prediction_seconds, total_layers,
                    source, matched,
                    predicted_grams, actual, pct,
                ),
            )
            c.commit()
            return

        # Existing row: patch the fields that legitimately change.
        updates: dict[str, Any] = {
            "last_seen": now,
            "gcode_state": gcode_state,
            "outcome": outcome,
            "print_error": int(print_error or 0),
        }
        if name and not row["subtask_name"]:
            updates["subtask_name"] = name
            updates["filename"] = filename
        # Backfill source/match if we couldn't resolve them on first
        # insert (e.g. orders.json hadn't been written yet).
        if not row["matched_order_filename"] and matched:
            updates["matched_order_filename"] = matched
            updates["capture_source"] = source
        elif not row["capture_source"] and source:
            updates["capture_source"] = source
        if prediction_seconds and not row["prediction_seconds"]:
            updates["prediction_seconds"] = prediction_seconds
        if total_layers and not row["total_layers"]:
            updates["total_layers"] = total_layers
        if predicted_grams and not row["predicted_grams"]:
            updates["predicted_grams"] = predicted_grams
        # last_percent monotonically advances during a print; latch the
        # max so a stutter back to 0 during firmware reboots doesn't
        # erase progress.
        if pct is not None and (row["last_percent"] is None or pct > row["last_percent"]):
            updates["last_percent"] = pct
        if is_running and not row["started_at"]:
            updates["started_at"] = now
        if is_terminal and not row["finished_at"]:
            updates["finished_at"] = now
            # Compute wall-clock duration when both ends are now known.
            start_iso = updates.get("started_at") or row["started_at"]
            if start_iso:
                try:
                    s = datetime.fromisoformat(start_iso)
                    f = datetime.fromisoformat(now)
                    updates["duration_seconds"] = max(0, int((f - s).total_seconds()))
                except ValueError:
                    pass
            # Compute actual_grams once at terminal transition. Scale
            # the prediction by the latched final percent — a cancelled
            # half-finished print still reports a meaningful figure.
            pred = updates.get("predicted_grams") or row["predicted_grams"]
            final_pct = pct if pct is not None else row["last_percent"]
            if pred:
                if outcome == "finished":
                    updates["actual_grams"] = round(float(pred), 1)
                elif final_pct is not None:
                    updates["actual_grams"] = round(float(pred) * (final_pct / 100.0), 1)

        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [row["id"]]
        c.execute(f"UPDATE jobs SET {sets} WHERE id = ?", values)
        c.commit()


def list_jobs(
    *,
    limit: int = 100,
    printer_id: str | None = None,
    capture_source: str | None = None,
) -> list[dict]:
    """Return jobs ordered by last_seen DESC. Used by both the API and
    the /jobs HTML page. Returns plain dicts so the result survives
    the request-thread / MQTT-thread boundary intact."""
    where = []
    params: list[Any] = []
    if printer_id:
        where.append("printer_id = ?")
        params.append(printer_id)
    if capture_source:
        where.append("capture_source = ?")
        params.append(capture_source)
    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_seen DESC LIMIT ?"
    params.append(limit)
    with _lock:
        c = _connect()
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    """Aggregate counts the /jobs page header uses. Reads in one
    transaction so the totals are coherent."""
    with _lock:
        c = _connect()
        total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_source = {
            r[0]: r[1] for r in c.execute(
                "SELECT capture_source, COUNT(*) FROM jobs GROUP BY capture_source"
            ).fetchall()
        }
        by_outcome = {
            r[0]: r[1] for r in c.execute(
                "SELECT outcome, COUNT(*) FROM jobs GROUP BY outcome"
            ).fetchall()
        }
        unmatched = c.execute(
            "SELECT COUNT(*) FROM jobs WHERE matched_order_filename IS NULL"
        ).fetchone()[0]
        # COALESCE the actual/predicted columns: a finished job always
        # has actual_grams; an in-flight one only has predicted, so this
        # gives a "total filament committed" figure without zeroing out
        # in-flight prints.
        grams_actual = c.execute(
            "SELECT COALESCE(SUM(actual_grams), 0) FROM jobs"
        ).fetchone()[0]
        grams_committed = c.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_grams, predicted_grams)), 0) FROM jobs"
        ).fetchone()[0]
    return {
        "total": total,
        "by_source": by_source,
        "by_outcome": by_outcome,
        "unmatched": unmatched,
        "grams_actual": round(float(grams_actual or 0), 1),
        "grams_committed": round(float(grams_committed or 0), 1),
    }
