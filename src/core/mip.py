"""FHA mortgage-insurance computation — domain-rules.md §2.2.10.

Thin wrappers over the versioned schedule in config/mip_schedule.py. VA loans
carry no MIP. Pure functions, no I/O.
"""

from __future__ import annotations

from datetime import date

from .amort import round_half_up
from .config import mip_schedule


def fha_mip_rates(fha_endorsement_date: date | None) -> dict:
    """{ufmip_rate, annual_mip_rate} for the loan's endorsement era."""
    return mip_schedule.fha_mip_rates(fha_endorsement_date)


def monthly_mip(balance: float, annual_mip_rate: float) -> float:
    """Monthly MIP = balance * annual MIP rate / 12, rounded to cents.

    A synthetic simplification: real FHA bases annual MIP on the average annual
    balance; using the current balance is close enough for this model.
    """
    return round_half_up(balance * annual_mip_rate / 12)


def ufmip_amount(
    base_loan_amount: float,
    ufmip_rate: float,
    months_since_prior_fha_closing: int | None = None,
) -> float:
    """Net upfront MIP after any FHA-to-FHA refund credit (domain-rules §2.2.10).

    The refund share (synthetic, §7a) reduces the gross UFMIP when the prior FHA
    loan closed within the 3-year window.
    """
    gross = base_loan_amount * ufmip_rate
    refund_share = mip_schedule.ufmip_refund_share(months_since_prior_fha_closing)
    return round_half_up(gross * (1 - refund_share))
