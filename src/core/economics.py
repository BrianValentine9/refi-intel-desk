"""Layer 2 — economics: scenario construction and economically-clear.

Builds a priced refi Scenario for a loan at a new note rate (old/new P&I, MIP,
P+I+MIP, and the three separate cost buckets), then judges whether the refinance
is genuinely worth doing under the correct program lens. See docs/domain-rules.md
§3. Pure functions, no I/O.
"""

from __future__ import annotations

import logging
import math

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from . import amort, mip, ufmip
from .config import assumptions as A
from .config.mip_schedule import fha_mip_rates
from .dateutil import full_months_between
from .models import ClearanceResult, Loan, Scenario

logger = logging.getLogger(__name__)


def _default_new_product(loan: Loan) -> str:
    """Streamline default: ARM refinances to fixed; fixed stays fixed."""
    return "fixed"


# --- FHA UFMIP adapter -------------------------------------------------------
# The desk prices its UFMIP through the HUD-grade reference calculator in
# src/core/ufmip.py. That module is a reference for checking arithmetic against
# Handbook 4000.1, not a production underwriting authority; the pool it runs
# against is synthetic. Decimal lives inside this seam only - scenario math on
# either side of it stays float.

DESK_PROGRAM_CODE = "203B"  # the desk models ordinary forward FHA only
DESK_TRANSACTION_TYPE = ufmip.TransactionType.REFI_STREAMLINE
DESK_FINANCE_UFMIP = True  # the desk always finances net UFMIP into the new balance

# Loans that could not be priced by the calculator and fell back to the legacy
# path, keyed by the reason. Counts, never exceptions: the ladder must not crash
# on one bad row.
_ufmip_fallbacks: Counter[str] = Counter()


def ufmip_fallback_counts() -> dict[str, int]:
    """Reason -> count of loans that fell back to the legacy UFMIP path."""
    return dict(_ufmip_fallbacks)


def reset_ufmip_fallback_counts() -> None:
    """Clear the fallback tally (used by tests and by one-shot report runs)."""
    _ufmip_fallbacks.clear()


def _to_decimal(value: float | None) -> Decimal | None:
    """Float -> Decimal at the boundary, via str so the cents are what they read as."""
    return None if value is None else Decimal(str(value))


def _ufmip_inputs(loan: Loan, new_term_months: int) -> ufmip.UfmipInputs:
    """Build the calculator's input record from a desk Loan.

    The loan on the desk is the one being refinanced, so it supplies every
    ``prior_*`` field. Anything the pool leaves None gets the desk default.
    """
    return ufmip.UfmipInputs(
        base_loan_amount=int(round(loan.balance)),
        program=DESK_PROGRAM_CODE,
        transaction_type=(
            ufmip.TransactionType(loan.transaction_type)
            if loan.transaction_type
            else DESK_TRANSACTION_TYPE
        ),
        new_closing_date=A.AS_OF_DATE,
        case_assignment_date=loan.fha_case_assignment_date or A.AS_OF_DATE,
        loan_term_months=new_term_months,
        finance_ufmip=DESK_FINANCE_UFMIP,
        prior_loan_fha=True,
        prior_endorsement_date=loan.fha_endorsement_date,
        prior_closing_date=loan.prior_fha_closing_date or loan.closing_date,
        prior_ufmip_paid=_to_decimal(loan.prior_ufmip_paid),
        prior_insurance_termination_reason=(
            ufmip.TerminationReason(loan.prior_insurance_termination_reason)
            if loan.prior_insurance_termination_reason
            else None
        ),
        fhac_refund_credit=_to_decimal(loan.fhac_refund_credit),
    )


def _record_fallback(reason: str, loan: Loan, detail: str) -> None:
    _ufmip_fallbacks[reason] += 1
    logger.warning(
        "UFMIP fallback to legacy path for loan %s (%s): %s", loan.loan_id, reason, detail
    )


def fha_financed_ufmip(loan: Loan, ufmip_rate: float, new_term_months: int) -> float:
    """Net UFMIP financed into the new FHA balance, priced by ``ufmip.compute_ufmip``.

    Falls back to the legacy ``mip.ufmip_amount`` synthetic whenever the calculator
    declines a loan the desk believes is FHA - an unusable input, an unconfigured
    rate era, or a product it does not price. Every fallback is logged and counted;
    none of them stop the ladder.
    """
    def legacy() -> float:
        months_since_prior = (
            full_months_between(loan.prior_fha_closing_date, A.AS_OF_DATE)
            if loan.prior_fha_closing_date is not None
            else None
        )
        return mip.ufmip_amount(loan.balance, ufmip_rate, months_since_prior)

    try:
        result = ufmip.compute_ufmip(_ufmip_inputs(loan, new_term_months))
    except (ValueError, TypeError) as exc:  # unrecognized enum value, bad date type
        _record_fallback("ADAPTER_ERROR", loan, f"{type(exc).__name__}: {exc}")
        return legacy()

    if result.status is ufmip.ApplicabilityOutcome.APPLICABLE_WITH_PREMIUM:
        return float(result.financed_ufmip)
    if result.status is ufmip.ApplicabilityOutcome.APPLICABLE_ZERO_PREMIUM:
        return 0.0

    _record_fallback(result.status.value, loan, "; ".join(result.validation_errors))
    return legacy()


