"""Single source of truth for the FRED series this project tracks.

Both the ingest pipeline and (later) the dashboard read the registry from here,
so a series is defined in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Series:
    """One FRED time series we ingest."""

    series_id: str
    friendly_name: str
    frequency: str  # "Daily" or "Weekly"
    units: str


# The VA and FHA indices are the heart of the project; verify IDs against the
# live FRED API before trusting them (see scripts/ or the ingest report).
SERIES: list[Series] = [
    Series("DGS10", "10-Year Treasury Yield", "Daily", "Percent"),
    Series("MORTGAGE30US", "Freddie Mac PMMS 30-Year Fixed Average", "Weekly", "Percent"),
    Series("OBMMIVA30YF", "Optimal Blue 30-Year VA Rate Index", "Daily", "Percent"),
    Series("OBMMIFHA30YF", "Optimal Blue 30-Year FHA Rate Index", "Daily", "Percent"),
    Series("OBMMIC30YF", "Optimal Blue 30-Year Conforming Rate Index", "Daily", "Percent"),
]


def all_series_ids() -> list[str]:
    """Return every tracked series id, in registry order."""
    return [s.series_id for s in SERIES]


def get_series(series_id: str) -> Series:
    """Look up one series by id, or raise KeyError if it isn't registered."""
    for series in SERIES:
        if series.series_id == series_id:
            return series
    raise KeyError(f"Unknown series_id: {series_id!r}")
