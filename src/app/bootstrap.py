"""Cloud/local boot helpers — secrets bridge and first-run database ensure.

Streamlit Community Cloud does not ship a populated SQLite file (local DBs are
gitignored). This module copies the committed seed when present, then optionally
refreshes from FRED when a key is available via env or ``st.secrets``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from src.app import data_access as da
from src.data import ingest
from src.data.series_registry import all_series_ids

SEED_DB_PATH = Path("data") / "seed.db"
SECRET_KEYS = ("FRED_API_KEY", "ANTHROPIC_API_KEY")


def apply_streamlit_secrets() -> None:
    """Copy Streamlit secrets into ``os.environ`` when env vars are unset."""
    try:
        import streamlit as st

        for key in SECRET_KEYS:
            if key in st.secrets and not os.environ.get(key):
                os.environ[key] = str(st.secrets[key]).strip()
    except Exception:
        # Local CLI / missing secrets.toml — dotenv already covers that path.
        return


def _copy_seed_if_needed(path: Path) -> bool:
    """Copy committed seed.db into the working DB path. Returns True if copied."""
    if not SEED_DB_PATH.is_file():
        return False
    if path.resolve() == SEED_DB_PATH.resolve():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SEED_DB_PATH, path)
    return True


def ensure_database(*, backfill_years: int = 5) -> tuple[bool, str | None]:
    """Make sure the working DB has the required series.

    Order: use existing DB → copy seed → ingest from FRED if keyed.
    Returns ``(ready, as_of)``.
    """
    path = da.db_path()
    conn = da.connect()
    try:
        if da.database_ready(conn):
            return True, da.as_of_date(conn)
    finally:
        conn.close()

    _copy_seed_if_needed(path)
    conn = da.connect()
    try:
        if da.database_ready(conn):
            return True, da.as_of_date(conn)
    finally:
        conn.close()

    if not os.environ.get("FRED_API_KEY"):
        return False, None

    ingest.run(all_series_ids(), backfill_years=backfill_years, db_path=path)
    conn = da.connect()
    try:
        return da.database_ready(conn), da.as_of_date(conn)
    finally:
        conn.close()
