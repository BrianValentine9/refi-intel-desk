"""Economics tests — program lens, break-even threshold, cost buckets, financing."""

from __future__ import annotations

from datetime import date

from src.core.config.assumptions import AS_OF_DATE
from src.core.economics import build_scenario, economically_clear
from tests.conftest import make_loan, make_scenario


def test_va_pi_lens_within_threshold_is_clear():
    loan = make_loan()  # VA
    scen = make_scenario(loan, 6.0, old_monthly_PI=2000.0, new_monthly_PI=1900.0,
                         economic_total_cost=4000.0)
    result = economically_clear(scen)
    assert result.clear is True
    assert result.values["economic_break_even_months_all_in"] == 40.0


def test_break_even_over_threshold_fails():
    loan = make_loan()
    scen = make_scenario(loan, 6.0, old_monthly_PI=2000.0, new_monthly_PI=1900.0,
                         economic_total_cost=5000.0)  # 50 months > 48
    result = economically_clear(scen)
    assert result.clear is False
    assert "break_even_over_threshold" in result.failed


def test_no_savings_fails():
    loan = make_loan()
    scen = make_scenario(loan, 6.0, old_monthly_PI=1900.0, new_monthly_PI=2000.0)
    assert "no_monthly_savings" in economically_clear(scen).failed


def test_fha_uses_pimip_lens_not_pi():
    # P&I improves (+$100) but MIP rises (+$200): under the FHA P+I+MIP lens this
    # is a net loss and must fail, proving the lens isn't plain P&I.
    loan = make_loan(program="FHA", fha_endorsement_date=date(2015, 1, 1),
                     fha_case_assignment_date=AS_OF_DATE)
    scen = make_scenario(loan, 6.0, old_monthly_PI=2000.0, new_monthly_PI=1900.0,
                         old_monthly_MIP=0.0, new_monthly_MIP=200.0)
    assert "no_monthly_savings" in economically_clear(scen).failed


def test_build_scenario_va_finances_funding_fee():
    loan = make_loan(program="VA", balance=300_000.0, va_funding_fee_exempt=False)
    scen = build_scenario(loan, 6.0)
    assert scen.new_balance > loan.balance  # 0.5% funding fee financed


def test_build_scenario_va_exempt_finances_nothing():
    loan = make_loan(program="VA", balance=300_000.0, va_funding_fee_exempt=True)
    assert build_scenario(loan, 6.0).new_balance == loan.balance


def test_build_scenario_fha_finances_ufmip_only():
    loan = make_loan(program="FHA", balance=300_000.0,
                     fha_endorsement_date=date(2015, 1, 1))
    scen = build_scenario(loan, 6.0)
    # New balance = balance + 1.75% UFMIP; ordinary closing costs are NOT financed.
    assert abs(scen.new_balance - (300_000.0 + 300_000.0 * 0.0175)) < 1.0
    assert scen.new_annual_mip_rate == 0.0055


def test_three_cost_buckets_are_separate():
    loan = make_loan(program="VA", balance=300_000.0)
    scen = build_scenario(loan, 6.0)
    assert scen.agency_recoupment_cost == 3000.0  # 1.0% of balance
    assert scen.excluded_prepaids_escrow_cost == 1200.0  # 0.4% of balance
    # Economic total is its own bucket and is larger than the agency one alone.
    assert scen.economic_total_cost > scen.agency_recoupment_cost
