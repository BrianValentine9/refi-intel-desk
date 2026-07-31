"""Economics tests — program lens, break-even threshold, cost buckets, financing."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.core import mip
from src.core.config.assumptions import AS_OF_DATE
from src.core.economics import (
    build_scenario,
    economically_clear,
    reset_ufmip_fallback_counts,
    ufmip_fallback_counts,
)
from src.core.ufmip import (
    ApplicabilityOutcome,
    TerminationReason,
    TransactionType,
    UfmipInputs,
    compute_ufmip,
)
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
    # Prior closing well outside the 3-year refund window, so the financed fee is
    # the gross 1.75% with no credit against it.
    loan = make_loan(program="FHA", balance=300_000.0,
                     closing_date=date(2022, 1, 10),
                     fha_endorsement_date=date(2022, 2, 10),
                     prior_fha_closing_date=date(2022, 1, 10))
    scen = build_scenario(loan, 6.0)
    # New balance = balance + 1.75% UFMIP; ordinary closing costs are NOT financed.
    assert scen.new_balance == 305_250.0
    assert scen.new_annual_mip_rate == 0.0055


# --- FHA UFMIP routed through the reference calculator -----------------------

def _windowed_fha_loan(**overrides):
    """An FHA loan whose prior closing sits inside the UFMIP refund window."""
    defaults = dict(
        program="FHA", balance=300_000.0,
        closing_date=date(2025, 5, 6), first_payment_date=date(2025, 6, 6),
        fha_endorsement_date=date(2025, 6, 5),
        fha_case_assignment_date=date(2026, 5, 31),
        prior_fha_closing_date=date(2025, 5, 6),
        transaction_type="REFI_STREAMLINE",
        prior_ufmip_paid=5250.0,
        prior_insurance_termination_reason="REFINANCE_PAYOFF",
    )
    defaults.update(overrides)
    return make_loan(**defaults)


def test_fha_financed_fee_matches_reference_calculator():
    """The desk's financed fee equals compute_ufmip on the same inputs, spelled out.

    The expected UfmipInputs are built by hand rather than through the adapter, so
    this pins the field mapping (which desk column feeds which calculator field),
    not just the arithmetic.
    """
    loan = _windowed_fha_loan()
    reset_ufmip_fallback_counts()
    scen = build_scenario(loan, 6.0)

    expected = compute_ufmip(UfmipInputs(
        base_loan_amount=300_000,
        program="203B",
        transaction_type=TransactionType.REFI_STREAMLINE,
        new_closing_date=AS_OF_DATE,
        case_assignment_date=date(2026, 5, 31),
        loan_term_months=loan.original_term_months,
        finance_ufmip=True,
        prior_loan_fha=True,
        prior_endorsement_date=date(2025, 6, 5),
        prior_closing_date=date(2025, 5, 6),
        prior_ufmip_paid=Decimal("5250.0"),
        prior_insurance_termination_reason=TerminationReason.REFINANCE_PAYOFF,
    ))
    assert expected.status is ApplicabilityOutcome.APPLICABLE_WITH_PREMIUM

    # Independently pinned: gross 5250.00, schedule month 14 -> 54% of 5250.00.
    assert expected.gross_ufmip == Decimal("5250.00")
    assert expected.applied_refund_credit == Decimal("2835.00")
    assert expected.financed_ufmip == Decimal("2415")

    assert scen.new_balance == round(300_000.0 + float(expected.financed_ufmip), 2)
    assert ufmip_fallback_counts() == {}


def test_fha_refund_credit_lowers_the_financed_fee_and_the_economic_cost():
    """A loan inside the refund window finances less than the same loan outside it."""
    inside = build_scenario(_windowed_fha_loan(), 6.0)
    outside = build_scenario(_windowed_fha_loan(
        closing_date=date(2022, 1, 10), first_payment_date=date(2022, 2, 10),
        fha_endorsement_date=date(2022, 2, 10),
        prior_fha_closing_date=date(2022, 1, 10),
    ), 6.0)
    assert inside.new_balance < outside.new_balance
    assert inside.economic_total_cost < outside.economic_total_cost


def test_fha_falls_back_to_legacy_when_the_calculator_declines():
    """A loan the calculator cannot price still gets a fee, counted as a fallback.

    Endorsement 10 years before the prior closing is chronologically impossible,
    so compute_ufmip returns INVALID_INPUT. The ladder must not crash on it.
    """
    loan = make_loan(program="FHA", balance=300_000.0,
                     fha_endorsement_date=date(2015, 1, 1),
                     fha_case_assignment_date=AS_OF_DATE)
    reset_ufmip_fallback_counts()
    scen = build_scenario(loan, 6.0)

    assert ufmip_fallback_counts() == {"INVALID_INPUT": 1}
    legacy_fee = mip.ufmip_amount(300_000.0, 0.0175, None)
    assert scen.new_balance == round(300_000.0 + legacy_fee, 2)


def test_va_financing_unchanged_by_the_ufmip_swap():
    """VA still finances only the 0.5% funding fee and never touches the calculator."""
    reset_ufmip_fallback_counts()
    scen = build_scenario(make_loan(program="VA", balance=300_000.0), 6.0)
    assert scen.new_balance == 301_500.0
    assert scen.old_monthly_MIP == 0.0
    assert scen.new_monthly_MIP == 0.0
    assert scen.old_annual_mip_rate == 0.0
    assert ufmip_fallback_counts() == {}


def test_whole_synthetic_pool_prices_without_a_single_fallback():
    """The pool's FHA rows are coherent enough for the calculator to price every one."""
    from src.core import pool

    reset_ufmip_fallback_counts()
    for loan in pool.generate_pool():
        if loan.program == "FHA":
            build_scenario(loan, 6.0)
    assert ufmip_fallback_counts() == {}


def test_three_cost_buckets_are_separate():
    loan = make_loan(program="VA", balance=300_000.0)
    scen = build_scenario(loan, 6.0)
    assert scen.agency_recoupment_cost == 3000.0  # 1.0% of balance
    assert scen.excluded_prepaids_escrow_cost == 1200.0  # 0.4% of balance
    # Economic total is its own bucket and is larger than the agency one alone.
    assert scen.economic_total_cost > scen.agency_recoupment_cost
