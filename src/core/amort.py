"""Loan amortization math. Pure functions, no I/O.

Money is rounded half-up to the cent (banker's rounding would bias payment math),
and the rounding behavior is asserted in tests.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def round_half_up(amount: float, places: str = "0.01") -> float:
    """Round half-up to the given decimal places (default cents)."""
    return float(Decimal(str(amount)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def monthly_pi(balance: float, annual_rate_pct: float, term_months: int) -> float:
    """Standard amortized monthly principal + interest payment.

    ``annual_rate_pct`` is a percentage (e.g. 6.5 for 6.5%). Computed in float for
    speed (the ladder prices tens of thousands of scenarios), then rounded half-up
    to cents so the reported payment is exact. A 0% rate degrades to balance/term.
    """
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if balance < 0:
        raise ValueError("balance must be non-negative")

    n = int(term_months)
    monthly_rate = annual_rate_pct / 1200.0

    if monthly_rate == 0:
        payment = balance / n
    else:
        growth = (1 + monthly_rate) ** n
        payment = balance * monthly_rate * growth / (growth - 1)

    return round_half_up(payment)
