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


# The committed seed is read-only by contract: no pragma, no WAL, no write ever.
_SEED_PATH = (Path(__file__).resolve().parents[2] / "data" / "seed.db").resolve()
BUSY_TIMEOUT_MS = 5000


def connect(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    readonly: bool = False,
    ensure_schema: bool = True,
    timeout: float | None = None,
) -> sqlite3.Connection:
    """Open the database; by default create it if needed and ensure the schema exists.

    ``readonly=True`` opens a ``mode=ro`` URI: no directory creation, no pragma, no
    schema statement, and writes raise ``sqlite3.OperationalError``. Use it for the
    committed seed. ``ensure_schema=False`` skips the CREATE script (for a DB that
    ``init_working_db`` already prepared). ``timeout`` is the sqlite busy wait in seconds.
    """
    path = Path(db_path)
    kwargs = {} if timeout is None else {"timeout": timeout}
    if readonly:
        return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, **kwargs)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, **kwargs)
    if ensure_schema:
        conn.executescript(_SCHEMA)
    return conn


def init_working_db(db_path: str | Path) -> None:
    """Create the schema once and switch the working DB to WAL journaling.

    For the working DB only: raises ValueError if the path is the committed seed
    (``data/seed.db``), which must stay in rollback-journal mode and byte-identical.
    """
    path = Path(db_path)
    resolved = path.resolve()
    if resolved == _SEED_PATH or resolved == (Path("data") / "seed.db").resolve():
        raise ValueError("init_working_db must never be called on the committed seed")
    conn = connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()


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
