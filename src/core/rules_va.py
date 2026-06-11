"""Layer 1 — VA IRRRL agency-clear rules. Implements docs/domain-rules.md §2.1.

Pure functions: (loan, scenario) in, ClearanceResult out. Every failure carries a
machine-readable reason and the values behind it (brief §1).
"""

from __future__ import annotations

import math

from .config import assumptions as A
from .dateutil import days_between
from .models import ClearanceResult, Loan, Scenario


def va_statutory_recoupment_months(scenario: Scenario) -> float:
    """Statutory recoupment in months — eligible costs / monthly P&I reduction.

    domain-rules §2.1.5. Denominator is the P&I reduction ONLY. Infinite when the
    new payment is not lower (the caller enforces the zero-cost rule separately).
    """
    pi_reduction = scenario.old_monthly_PI - scenario.new_monthly_PI
    if pi_reduction <= 0:
        return math.inf
    return scenario.agency_recoupment_cost / pi_reduction


def _ntb_passes(loan: Loan, scenario: Scenario) -> bool:
    """VA net tangible benefit by product direction (domain-rules §2.1.4, §7a)."""
    drop = loan.note_rate - scenario.new_note_rate
    old, new = loan.product_type, scenario.new_product_type
    if old == "fixed" and new == "fixed":
        return drop >= A.VA_NTB_FIXED_TO_FIXED_DROP
    if old == "fixed" and new == "arm":
        return drop >= A.VA_NTB_FIXED_TO_ARM_DROP
    if old == "arm" and new == "fixed":
        # Architect ruling §7a: any rate reduction is a tangible benefit.
        return scenario.new_note_rate < loan.note_rate
    # ARM->ARM is unspecified in the source; require a real reduction.
    return drop > 0


def agency_clear(loan: Loan, scenario: Scenario, *, as_of=A.AS_OF_DATE) -> ClearanceResult:
    """VA IRRRL agency-clear verdict (domain-rules §2.1)."""
    if loan.program != "VA":
        raise ValueError("rules_va.agency_clear called on a non-VA loan")

    failed: list[str] = []

    # §2.1.3 Seasoning — first payment due >= 210 days before closing AND 6 payments.
    days_seasoned = days_between(loan.first_payment_date, as_of)
    if days_seasoned < A.VA_SEASONING_DAYS:
        failed.append("seasoning_210_days")
    if loan.consecutive_on_time < A.VA_SEASONING_PAYMENTS:
        failed.append("seasoning_six_payments")

    # §2.1.4 Net tangible benefit.
    if not _ntb_passes(loan, scenario):
        failed.append("ntb_not_met")

    # §2.1.5 Statutory recoupment.
    pi_reduction = scenario.old_monthly_PI - scenario.new_monthly_PI
    months = va_statutory_recoupment_months(scenario)
    if pi_reduction > 0:
        if months > A.VA_RECOUPMENT_MAX_MONTHS:
            failed.append("recoupment_over_36")
    else:
        # No P&I reduction => eligible costs must be exactly zero (program rule).
        if scenario.agency_recoupment_cost != 0:
            failed.append("recoupment_costs_without_pi_reduction")

    return ClearanceResult(
        clear=not failed,
        failed=failed,
        values={
            "days_seasoned": days_seasoned,
            "consecutive_on_time": loan.consecutive_on_time,
            "rate_drop": round(loan.note_rate - scenario.new_note_rate, 4),
            "va_statutory_recoupment_months": (
                round(months, 2) if math.isfinite(months) else None
            ),
            "monthly_pi_reduction": round(pi_reduction, 2),
        },
    )
