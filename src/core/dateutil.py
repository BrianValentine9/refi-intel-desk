"""Small pure date helpers for seasoning math."""

from __future__ import annotations

from datetime import date


def days_between(start: date, end: date) -> int:
    """Calendar days from start to end (negative if end precedes start)."""
    return (end - start).days


def full_months_between(start: date, end: date) -> int:
    """Number of *full* calendar months from start to end.

    e.g. Jan 15 -> Jul 15 is 6; Jan 15 -> Jul 14 is 5. Negative if end < start.
    """
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return months
