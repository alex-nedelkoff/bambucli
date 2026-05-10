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
from datetime import datetime, timedelta
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
    ("predicted_grams",    "ALTER TABLE jobs ADD COLUMN predicted_grams REAL"),
    ("actual_grams",       "ALTER TABLE jobs ADD COLUMN actual_grams REAL"),
    ("last_percent",       "ALTER TABLE jobs ADD COLUMN last_percent INTEGER"),
    # Once a human has edited any field on a row, the MQTT observer
    # stops overwriting most fields so manual corrections aren't
    # clobbered on the next report frame. See record_observation.
    ("is_manually_edited", "ALTER TABLE jobs ADD COLUMN is_manually_edited INTEGER DEFAULT 0"),
    # State machine for the grams auto-fetcher in printer_dashboard.py:
    #   NULL    = couldn't resolve locally yet, but no fetch attempt scheduled
    #   pending = needs fetch from the printer's FTP storage
    #   done    = grams resolved (either locally or via FTP fetch)
    #   failed  = fetch attempted, didn't work
    #   skipped = file naming didn't give us anything to fetch
    ("grams_fetch_state",  "ALTER TABLE jobs ADD COLUMN grams_fetch_state TEXT"),
]


def _apply_migrations(c: sqlite3.Connection) -> None:
    cols = {row[1] for row in c.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, ddl in _MIGRATIONS:
        if col not in cols:
            c.execute(ddl)
    # Idempotent backfill — promote rows that look fetchable (filename
    # known, grams unresolved) into the 'pending' state. Runs every
    # startup because the WHERE clause is self-limiting: rows the loop
    # has already processed land in 'done' / 'failed' / 'skipped' and
    # never come back through this UPDATE.
    c.execute(
        "UPDATE jobs SET grams_fetch_state = 'pending' "
        "WHERE grams_fetch_state IS NULL "
        "  AND predicted_grams IS NULL "
        "  AND filename IS NOT NULL"
    )
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
    raw_tid = "" if task_id is None else str(task_id).strip()
    name = (subtask_name or "").strip() or (captured_filename or "").strip() or None
    # P1S firmware reports task_id="0" even during an active print —
    # the only stable identity is the subtask_name. When we get a
    # zero/missing task_id but a real subtask_name, we'll synthesize a
    # task_id below (inside the lock) so each print run gets its own
    # row. If both are empty/zero, the printer is genuinely idle and
    # we drop the frame.
    synthesized = not raw_tid or raw_tid == "0"
    if synthesized and not name:
        return

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
    is_running_state = (gcode_state or "").upper() in ("RUNNING", "PREPARE", "PAUSE", "PAUSED")

    with _lock:
        c = _connect()

        # Resolve a stable task_id when the printer didn't give us one.
        # We pick the most recent row matching (printer_id, subtask_name);
        # if it's still "running" we continue it, if it's terminal AND
        # we're now seeing a fresh running state, that's a re-print of
        # the same file and we mint a new id. Anything else (terminal +
        # still terminal in the report) reuses so steady-state FINISH
        # frames don't keep duplicating rows.
        if synthesized:
            existing = c.execute(
                "SELECT task_id, outcome FROM jobs "
                "WHERE printer_id = ? AND subtask_name = ? "
                "ORDER BY last_seen DESC LIMIT 1",
                (printer_id, name),
            ).fetchone()
            reuse = (
                existing is not None
                and not (
                    existing["outcome"] in ("finished", "failed", "cancelled")
                    and is_running_state
                )
            )
            if reuse:
                tid = existing["task_id"]
            else:
                # Compact, sortable, unique-enough — strip punctuation
                # so the synthetic key is safe in URLs/logs.
                stamp = now.replace(":", "").replace("-", "")
                tid = f"sub:{name}:{stamp}"
        else:
            tid = raw_tid

        cur = c.execute(
            "SELECT id, started_at, finished_at, subtask_name, filename, "
            "capture_source, matched_order_filename, prediction_seconds, "
            "total_layers, predicted_grams, actual_grams, last_percent, "
            "is_manually_edited, grams_fetch_state "
            "FROM jobs WHERE printer_id = ? AND task_id = ?",
            (printer_id, tid),
        )
        row = cur.fetchone()

        # Only set started_at the first time we see the job in a
        # running-ish state; only set finished_at the first time it
        # leaves running into a terminal state.
        is_running = is_running_state
        is_terminal = outcome in ("finished", "failed", "cancelled")

        if row is None:
            started_at = now if is_running_state else None
            finished_at = now if outcome in ("finished", "failed", "cancelled") else None
            duration = None
            if started_at and finished_at:
                duration = 0
            actual = None
            if predicted_grams and finished_at:
                # Scale by final percent for partial prints; full
                # weight for completed jobs.
                actual = round(predicted_grams * ((pct or 100) / 100.0), 1)
            # If we couldn't pull grams from orders.json or the local
            # work dir but the file does have a name, queue it for the
            # FTP-fetch loop in printer_dashboard. No name → nothing to
            # fetch, mark skipped so the loop doesn't keep scanning it.
            if predicted_grams is not None:
                fetch_state = "done"
            elif filename:
                fetch_state = "pending"
            else:
                fetch_state = "skipped"
            c.execute(
                "INSERT INTO jobs ("
                "printer_id, task_id, subtask_name, filename, gcode_state, "
                "outcome, print_error, started_at, finished_at, last_seen, "
                "duration_seconds, prediction_seconds, total_layers, "
                "capture_source, matched_order_filename, "
                "predicted_grams, actual_grams, last_percent, grams_fetch_state"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    printer_id, tid, name, filename, gcode_state,
                    outcome, int(print_error or 0), started_at, finished_at, now,
                    duration, prediction_seconds, total_layers,
                    source, matched,
                    predicted_grams, actual, pct, fetch_state,
                ),
            )
            c.commit()
            return

        # Existing row: patch the fields that legitimately change.
        # Once a human has touched this row through the /api/jobs/<id>
        # PATCH endpoint, the observer drops to a minimal-update mode:
        # only liveness fields (last_seen, gcode_state, last_percent)
        # get refreshed. Everything else — subtask_name, outcome,
        # capture_source, predicted_grams, etc. — is left alone so the
        # human's correction sticks across MQTT frames.
        manually_edited = bool(row["is_manually_edited"])

        updates: dict[str, Any] = {
            "last_seen": now,
            "gcode_state": gcode_state,
        }
        if not manually_edited:
            updates["outcome"] = outcome
            updates["print_error"] = int(print_error or 0)
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
                updates["grams_fetch_state"] = "done"
            # Promote a row from "no fetch needed yet" to "pending" once
            # we know the filename — gives the FTP fetch loop a target.
            if (not row["predicted_grams"] and not predicted_grams
                    and (filename or row["filename"])
                    and row["grams_fetch_state"] in (None, "skipped")):
                updates["grams_fetch_state"] = "pending"
        # last_percent monotonically advances during a print; latch the
        # max so a stutter back to 0 during firmware reboots doesn't
        # erase progress. Always tracked, even on manually-edited rows
        # — it's a liveness signal, not user-correctable.
        if pct is not None and (row["last_percent"] is None or pct > row["last_percent"]):
            updates["last_percent"] = pct
        if not manually_edited:
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


