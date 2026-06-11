"""Layer 1 — FHA Streamline agency-clear rules. Implements docs/domain-rules.md §2.2.

Pure functions: (loan, scenario) in, ClearanceResult out. NTB is computed on the
combined rate (note rate + annual MIP rate), never note rate alone.
"""

from __future__ import annotations

from .config import assumptions as A
from .dateutil import days_between, full_months_between
from .models import ClearanceResult, Loan, Scenario


def agency_clear(loan: Loan, scenario: Scenario) -> ClearanceResult:
    """FHA Streamline agency-clear verdict (domain-rules §2.2)."""
    if loan.program != "FHA":
        raise ValueError("rules_fha.agency_clear called on a non-FHA loan")

    failed: list[str] = []

    old_combined = scenario.old_combined_rate
    new_combined = scenario.new_combined_rate
    combined_drop = old_combined - new_combined
    term_reduction = scenario.new_term_months < loan.remaining_term_months

    # §2.2.4 Net tangible benefit on the combined rate.
    if term_reduction:
        ok = (new_combined < old_combined) and (
            scenario.new_monthly_PIMIP
            <= scenario.old_monthly_PIMIP + A.FHA_NTB_TERM_REDUCTION_PIMIP_TOLERANCE
        )
        if not ok:
            failed.append("ntb_term_reduction")
    else:
        if combined_drop < A.FHA_NTB_COMBINED_DROP:
            failed.append("ntb_combined_rate_050")

    # §2.2.5 Seasoning, measured at FHA case-number assignment.
    measure_date = loan.fha_case_assignment_date or A.AS_OF_DATE
    months_since_first = full_months_between(loan.first_payment_date, measure_date)
    days_since_prior = days_between(loan.closing_date, measure_date)
    if loan.payments_made < A.FHA_SEASONING_PAYMENTS:
        failed.append("fha_seasoning_six_payments")
    if months_since_first < A.FHA_SEASONING_MONTHS:
        failed.append("fha_seasoning_six_months")
    if days_since_prior < A.FHA_SEASONING_DAYS:
        failed.append("fha_seasoning_210_days")

    # §2.2.6 Payment history — at most one 30-day late in the prior six months.
    if loan.lates_30_in_6mo > A.FHA_MAX_30DAY_LATES_6MO:
        failed.append("payment_history_lates")

    # §2.2.9 Non-owner-occupied may only streamline into a fixed-rate mortgage.
    if loan.occupancy == "non_owner" and scenario.new_product_type != "fixed":
        failed.append("non_owner_requires_fixed")

    return ClearanceResult(
        clear=not failed,
        failed=failed,
        values={
            "old_combined_rate": round(old_combined, 4),
            "new_combined_rate": round(new_combined, 4),
            "combined_rate_drop": round(combined_drop, 4),
            "term_reduction": term_reduction,
            "payments_made": loan.payments_made,
            "months_since_first_payment": months_since_first,
            "days_since_prior_closing": days_since_prior,
            "lates_30_in_6mo": loan.lates_30_in_6mo,
        },
    )
