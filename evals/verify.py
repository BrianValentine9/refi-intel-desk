"""Eval harness — every quoted figure in the brief must match the snapshot."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.brief.snapshot import BriefSnapshot, as_of_mentioned

_PCT = re.compile(r"(?<!\d)(-?\d+\.\d{1,3})%")
_INT = re.compile(r"\b(\d{1,3}(?:,\d{3})*|\d+)\b")
# A decimal written as a points value: "0.280 points", "-0.28 point", "0.031 percentage points", "0.031 pts".
_POINTS = re.compile(r"(?<![\d.])[+-]?(\d+\.\d+)\s*(?:percentage\s+points?|points?|pts)\b", re.IGNORECASE)
_MONTHS = re.compile(r"(\d+\.\d)\s+months", re.IGNORECASE)


@dataclass
class EvalResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    percentages_checked: int = 0
    counts_checked: int = 0
    points_checked: int = 0

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        parts = [status, f"pct={self.percentages_checked}", f"counts={self.counts_checked}", f"points={self.points_checked}"]
        if self.errors:
            parts.append("errors=" + "; ".join(self.errors))
        return " | ".join(parts)


def _parse_ints(text: str) -> list[int]:
    out: list[int] = []
    for match in _INT.finditer(text):
        raw = match.group(1).replace(",", "")
        if len(raw) > 6:
            continue
        out.append(int(raw))
    return out


def verify_brief(text: str, snapshot: BriefSnapshot) -> EvalResult:
    """Check brief numbers against snapshot; flag unsupported percentages and counts."""
    result = EvalResult(passed=True)
    allowed_pct = snapshot.allowed_percentages()
    allowed_counts = snapshot.allowed_counts()
    allowed_medians = snapshot.allowed_medians()

    for match in _PCT.finditer(text):
        value = round(float(match.group(1)), 3)
        result.percentages_checked += 1
        if value not in allowed_pct:
            result.passed = False
            result.errors.append(f"unsupported rate/delta {value:.3f}%")

    for match in _MONTHS.finditer(text):
        value = round(float(match.group(1)), 1)
        result.percentages_checked += 1
        if value not in allowed_medians:
            result.passed = False
            result.errors.append(f"unsupported median {value:.1f} months")

    allowed_points = snapshot.allowed_points()
    for match in _POINTS.finditer(text):
        value = round(float(match.group(1)), 3)
        result.points_checked += 1
        if value not in allowed_points:
            result.passed = False
            result.errors.append(f"unsupported points value {value:.3f}")

    # Strip the percentage and points tokens checked above before the integer scan
    # (avoids 250 from 4.250% and 280 from 0.280 points). Every other decimal stays in the text.
    stripped = _PCT.sub(" ", text)
    stripped = _POINTS.sub(" ", stripped)
    for match in _MONTHS.finditer(stripped):
        stripped = stripped.replace(match.group(0), " ")

    for value in _parse_ints(stripped):
        if value in allowed_counts:
            result.counts_checked += 1
            continue
        if value in {snapshot.threshold_months, snapshot.seed}:
            continue
        if 1900 <= value <= 2100:
            continue
        if value < 100:
            continue
        result.passed = False
        result.errors.append(f"unsupported count {value:,}")

    if not as_of_mentioned(text, snapshot.as_of):
        result.warnings.append(f"as-of date {snapshot.as_of} not mentioned")

    if "synthetic" not in text.lower() and "modeled" not in text.lower():
        result.warnings.append("disclaimer language (synthetic/modeled) not found")

    return result
