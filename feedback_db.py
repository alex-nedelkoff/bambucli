"""SQLite-backed feedback / suggestions log for the pilot.

Separate database from jobs.db because feedback isn't joined to any
print-job state; isolating it keeps the jobs schema migrations focused
and makes the feedback table easy to read / back up on its own. Same
singleton-connection + check_same_thread=False pattern as jobs_db so
the FastAPI request thread doesn't pay reconnect cost on every hit.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "printqueue" / "feedback.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    author      TEXT,
    body        TEXT    NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    resolved_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_sort
    ON feedback(resolved, created_at DESC);
"""

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10.0)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
    return _conn


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["resolved"] = bool(d.get("resolved"))
    return d


def add_entry(*, author: str | None, body: str) -> dict:
    """Insert a new feedback entry. Body is required; author is whatever
    the staff picker had selected (None if no name was set)."""
    clean_body = (body or "").strip()
    if not clean_body:
        raise ValueError("body cannot be empty")
    clean_author = (author or "").strip() or None
    with _lock:
        c = _connect()
        cur = c.execute(
            "INSERT INTO feedback (created_at, author, body) VALUES (?, ?, ?)",
            (_now_iso(), clean_author, clean_body),
        )
        c.commit()
        new_id = cur.lastrowid
    entry = get_entry(new_id)
    assert entry is not None
    return entry


def list_entries() -> list[dict]:
    """All entries. Open ones first (newest open at top), then resolved
    ones (newest resolved at top of the resolved block). Secondary sort
    on id DESC so entries posted in the same second still order
    intuitively (newest first)."""
    with _lock:
        c = _connect()
        rows = c.execute(
            """SELECT * FROM feedback
               ORDER BY resolved ASC, created_at DESC, id DESC"""
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_entry(entry_id: int) -> dict | None:
    with _lock:
        c = _connect()
        row = c.execute(
            "SELECT * FROM feedback WHERE id = ?", (entry_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def set_resolved(entry_id: int, *, resolved: bool,
                 resolved_by: str | None) -> dict | None:
    """Toggle the resolved flag. resolved_by is the staff name picked
    when the toggle was clicked; cleared on un-resolve."""
    with _lock:
        c = _connect()
        if resolved:
            c.execute(
                """UPDATE feedback
                   SET resolved = 1,
                       resolved_at = ?,
                       resolved_by = ?
                   WHERE id = ?""",
                (_now_iso(), (resolved_by or "").strip() or None, entry_id),
            )
        else:
            c.execute(
                """UPDATE feedback
                   SET resolved = 0,
                       resolved_at = NULL,
                       resolved_by = NULL
                   WHERE id = ?""",
                (entry_id,),
            )
        c.commit()
    return get_entry(entry_id)


def delete_entry(entry_id: int) -> bool:
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM feedback WHERE id = ?", (entry_id,))
        c.commit()
        return cur.rowcount > 0
