"""Versioned FHA mortgage-insurance schedule — domain-rules.md §2.2.10 and §7a.

FHA MIP is schedule-driven, never a single universal constant. This config is
versioned by endorsement era. Per the architect ruling (§7a), the standard era
uses a single synthetic stratum; the pre-2009-05-31 special treatment is separate.
If this ever becomes a live calculator, re-verify against the current HUD handbook.
"""

from __future__ import annotations

from datetime import date

# Loans whose prior FHA mortgage was endorsed on or before this date get the
# special reduced streamline/simple-refi treatment (domain-rules §2.2.10).
PRE_2009_ENDORSEMENT_CUTOFF = date(2009, 5, 31)

# Special reduced treatment: UFMIP 0.01%, annual MIP 0.55%.
SPECIAL_PRE_2009 = {"ufmip_rate": 0.0001, "annual_mip_rate": 0.0055}

# Standard era (endorsed after 2009-05-31). SYNTHETIC SIMPLIFICATION (§7a):
# single stratum 1.75% UFMIP / 0.55% annual MIP. 0.55% is the current HUD value
# for the common 30yr stratum (base <= $726,200, LTV > 95%, eff. 2023-03-20).
# No property-value/LTV tiering is modeled.
STANDARD = {"ufmip_rate": 0.0175, "annual_mip_rate": 0.0055}

# UFMIP refund credit for FHA-to-FHA refis within 3 years (domain-rules §2.2.10, §7a).
# SYNTHETIC SIMPLIFICATION of HUD's actual refund table: the refundable share of
# the new UFMIP declines linearly from REFUND_START_SHARE at closing to 0 at
# REFUND_MAX_MONTHS. Applied only when the prior FHA closing is within this window.
REFUND_MAX_MONTHS = 36
REFUND_START_SHARE = 0.80


def fha_mip_rates(fha_endorsement_date: date | None) -> dict:
    """Return {ufmip_rate, annual_mip_rate} for the applicable endorsement era."""
    if fha_endorsement_date is not None and fha_endorsement_date <= PRE_2009_ENDORSEMENT_CUTOFF:
        return SPECIAL_PRE_2009
    return STANDARD


def ufmip_refund_share(months_since_prior_fha_closing: int | None) -> float:
    """SYNTHETIC declining refund share; 0.0 outside the 3-year window."""
    m = months_since_prior_fha_closing
    if m is None or m < 0 or m >= REFUND_MAX_MONTHS:
        return 0.0
    return REFUND_START_SHARE * (1 - m / REFUND_MAX_MONTHS)
