"""SQLite persistence for FRED observations.

Stores raw observations idempotently (re-running ingest never duplicates rows)
and keeps a small audit trail of ingest runs.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .fred_client import Observation

DEFAULT_DB_PATH = Path("data") / "refi.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    series_id TEXT NOT NULL,
    obs_date  TEXT NOT NULL,   -- ISO yyyy-mm-dd
    value     REAL,            -- NULL for missing
    PRIMARY KEY (series_id, obs_date)
);
CREATE TABLE IF NOT EXISTS ingest_log (
    series_id     TEXT NOT NULL,
    run_at        TEXT NOT NULL,  -- ISO timestamp
    rows_upserted INTEGER NOT NULL
);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the database and ensure the schema exists."""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def upsert_observations(conn: sqlite3.Connection, observations: Iterable[Observation]) -> int:
    """Insert or replace observations by (series_id, obs_date). Returns row count.

    Uses INSERT OR REPLACE on the primary key, so ingesting the same data twice
    is a no-op on row count — idempotency is guaranteed at the storage layer.
    """
    rows = [(o.series_id, o.obs_date, o.value) for o in observations]
    conn.executemany(
        "INSERT OR REPLACE INTO observations (series_id, obs_date, value) "
        "VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def latest_date(conn: sqlite3.Connection, series_id: str) -> str | None:
    """Most recent obs_date stored for a series, or None if we have none.

    Used as the start point for incremental pulls.
    """
    cur = conn.execute(
        "SELECT MAX(obs_date) FROM observations WHERE series_id = ?", (series_id,)
    )
    return cur.fetchone()[0]


def latest_nonnull(conn: sqlite3.Connection, series_id: str) -> tuple[str, float] | None:
    """Most recent (date, value) where value is not NULL, or None.

    The newest dated row can carry a NULL (value not published yet), so the
    summary reports the latest *real* reading.
    """
    cur = conn.execute(
        "SELECT obs_date, value FROM observations "
        "WHERE series_id = ? AND value IS NOT NULL "
        "ORDER BY obs_date DESC LIMIT 1",
        (series_id,),
    )
    return cur.fetchone()


def count_observations(conn: sqlite3.Connection, series_id: str | None = None) -> int:
    """Total stored observations, optionally filtered to one series."""
    if series_id is None:
        cur = conn.execute("SELECT COUNT(*) FROM observations")
    else:
        cur = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE series_id = ?", (series_id,)
        )
    return cur.fetchone()[0]


def log_ingest(conn: sqlite3.Connection, series_id: str, rows_upserted: int) -> None:
    """Record one ingest run in the audit log."""
    conn.execute(
        "INSERT INTO ingest_log (series_id, run_at, rows_upserted) VALUES (?, ?, ?)",
        (series_id, datetime.now(timezone.utc).isoformat(), rows_upserted),
    )
    conn.commit()
