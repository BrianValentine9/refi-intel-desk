"""Read-only data access for the dashboard.

All SQL the UI needs lives here (centralized and tested), so no raw query strings
are scattered through the Streamlit layer. These helpers only read; the app never
writes to the database.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from src.data import db

# Series the dashboard surfaces (Treasury + VA/FHA/conforming indices).
TREASURY = "DGS10"
VA_INDEX = "OBMMIVA30YF"
FHA_INDEX = "OBMMIFHA30YF"
CONFORMING_INDEX = "OBMMIC30YF"
REQUIRED_SERIES = (TREASURY, VA_INDEX, FHA_INDEX, CONFORMING_INDEX)


def db_path() -> Path:
    """Database path, overridable via REFI_DB_PATH (used for the missing-DB demo)."""
    override = os.environ.get("REFI_DB_PATH")
    return Path(override) if override else Path(db.DEFAULT_DB_PATH)


def connect():
    """Open a connection to the resolved database path."""
    return db.connect(db_path())


def database_ready(conn) -> bool:
    """True only if every required rate series has at least one real observation."""
    return all(db.latest_nonnull(conn, series) is not None for series in REQUIRED_SERIES)


def get_latest(conn, series_id: str) -> tuple[str, float] | None:
    """Most recent (date, value) for a series, skipping trailing NULLs."""
    return db.latest_nonnull(conn, series_id)


def as_of_date(conn) -> str | None:
    """Latest observation date across the required series (the desk's as-of date)."""
    dates = [db.latest_nonnull(conn, s)[0] for s in REQUIRED_SERIES if db.latest_nonnull(conn, s)]
    return max(dates) if dates else None


def get_prior(conn, series_id: str, days: int = 7) -> tuple[str, float] | None:
    """Observation ~``days`` before the latest one, business-day tolerant.

    Returns the most recent real observation on or before (latest_date - days), so a
    target landing on a weekend/holiday falls back to the prior trading day.
    """
    latest = db.latest_nonnull(conn, series_id)
    if latest is None:
        return None
    target = (date.fromisoformat(latest[0]) - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "SELECT obs_date, value FROM observations "
        "WHERE series_id = ? AND value IS NOT NULL AND obs_date <= ? "
        "ORDER BY obs_date DESC LIMIT 1",
        (series_id, target),
    )
    return cur.fetchone()


def delta_vs_prior(conn, series_id: str, days: int = 7) -> float | None:
    """Latest value minus the value ~``days`` prior, or None if unavailable."""
    latest = db.latest_nonnull(conn, series_id)
    prior = get_prior(conn, series_id, days)
    if latest is None or prior is None:
        return None
    return round(latest[1] - prior[1], 3)


def get_range(conn, series_id: str, n_days: int) -> list[tuple[str, float]]:
    """Trailing ``n_days`` of real (date, value) observations, oldest first."""
    latest = db.latest_nonnull(conn, series_id)
    if latest is None:
        return []
    cutoff = (date.fromisoformat(latest[0]) - timedelta(days=n_days)).isoformat()
    cur = conn.execute(
        "SELECT obs_date, value FROM observations "
        "WHERE series_id = ? AND value IS NOT NULL AND obs_date >= ? "
        "ORDER BY obs_date ASC",
        (series_id, cutoff),
    )
    return cur.fetchall()
