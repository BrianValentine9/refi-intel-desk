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

    ``annual_rate_pct`` is a percentage (e.g. 6.5 for 6.5%). Uses Decimal for
    precision, then rounds half-up to cents. A 0% rate degrades to balance/term.
    """
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if balance < 0:
        raise ValueError("balance must be non-negative")

    principal = Decimal(str(balance))
    monthly_rate = Decimal(str(annual_rate_pct)) / Decimal(1200)
    n = int(term_months)

    if monthly_rate == 0:
        payment = principal / Decimal(n)
    else:
        growth = (1 + monthly_rate) ** n
        payment = principal * monthly_rate * growth / (growth - 1)

    return float(payment.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