@dataclass(frozen=True)
class LoanBase:
    """The rate-independent half of a refi scenario for one loan.

    Everything here is fixed for a loan regardless of the trigger rate, so the
    ladder computes it once and reuses it across every rung (only new_monthly_PI
    changes with the rate).
    """

    new_product_type: str
    new_term_months: int
    new_balance: float
    old_monthly_PI: float
    old_monthly_MIP: float
    new_monthly_MIP: float
    old_annual_mip_rate: float
    new_annual_mip_rate: float
    agency_recoupment_cost: float
    economic_total_cost: float
    excluded_prepaids_escrow_cost: float


def precompute_base(
    loan: Loan,
    *,
    new_product_type: str | None = None,
    new_term_months: int | None = None,
    cost_pct: float = A.RECOUPMENT_COST_PCT_BASE,
) -> LoanBase:
    """Compute the rate-independent parts of a refi scenario once.

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
        financed_fee = 0.0 if loan.va_funding_fee_exempt else loan.balance * A.VA_IRRRL_FUNDING_FEE_RATE
        new_balance = loan.balance + financed_fee
        old_monthly_MIP = new_monthly_MIP = 0.0
    elif loan.program == "FHA":
        rates = fha_mip_rates(loan.fha_endorsement_date)
        old_annual_mip_rate = new_annual_mip_rate = rates["annual_mip_rate"]
        # Only UFMIP routes through the reference calculator; monthly/annual MIP
        # still comes from the versioned schedule above.
        financed_fee = fha_financed_ufmip(loan, rates["ufmip_rate"], new_term_months)
        new_balance = loan.balance + financed_fee
        old_monthly_MIP = mip.monthly_mip(loan.balance, old_annual_mip_rate)
        new_monthly_MIP = mip.monthly_mip(new_balance, new_annual_mip_rate)
    else:
        raise ValueError(f"Unknown program: {loan.program!r}")

    # Three separate cost buckets (domain-rules §3).
    agency_recoupment_cost = round(loan.balance * cost_pct, 2)
    excluded_prepaids_escrow_cost = round(loan.balance * A.PREPAIDS_ESCROW_PCT, 2)
    economic_total_cost = round(
        agency_recoupment_cost + excluded_prepaids_escrow_cost + financed_fee, 2
    )

    return LoanBase(
        new_product_type=new_product_type,
        new_term_months=new_term_months,
        new_balance=round(new_balance, 2),
        old_monthly_PI=old_monthly_PI,
        old_monthly_MIP=old_monthly_MIP,
        new_monthly_MIP=new_monthly_MIP,
        old_annual_mip_rate=old_annual_mip_rate,
        new_annual_mip_rate=new_annual_mip_rate,
        agency_recoupment_cost=agency_recoupment_cost,
        economic_total_cost=economic_total_cost,
        excluded_prepaids_escrow_cost=excluded_prepaids_escrow_cost,
    )


def scenario_at_rate(loan: Loan, base: LoanBase, new_note_rate: float) -> Scenario:
    """Assemble a Scenario from a precomputed base at a specific new note rate."""
    new_monthly_PI = amort.monthly_pi(base.new_balance, new_note_rate, base.new_term_months)
    return Scenario(
        loan=loan,
        new_note_rate=new_note_rate,
        new_product_type=base.new_product_type,
        new_term_months=base.new_term_months,
        new_balance=base.new_balance,
        old_monthly_PI=base.old_monthly_PI,
        new_monthly_PI=new_monthly_PI,
        old_monthly_MIP=base.old_monthly_MIP,
        new_monthly_MIP=base.new_monthly_MIP,
        old_annual_mip_rate=base.old_annual_mip_rate,
        new_annual_mip_rate=base.new_annual_mip_rate,
        agency_recoupment_cost=base.agency_recoupment_cost,
        economic_total_cost=base.economic_total_cost,
        excluded_prepaids_escrow_cost=base.excluded_prepaids_escrow_cost,
    )


def build_scenario(
    loan: Loan,
    new_note_rate: float,
    *,
    new_product_type: str | None = None,
    new_term_months: int | None = None,
    cost_pct: float = A.RECOUPMENT_COST_PCT_BASE,
) -> Scenario:
    """Price one refi scenario for ``loan`` at ``new_note_rate`` (convenience wrapper)."""
    base = precompute_base(
        loan, new_product_type=new_product_type, new_term_months=new_term_months, cost_pct=cost_pct
    )
    return scenario_at_rate(loan, base, new_note_rate)


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
