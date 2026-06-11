"""FHA MIP schedule tests — pre-2009 special, standard era, UFMIP refund credit."""

from __future__ import annotations

from datetime import date

from src.core import mip
from src.core.config import mip_schedule


def test_pre_2009_endorsement_gets_special_treatment():
    rates = mip.fha_mip_rates(date(2009, 5, 31))  # on the cutoff
    assert rates == {"ufmip_rate": 0.0001, "annual_mip_rate": 0.0055}


def test_day_after_cutoff_gets_standard_treatment():
    rates = mip.fha_mip_rates(date(2009, 6, 1))
    assert rates["ufmip_rate"] == 0.0175
    assert rates["annual_mip_rate"] == 0.0055


def test_missing_endorsement_date_is_standard():
    assert mip.fha_mip_rates(None)["ufmip_rate"] == 0.0175


def test_monthly_mip_is_balance_times_rate_over_12():
    assert mip.monthly_mip(300_000, 0.0055) == 137.50


def test_ufmip_refund_applies_within_3_years():
    gross = round(300_000 * 0.0175, 2)
    net_recent = mip.ufmip_amount(300_000, 0.0175, months_since_prior_fha_closing=0)
    assert net_recent < gross  # refund credit reduces the new UFMIP


def test_ufmip_refund_not_applied_after_3_years():
    gross = round(300_000 * 0.0175, 2)
    net_old = mip.ufmip_amount(300_000, 0.0175, months_since_prior_fha_closing=40)
    assert net_old == gross


def test_refund_share_boundaries():
    assert mip_schedule.ufmip_refund_share(0) == 0.80
    assert mip_schedule.ufmip_refund_share(36) == 0.0
    assert mip_schedule.ufmip_refund_share(None) == 0.0