# Whitelist of columns the staff-facing UI is allowed to overwrite.
# Anything outside this set is rejected at the endpoint with a 400 so
# typos / malicious payloads can't update task_id, last_seen, etc.
_EDITABLE_FIELDS: dict[str, type | tuple] = {
    "subtask_name":            (str, type(None)),
    "filename":                (str, type(None)),
    "outcome":                 (str, type(None)),
    "capture_source":          (str, type(None)),
    "matched_order_filename":  (str, type(None)),
    "predicted_grams":         (float, int, type(None)),
    "actual_grams":            (float, int, type(None)),
}
_VALID_OUTCOMES = {"finished", "failed", "cancelled", "running", "unknown", None, ""}
_VALID_SOURCES  = {"local-slice", "local-import", "local-other",
                   "studio-send", "sd-card-or-other", None, ""}


def update_job(job_id: int, fields: dict) -> dict:
    """Apply staff edits to a job row. Only whitelisted columns are
    writable; bad keys raise ValueError so the UI surfaces them as 400.
    Sets is_manually_edited=1 so the MQTT observer's upsert leaves
    these fields alone going forward."""
    if not isinstance(fields, dict):
        raise ValueError("expected an object body")
    bad = set(fields) - set(_EDITABLE_FIELDS)
    if bad:
        raise ValueError(f"non-editable fields: {sorted(bad)}")
    if not fields:
        return {"ok": True, "id": job_id, "updated": []}

    clean: dict[str, Any] = {}
    for k, v in fields.items():
        # Treat empty strings as NULL so a cleared cell drops back to
        # "no value" rather than the literal "" — matches how the
        # observer represents missing data.
        if isinstance(v, str) and v.strip() == "":
            v = None
        if k in ("predicted_grams", "actual_grams"):
            if v is None:
                clean[k] = None
            else:
                try:
                    clean[k] = float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{k}: expected a number, got {v!r}")
        elif k == "outcome":
            if v not in _VALID_OUTCOMES:
                raise ValueError(f"outcome: invalid {v!r}")
            clean[k] = v
        elif k == "capture_source":
            if v not in _VALID_SOURCES:
                raise ValueError(f"capture_source: invalid {v!r}")
            clean[k] = v
        else:
            clean[k] = v.strip() if isinstance(v, str) else v

    clean["is_manually_edited"] = 1
    sets = ", ".join(f"{k} = ?" for k in clean)
    values = list(clean.values()) + [job_id]
    with _lock:
        c = _connect()
        cur = c.execute(f"UPDATE jobs SET {sets} WHERE id = ?", values)
        if cur.rowcount == 0:
            raise ValueError(f"no job with id {job_id}")
        c.commit()
    return {"ok": True, "id": job_id, "updated": [k for k in clean if k != "is_manually_edited"]}


