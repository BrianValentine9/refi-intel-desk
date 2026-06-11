"""Layer 2 — economics: scenario construction and economically-clear.

Builds a priced refi Scenario for a loan at a new note rate (old/new P&I, MIP,
P+I+MIP, and the three separate cost buckets), then judges whether the refinance
is genuinely worth doing under the correct program lens. See docs/domain-rules.md
§3. Pure functions, no I/O.
"""

from __future__ import annotations

import math

from . import amort, mip
from .config import assumptions as A
from .config.mip_schedule import fha_mip_rates
from .dateutil import full_months_between
from .models import ClearanceResult, Loan, Scenario


def _default_new_product(loan: Loan) -> str:
    """Streamline default: ARM refinances to fixed; fixed stays fixed."""
    return "fixed"


def build_scenario(
    loan: Loan,
    new_note_rate: float,
    *,
    new_product_type: str | None = None,
    new_term_months: int | None = None,
    cost_pct: float = A.RECOUPMENT_COST_PCT_BASE,
) -> Scenario:
    """Price one refi scenario for ``loan`` at ``new_note_rate``.

    Financing rules (domain-rules §2.1.6, §2.2.2, §2.2.10):
      - VA: funding fee (0.5% unless exempt) is financed into the new balance.
      - FHA: net UFMIP (after any refund credit) is financed; ordinary closing
        costs are NOT financed into the new loan amount.
    """
    new_product_type = new_product_type or _default_new_product(loan)
    new_term_months = new_term_months or loan.original_term_months

    old_monthly_PI = amort.monthly_pi(loan.balance, loan.note_rate, loan.remaining_term_months)

    if loan.program == "VA":
        old_annual_mip_rate = new_annual_mip_rate = 0.0
        funding_fee = 0.0 if loan.va_funding_fee_exempt else loan.balance * A.VA_IRRRL_FUNDING_FEE_RATE
        financed_fee = funding_fee
        new_balance = loan.balance + financed_fee
        old_monthly_MIP = new_monthly_MIP = 0.0
    elif loan.program == "FHA":
        rates = fha_mip_rates(loan.fha_endorsement_date)
        old_annual_mip_rate = new_annual_mip_rate = rates["annual_mip_rate"]
        months_since_prior = (
            full_months_between(loan.prior_fha_closing_date, A.AS_OF_DATE)
            if loan.prior_fha_closing_date is not None
            else None
        )
        financed_fee = mip.ufmip_amount(loan.balance, rates["ufmip_rate"], months_since_prior)
        new_balance = loan.balance + financed_fee
        old_monthly_MIP = mip.monthly_mip(loan.balance, old_annual_mip_rate)
        new_monthly_MIP = mip.monthly_mip(new_balance, new_annual_mip_rate)
    else:
        raise ValueError(f"Unknown program: {loan.program!r}")

    new_monthly_PI = amort.monthly_pi(new_balance, new_note_rate, new_term_months)

    # Three separate cost buckets (domain-rules §3).
    agency_recoupment_cost = round(loan.balance * cost_pct, 2)
    excluded_prepaids_escrow_cost = round(loan.balance * A.PREPAIDS_ESCROW_PCT, 2)
    economic_total_cost = round(
        agency_recoupment_cost + excluded_prepaids_escrow_cost + financed_fee, 2
    )

    return Scenario(
        loan=loan,
        new_note_rate=new_note_rate,
        new_product_type=new_product_type,
        new_term_months=new_term_months,
        new_balance=round(new_balance, 2),
        old_monthly_PI=old_monthly_PI,
        new_monthly_PI=new_monthly_PI,
        old_monthly_MIP=old_monthly_MIP,
        new_monthly_MIP=new_monthly_MIP,
        old_annual_mip_rate=old_annual_mip_rate,
        new_annual_mip_rate=new_annual_mip_rate,
        agency_recoupment_cost=agency_recoupment_cost,
        economic_total_cost=economic_total_cost,
        excluded_prepaids_escrow_cost=excluded_prepaids_escrow_cost,
    )


def lens_monthly_savings(scenario: Scenario) -> float:
    """Monthly savings under the correct program lens (domain-rules §3).

    P&I for the VA framing; P+I+MIP for FHA.
    """
    if scenario.loan.program == "VA":
        return scenario.monthly_savings_PI
    return scenario.monthly_savings_PIMIP


def economic_break_even_months(scenario: Scenario) -> float:
    """All-in break-even = economic total cost / lens monthly savings.

    Infinite when there are no monthly savings.
    """
    savings = lens_monthly_savings(scenario)
    if savings <= 0:
        return math.inf
    return scenario.economic_total_cost / savings


def economically_clear(
    scenario: Scenario, *, threshold_months: int = A.BREAK_EVEN_THRESHOLD_MONTHS
) -> ClearanceResult:
    """Layer 2 verdict — positive savings AND all-in break-even within the house
    threshold (HOUSE POLICY, not agency law — domain-rules §3)."""
    savings = lens_monthly_savings(scenario)
    break_even = economic_break_even_months(scenario)
    failed: list[str] = []
    if savings <= 0:
        failed.append("no_monthly_savings")
    if break_even > threshold_months:
        failed.append("break_even_over_threshold")
    return ClearanceResult(
        clear=not failed,
        failed=failed,
        values={
            "lens_monthly_savings": round(savings, 2),
            "economic_break_even_months_all_in": (
                round(break_even, 2) if math.isfinite(break_even) else None
            ),
            "threshold_months": threshold_months,
        },
    )
