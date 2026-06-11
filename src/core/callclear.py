"""Layer 3 — call-clear: execution-risk heuristics. Implements docs/domain-rules.md §4.

This is NOT a deterministic rule engine. It surfaces hard blockers, soft blockers,
and unknowns so a human can judge whether a file is worth outreach. A loan can be
agency-clear and economically-clear yet still not call-clear.
"""

from __future__ import annotations

from .config import assumptions as A
from .models import CallClearResult, ClearanceResult, Loan, Scenario

_SEASONING_REASONS = {
    "seasoning_210_days",
    "seasoning_six_payments",
    "fha_seasoning_six_payments",
    "fha_seasoning_six_months",
    "fha_seasoning_210_days",
}


def evaluate(loan: Loan, scenario: Scenario, agency: ClearanceResult) -> CallClearResult:
    """Call-clear verdict (domain-rules §4). ``agency`` feeds known defects in."""
    hard: list[str] = []
    soft: list[str] = []
    unknown: list[str] = []

    # --- Hard blockers: known disqualifiers ---
    if any(reason in _SEASONING_REASONS for reason in agency.failed):
        hard.append("seasoning_defect")
    if loan.lates_30_in_6mo > A.FHA_MAX_30DAY_LATES_6MO:
        hard.append("payment_history_defect")

    # --- Soft blockers: quote-integrity / optics risk ---
    if scenario.new_term_months > loan.remaining_term_months:
        soft.append("term_reset_optics")
    if loan.escrowed:
        soft.append("escrow_reset_risk")
    if loan.program == "FHA" and scenario.new_monthly_MIP != scenario.old_monthly_MIP:
        soft.append("insurance_payment_shift")

    # --- Unknowns: need human review, not computable from synthetic data ---
    if loan.has_second_lien:
        unknown.append("subordination_unknown")
    if loan.program == "VA":
        unknown.append("prior_occupancy_cert_unknown")
        if not loan.va_funding_fee_exempt:
            unknown.append("funding_fee_exempt_unknown")
    if loan.program == "FHA":
        unknown.append("manual_underwrite_path")
        payment_jump = (
            scenario.old_monthly_PIMIP > 0
            and scenario.new_monthly_PIMIP
            >= scenario.old_monthly_PIMIP * (1 + A.FHA_CREDIT_QUALIFY_PAYMENT_INCREASE)
        )
        if loan.borrower_change_pending or payment_jump:
            unknown.append("credit_qualifying_required")

    return CallClearResult(
        clear=not hard,
        hard_blockers=hard,
        soft_blockers=soft,
        unknowns=unknown,
    )
