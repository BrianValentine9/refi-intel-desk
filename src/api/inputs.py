"""Allowlist parsers for API query inputs.

Query params arrive as strings. Each parser accepts an int or a plain base-10 integer
string and raises :class:`InputError` (a ValueError) with a clear message otherwise.
Floats such as "100.5", bools, negatives and blanks are rejected.
"""

from __future__ import annotations

import re

COST_BP_CHOICES = tuple(range(50, 151, 10))  # 0.50% .. 1.50% in 0.10% steps
THRESHOLD_RANGE = (12, 120)
RUNG_RANGE = (0, 16)
DAYS_CHOICES = (90, 180, 365)

_INT = re.compile(r"[0-9]+")


class InputError(ValueError):
    """A query input failed validation."""


def _to_int(name: str, value: object) -> int:
    if isinstance(value, bool):
        raise InputError(f"{name} must be an integer, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INT.fullmatch(value.strip()):
        return int(value.strip())
    raise InputError(f"{name} must be a whole number, got {value!r}")


def parse_cost_bp(value: object) -> int:
    """Recoupment cost in basis points: 50, 60, ... 150."""
    bp = _to_int("cost_bp", value)
    if bp not in COST_BP_CHOICES:
        raise InputError("cost_bp must be one of 50, 60, ..., 150")
    return bp


def cost_pct_from_bp(bp: int) -> float:
    """Basis points to a fraction, rounded to 4 places (70 -> 0.007 exactly)."""
    return round(bp / 10000, 4)


def parse_threshold(value: object) -> int:
    """Break-even threshold in months: 12..120."""
    n = _to_int("threshold", value)
    lo, hi = THRESHOLD_RANGE
    if not lo <= n <= hi:
        raise InputError(f"threshold must be between {lo} and {hi}")
    return n


def parse_rung(value: object) -> int:
    """Ladder rung index: 0..16."""
    n = _to_int("rung", value)
    lo, hi = RUNG_RANGE
    if not lo <= n <= hi:
        raise InputError(f"rung must be between {lo} and {hi}")
    return n


def parse_days(value: object) -> int:
    """Series window in days: 90, 180 or 365."""
    n = _to_int("days", value)
    if n not in DAYS_CHOICES:
        raise InputError("days must be one of 90, 180, 365")
    return n