def find_grams_fetch_candidates(*, limit: int = 5, max_age_hours: int = 24) -> list[dict]:
    """Rows the FTP-fetch loop should try next: grams unresolved, file
    name known, last seen recently enough that the file is probably
    still on the printer's storage. Older rows are skipped because
    Bambu printers rotate their internal storage and the file's likely
    gone."""
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        rows = c.execute(
            "SELECT id, printer_id, subtask_name, filename, outcome, "
            "last_percent, predicted_grams "
            "FROM jobs "
            "WHERE grams_fetch_state = 'pending' "
            "  AND filename IS NOT NULL "
            "  AND last_seen >= ? "
            "ORDER BY last_seen DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def set_grams_fetched(job_id: int, predicted_grams: float, *,
                      actual_grams: float | None = None) -> None:
    """Mark a row's grams as resolved via FTP fetch. actual_grams is
    set only when the print is already terminal (so we know the final
    percent); for in-flight rows we leave actual_grams alone — the
    next observation that flips the row to terminal will compute it
    from the now-set predicted_grams."""
    with _lock:
        c = _connect()
        if actual_grams is None:
            c.execute(
                "UPDATE jobs SET predicted_grams = ?, grams_fetch_state = 'done' "
                "WHERE id = ?",
                (predicted_grams, job_id),
            )
        else:
            c.execute(
                "UPDATE jobs SET predicted_grams = ?, actual_grams = ?, "
                "grams_fetch_state = 'done' WHERE id = ?",
                (predicted_grams, actual_grams, job_id),
            )
        c.commit()


def set_grams_fetch_state(job_id: int, state: str) -> None:
    """Move the fetch state machine forward (typically to 'failed' or
    'skipped') so the loop stops retrying."""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE jobs SET grams_fetch_state = ? WHERE id = ?",
            (state, job_id),
        )
        c.commit()


def delete_job(job_id: int) -> dict:
    """Drop a single job row. Used for cleaning up bad data (false
    starts, ghost task_ids from firmware reboots, manual entries).
    Raises ValueError if no row exists with that id."""
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        if cur.rowcount == 0:
            raise ValueError(f"no job with id {job_id}")
        c.commit()
    return {"ok": True, "id": job_id}


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
