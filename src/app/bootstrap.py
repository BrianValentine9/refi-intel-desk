"""Cloud/local boot helpers - secrets bridge, first-run database ensure, and a
staleness refresh.

Streamlit Community Cloud does not ship a populated SQLite file (local DBs are
gitignored). This module copies the committed seed when present, then keeps the
data current: once a ready database exists, if its latest observation is more than
a few days old and a FRED key is available, it runs a small incremental ingest.
The refresh fails open - any error leaves the existing data in place and the
"As of" caption stays truthful.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import date
from pathlib import Path

from src.app import data_access as da
from src.data import db, ingest
from src.data.series_registry import all_series_ids

SEED_DB_PATH = Path("data") / "seed.db"
SECRET_KEYS = ("FRED_API_KEY", "ANTHROPIC_API_KEY")

# Refresh policy. Rates are business-daily, so a gap of up to 3 calendar days
# (a Friday observation still being latest on Monday) is normal, not stale.
STALE_AFTER_DAYS = 3
# At most one refresh attempt per container per few hours, so Streamlit reruns do
# not stampede FRED. Module-level state resets on container restart, which is fine.
REFRESH_MIN_INTERVAL_SEC = 3 * 60 * 60
_last_refresh_attempt: float | None = None
# Guards the throttle check-and-set so concurrent sessions/threads start one refresh.
_refresh_lock = threading.Lock()


def apply_streamlit_secrets() -> None:
    """Copy Streamlit secrets into ``os.environ`` when env vars are unset."""
    try:
        import streamlit as st

        for key in SECRET_KEYS:
            if key in st.secrets and not os.environ.get(key):
                os.environ[key] = str(st.secrets[key]).strip()
    except Exception:
        # Local CLI / missing secrets.toml - dotenv already covers that path.
        return


def secrets_from_env() -> dict[str, bool]:
    """Report which secret keys are present in ``os.environ`` (never the values).

    Env-only and Streamlit-free, for the API process; the dashboard keeps
    ``apply_streamlit_secrets``.
    """
    return {key: bool(os.environ.get(key)) for key in SECRET_KEYS}


def _copy_seed_if_needed(path: Path) -> bool:
    """Copy committed seed.db into the working DB path. Returns True if copied."""
    if not SEED_DB_PATH.is_file():
        return False
    if path.resolve() == SEED_DB_PATH.resolve():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic and WAL-safe: stage next to the target, drop stale sidecars so they cannot
    # be replayed onto the fresh file, swap in, then switch the copy to WAL.
    tmp = path.with_name(path.name + ".tmp")
    try:
        shutil.copy2(SEED_DB_PATH, tmp)
        # -wal first: it holds committed data, so if another connection has it open
        # (Windows WinError 32) we stop before deleting anything else.
        for suffix in ("-wal", "-journal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        os.replace(tmp, path)
        db.init_working_db(path)
    except OSError:
        # A live connection holds the working DB. Leave it as it was and let the
        # caller fall through to its not-ready path instead of crashing.
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def _is_stale(as_of: str | None) -> bool:
    """True when the latest observation is more than STALE_AFTER_DAYS old."""
    if not as_of:
        return False
    try:
        observed = date.fromisoformat(as_of[:10])
    except ValueError:
        return False
    return (date.today() - observed).days > STALE_AFTER_DAYS


def _refresh_throttled() -> bool:
    """True when a refresh was attempted within REFRESH_MIN_INTERVAL_SEC."""
    if _last_refresh_attempt is None:
        return False
    return (time.monotonic() - _last_refresh_attempt) < REFRESH_MIN_INTERVAL_SEC


def _maybe_refresh_stale(path: Path, as_of: str | None) -> None:
    """Incremental FRED pull when the working DB is stale, keyed, and not throttled.

    Fails open: the throttle marker is set before the pull, and any ingest error is
    swallowed so the app keeps serving the existing data unchanged.
    """
    global _last_refresh_attempt
    if not _is_stale(as_of):
        return
    if not os.environ.get("FRED_API_KEY"):
        return
    with _refresh_lock:
        if _refresh_throttled():
            return
        _last_refresh_attempt = time.monotonic()
    try:
        # Incremental: each series resumes from its latest stored date, so the pull is small.
        ingest.run(all_series_ids(), db_path=path)
    except Exception:
        # Fail open: serve the existing data unchanged.
        return


def _ready_as_of() -> tuple[bool, str | None]:
    """Open the working DB and report ``(database_ready, as_of_date)``."""
    conn = da.connect()
    try:
        return da.database_ready(conn), da.as_of_date(conn)
    finally:
        conn.close()


def ensure_database(*, backfill_years: int = 5) -> tuple[bool, str | None]:
    """Make sure the working DB has the required series and is reasonably current.

    Order: use existing DB, else copy seed, else ingest from FRED if keyed. Once a
    ready DB exists, a stale-and-keyed database gets a small incremental refresh.
    Returns ``(ready, as_of)``.
    """
    path = da.db_path()

    ready, as_of = _ready_as_of()
    if not ready:
        _copy_seed_if_needed(path)
        ready, as_of = _ready_as_of()

    if not ready:
        # No existing DB and no usable seed: first-run ingest only if keyed.
        if not os.environ.get("FRED_API_KEY"):
            return False, None
        ingest.run(all_series_ids(), backfill_years=backfill_years, db_path=path)
        return _ready_as_of()

    # Ready DB in hand. Refresh it if the data has gone stale (fail open).
    _maybe_refresh_stale(path, as_of)
    return _ready_as_of()
