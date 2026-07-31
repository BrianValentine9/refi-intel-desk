"""Verification suite for the FHA UFMIP reference calculator (src/core/ufmip.py).

Reference vectors plus boundary and invariant tests, grounded in HUD Handbook
4000.1 Appendix 1.0. Each vector pins a documented rule to its expected output.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.core.ufmip import (
    Assumption,
    ApplicabilityOutcome,
    RefundSource,
    SEC_247_RATES_CASH,
    SEC_247_RATES_FINANCED,
    TerminationReason,
    TransactionType,
    UfmipInputs,
    completed_months,
    compute_ufmip,
    refund_pct,
)

D = Decimal


def make_inputs(**overrides) -> UfmipInputs:
    defaults = dict(
        base_loan_amount=None,
        program="203B",
        transaction_type=TransactionType.PURCHASE,
        new_closing_date=None,
        case_assignment_date=None,
        loan_term_months=360,
        finance_ufmip=True,
        prior_loan_fha=None,
        prior_endorsement_date=None,
        prior_closing_date=None,
        prior_ufmip_paid=None,
        new_disbursement_date=None,
        prior_insurance_termination_reason=None,
        fhac_refund_credit=None,
    )
    defaults.update(overrides)
    if defaults["case_assignment_date"] is None:
        defaults["case_assignment_date"] = defaults["new_closing_date"]
    if (
        "prior_insurance_termination_reason" not in overrides
        and defaults["transaction_type"] in (
            TransactionType.REFI_STREAMLINE, TransactionType.REFI_SIMPLE,
            TransactionType.REFI_RATE_TERM, TransactionType.REFI_CASH_OUT,
        )
        and defaults["prior_loan_fha"]
    ):
        defaults["prior_insurance_termination_reason"] = TerminationReason.REFINANCE_PAYOFF
    return UfmipInputs(**defaults)


# --------------------------------------------------------------------------
# Reference vectors
# --------------------------------------------------------------------------

def test_v1_financed_purchase():
    r = compute_ufmip(make_inputs(base_loan_amount=400_000, new_closing_date=date(2026, 6, 1)))
    assert r.status == ApplicabilityOutcome.APPLICABLE_WITH_PREMIUM
    assert r.rate == D("0.0175")
    assert r.gross_ufmip == D("7000.00")
    assert r.net_ufmip == D("7000.00")
    assert r.financed_ufmip == D("7000.00")
    assert r.cash_ufmip == D("0.00")
    assert r.total_mortgage == 407_000


def test_v2_cash_purchase():
    r = compute_ufmip(make_inputs(
        base_loan_amount=250_000, new_closing_date=date(2026, 6, 1), finance_ufmip=False,
    ))
    assert r.rate == D("0.0175")
    assert r.gross_ufmip == D("4375.00")
    assert r.financed_ufmip == D("0.00")
    assert r.cash_ufmip == D("4375.00")
    assert r.total_mortgage == 250_000


def test_v3_subdollar_remainder():
    r = compute_ufmip(make_inputs(base_loan_amount=333_333, new_closing_date=date(2026, 6, 1)))
    assert r.gross_ufmip == D("5833.33")
    assert r.financed_ufmip == D("5833.00")
    assert r.cash_ufmip == D("0.33")
    assert r.total_mortgage == 339_166


def test_v4_refund_mid_table_and_anchor_divergence():
    base = dict(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("5250.00"),
    )
    r = compute_ufmip(make_inputs(**base))
    assert r.gross_ufmip == D("5250.00")
    assert r.completed_months == 13
    assert r.refund_schedule_month == 14
    assert r.computed_refund_credit == D("2835.00")
    assert r.applied_refund_credit == D("2835.00")
    assert r.net_ufmip == D("2415.00")
    assert r.total_mortgage == 302_415
    assert r.refund_source == RefundSource.LOCAL_RULE
    assert Assumption.REFUND_CLOCK_CLOSING_ANCHOR in r.assumptions_applied

    # Named criterion (false-green kill): endorsement-date anchor must land in a
    # DIFFERENT tier (mo 12 / 58%) than closing-date anchor (mo 14 / 54%).
    closing_anchor_months = completed_months(base["prior_closing_date"], base["new_closing_date"])
    endorsement_anchor_months = completed_months(base["prior_endorsement_date"], base["new_closing_date"])
    assert closing_anchor_months != endorsement_anchor_months
    assert refund_pct(closing_anchor_months + 1) != refund_pct(endorsement_anchor_months + 1)
    assert refund_pct(closing_anchor_months + 1) == D("54")
    assert refund_pct(endorsement_anchor_months + 1) == D("58")


def test_v5_schedule_month_36_last_nonzero():
    r = compute_ufmip(make_inputs(
        base_loan_amount=400_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2023, 7, 15),
        prior_endorsement_date=date(2023, 9, 1),
        new_closing_date=date(2026, 7, 10),
        prior_ufmip_paid=D("7000.00"),
    ))
    assert r.refund_schedule_month == 36
    assert r.computed_refund_credit == D("700.00")
    assert r.net_ufmip == D("6300.00")
    assert r.total_mortgage == 406_300


def test_v6_schedule_month_37_expired():
    r = compute_ufmip(make_inputs(
        base_loan_amount=400_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2023, 7, 1),
        prior_endorsement_date=date(2023, 9, 1),
        new_closing_date=date(2026, 7, 2),
        prior_ufmip_paid=D("7000.00"),
    ))
    assert r.completed_months == 36
    assert r.refund_schedule_month == 37
    assert r.computed_refund_credit == D("0.00")
    assert r.applied_refund_credit == D("0.00")
    assert r.net_ufmip == D("7000.00")
    assert r.total_mortgage == 407_000


def test_v7_legacy_streamline_rate_refund_independence():
    r = compute_ufmip(make_inputs(
        base_loan_amount=200_000,
        transaction_type=TransactionType.REFI_STREAMLINE,
        prior_loan_fha=True,
        prior_endorsement_date=date(2009, 4, 15),
        prior_closing_date=date(2009, 2, 20),
        new_closing_date=date(2026, 7, 1),
    ))
    assert r.rate == D("0.0001")
    assert r.rate_rule_id == "LEGACY_REFI_001"
    assert r.gross_ufmip == D("20.00")
    assert r.computed_refund_credit == D("0.00")  # window long expired
    assert r.net_ufmip == D("20.00")
    assert r.total_mortgage == 200_020


def test_v8_sec247_financed_gt300():
    r = compute_ufmip(make_inputs(base_loan_amount=350_000, program="SEC247",
                                   loan_term_months=360, new_closing_date=date(2026, 6, 1)))
    assert r.rate == D("0.038")
    assert r.rate_rule_id == "SEC247_GT300_FIN"
    assert r.gross_ufmip == D("13300.00")
    assert r.total_mortgage == 363_300


def test_v9_sec247_cash_le216():
    r = compute_ufmip(make_inputs(base_loan_amount=350_000, program="SEC247",
                                   loan_term_months=180, finance_ufmip=False,
                                   new_closing_date=date(2026, 6, 1)))
    assert r.rate == D("0.02344")
    assert r.gross_ufmip == D("8204.00")
    assert r.total_mortgage == 350_000


def test_v10_sec248_zero_premium():
    r = compute_ufmip(make_inputs(base_loan_amount=300_000, program="SEC248",
                                   finance_ufmip=None, new_closing_date=date(2026, 6, 1)))
    assert r.status == ApplicabilityOutcome.APPLICABLE_ZERO_PREMIUM
    assert r.rate_rule_id == "SEC248_ZERO"
    assert r.gross_ufmip == D("0.00")
    assert r.total_mortgage == 300_000


def test_v11_unknown_program_fails_closed():
    r = compute_ufmip(make_inputs(base_loan_amount=275_000, program="XX9",
                                   new_closing_date=date(2026, 6, 1)))
    assert r.status == ApplicabilityOutcome.INVALID_INPUT
    assert r.rate is None
    assert any("unrecognized program code" in e for e in r.validation_errors)


def test_v12_cash_out_refund_local_assumption():
    r = compute_ufmip(make_inputs(
        base_loan_amount=280_000,
        transaction_type=TransactionType.REFI_CASH_OUT,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 11, 20),
        prior_endorsement_date=date(2026, 1, 5),
        new_closing_date=date(2026, 7, 15),
        prior_ufmip_paid=D("6000.00"),
    ))
    assert r.refund_schedule_month == 8
    assert r.computed_refund_credit == D("3960.00")
    assert r.applied_refund_credit == D("3960.00")
    assert r.net_ufmip == D("940.00")
    assert r.total_mortgage == 280_940
    assert r.refund_source == RefundSource.LOCAL_ASSUMPTION
    assert Assumption.CASH_OUT_REFUND_ELIGIBLE in r.assumptions_applied


def test_v13_sec247_264_month_band_edge():
    r = compute_ufmip(make_inputs(base_loan_amount=350_000, program="SEC247",
                                   loan_term_months=264, new_closing_date=date(2026, 6, 1)))
    assert r.rate == D("0.030")
    assert r.gross_ufmip == D("10500.00")
    assert r.total_mortgage == 360_500


def test_v14_forfeited_credit_capped_at_gross():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2026, 4, 20),
        prior_endorsement_date=date(2026, 5, 15),
        new_closing_date=date(2026, 7, 1),
        prior_ufmip_paid=D("8750.00"),
    ))
    assert r.refund_schedule_month == 3
    assert r.computed_refund_credit == D("6650.00")
    assert r.applied_refund_credit == D("5250.00")
    assert r.forfeited_refund_credit == D("1400.00")
    assert r.net_ufmip == D("0.00")
    assert r.total_mortgage == 300_000
    assert r.status == ApplicabilityOutcome.APPLICABLE_WITH_PREMIUM


def test_v15_purchase_with_stale_prior_fields_no_refund():
    r = compute_ufmip(make_inputs(
        base_loan_amount=320_000,
        transaction_type=TransactionType.PURCHASE,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 1, 10),
        prior_endorsement_date=date(2025, 3, 1),
        new_closing_date=date(2026, 7, 1),
    ))
    assert r.gross_ufmip == D("5600.00")
    assert r.computed_refund_credit == D("0.00")
    assert r.refund_source == RefundSource.NONE
    assert r.completed_months is None
    assert "STALE_PRIOR_FIELDS" in r.warnings
    assert r.total_mortgage == 325_600


def test_v16_fhac_override_and_discrepancy():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("5250.00"),
        fhac_refund_credit=D("2500.00"),
    ))
    assert r.computed_refund_credit == D("2835.00")
    assert r.applied_refund_credit == D("2500.00")
    assert r.refund_source == RefundSource.FHAC
    assert r.fhac_discrepancy == D("335.00")
    assert "FHAC_DISCREPANCY" in r.warnings
    assert r.net_ufmip == D("2750.00")
    assert r.total_mortgage == 302_750


def test_v17_reversed_dates_never_alias_to_month_one():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2026, 8, 1),          # after new_closing_date
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("5250.00"),
    ))
    assert r.status == ApplicabilityOutcome.INVALID_INPUT
    assert r.refund_schedule_month is None
    assert r.gross_ufmip is None


# --------------------------------------------------------------------------
# Boundary tests
# --------------------------------------------------------------------------

def test_boundary_month_zero_completed_schedule_one():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2026, 7, 1),
        prior_endorsement_date=date(2026, 7, 1),
        new_closing_date=date(2026, 7, 20),
        prior_ufmip_paid=D("1000.00"),
    ))
    assert r.completed_months == 0
    assert r.refund_schedule_month == 1
    assert r.computed_refund_credit == D("800.00")  # 80%


def test_boundary_end_of_month_clamp():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 1, 31),
        prior_endorsement_date=date(2025, 1, 31),
        new_closing_date=date(2025, 2, 28),
        prior_ufmip_paid=D("1000.00"),
    ))
    assert r.completed_months == 1
    assert r.refund_schedule_month == 2
    assert r.computed_refund_credit == D("780.00")  # 78%


def test_boundary_schedule_month_35():
    r = compute_ufmip(make_inputs(
        base_loan_amount=400_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2023, 7, 15),
        prior_endorsement_date=date(2023, 9, 1),
        new_closing_date=date(2026, 6, 10),
        prior_ufmip_paid=D("7000.00"),
    ))
    assert r.completed_months == 34
    assert r.refund_schedule_month == 35


@pytest.mark.parametrize(
    "prior_endorsement, expected_rate, expected_rule_id",
    [
        (date(2009, 5, 31), D("0.0001"), "LEGACY_REFI_001"),  # inclusive cutoff
        (date(2009, 6, 1), D("0.0175"), "STD_175"),
    ],
)
def test_boundary_2009_cutoff_inclusive(prior_endorsement, expected_rate, expected_rule_id):
    r = compute_ufmip(make_inputs(
        base_loan_amount=200_000,
        transaction_type=TransactionType.REFI_STREAMLINE,
        prior_loan_fha=True,
        prior_endorsement_date=prior_endorsement,
        prior_closing_date=date(2009, 2, 1),
        new_closing_date=date(2026, 7, 1),
    ))
    assert r.rate == expected_rate
    assert r.rate_rule_id == expected_rule_id


@pytest.mark.parametrize(
    "term_months, expect_financed_rate",
    [
        (216, D("0.024")), (217, D("0.030")),
        (264, D("0.030")), (265, D("0.036")),
        (300, D("0.036")), (301, D("0.038")),
    ],
)
def test_boundary_sec247_month_bands(term_months, expect_financed_rate):
    r = compute_ufmip(make_inputs(base_loan_amount=350_000, program="SEC247",
                                   loan_term_months=term_months, new_closing_date=date(2026, 6, 1)))
    assert r.rate == expect_financed_rate


def test_boundary_reversed_dates_invalid():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2026, 8, 1),
        prior_endorsement_date=date(2026, 8, 1),
        new_closing_date=date(2026, 6, 1),
        prior_ufmip_paid=D("1000.00"),
    ))
    assert r.status == ApplicabilityOutcome.INVALID_INPUT


def test_boundary_future_case_assignment_date_invalid():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        new_closing_date=date(2026, 6, 1),
        case_assignment_date=date(2026, 7, 1),  # after new_closing_date
    ))
    assert r.status == ApplicabilityOutcome.INVALID_INPUT


def test_boundary_fhac_absent_uses_local_rule():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("5250.00"),
    ))
    assert r.refund_source == RefundSource.LOCAL_RULE
    assert r.fhac_discrepancy is None


def test_boundary_fhac_present_exact_match_no_flag():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("5250.00"),
        fhac_refund_credit=D("2835.00"),  # matches computed_refund_credit exactly
    ))
    assert r.refund_source == RefundSource.FHAC
    assert r.fhac_discrepancy == D("0.00")
    assert "FHAC_DISCREPANCY" not in r.warnings


def test_boundary_fhac_present_tiny_difference_warns():
    # No dollar band: any nonzero gap warns, no matter how small.
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("5250.00"),
        fhac_refund_credit=D("2835.50"),  # $0.50 off computed_refund_credit of 2835.00
    ))
    assert r.refund_source == RefundSource.FHAC
    assert r.fhac_discrepancy == D("-0.50")
    assert "FHAC_DISCREPANCY" in r.warnings


def test_boundary_sec248_finance_flag_ignored_with_warning():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000, program="SEC248", finance_ufmip=True,
        new_closing_date=date(2026, 6, 1),
    ))
    assert r.status == ApplicabilityOutcome.APPLICABLE_ZERO_PREMIUM
    assert "FINANCE_UFMIP_IGNORED_SEC248" in r.warnings
    assert r.total_mortgage == 300_000


def test_boundary_half_cent_rounding():
    r = compute_ufmip(make_inputs(base_loan_amount=302, new_closing_date=date(2026, 6, 1)))
    # 0.0175 * 302 = 5.2850 exactly -> half-up ties round up.
    assert r.gross_ufmip == D("5.29")


def test_forfeited_refund_credit_never_negative_when_fhac_exceeds_computed():
    # computed_refund_credit = 2835.00 (as in v4/v16); FHAC = 3000.00 > computed,
    # still <= gross (5250.00). forfeited must clamp to 0, not go negative;
    # the signed gap lives in fhac_discrepancy only.
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("5250.00"),
        fhac_refund_credit=D("3000.00"),
    ))
    assert r.computed_refund_credit == D("2835.00")
    assert r.applied_refund_credit == D("3000.00")
    assert r.forfeited_refund_credit == D("0.00")
    assert r.fhac_discrepancy == D("-165.00")
    assert "FHAC_DISCREPANCY" in r.warnings


def test_negative_prior_ufmip_paid_rejected():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("-100.00"),
    ))
    assert r.status == ApplicabilityOutcome.INVALID_INPUT


def test_negative_prior_ufmip_paid_rejected_even_with_fhac_present():
    # Previously ">= 0" was only enforced when the window was open AND no FHAC
    # figure was supplied; with FHAC present, a negative prior_ufmip_paid still
    # reached Step 3 and poisoned computed_refund_credit / fhac_discrepancy.
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("-100.00"),
        fhac_refund_credit=D("2500.00"),
    ))
    assert r.status == ApplicabilityOutcome.INVALID_INPUT


def test_negative_fhac_refund_credit_rejected():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("5250.00"),
        fhac_refund_credit=D("-1.00"),
    ))
    assert r.status == ApplicabilityOutcome.INVALID_INPUT
    assert any("fhac_refund_credit" in e for e in r.validation_errors)


def test_out_of_scope_program_before_rate_table_configuration_error():
    # HECM + a case-assignment date before the rate table's effective date must
    # still route to NOT_APPLICABLE (product routing first) - not CONFIGURATION_ERROR.
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        program="HECM",
        new_closing_date=date(2020, 1, 15),
        case_assignment_date=date(2020, 1, 1),  # before RATE_TABLE_EFFECTIVE_DATE
    ))
    assert r.status == ApplicabilityOutcome.NOT_APPLICABLE


def test_missing_termination_reason_no_fhac_fails_closed():
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("5250.00"),
        prior_insurance_termination_reason=None,
    ))
    assert r.status == ApplicabilityOutcome.INVALID_INPUT
    assert any("prior_insurance_termination_reason" in e for e in r.validation_errors)


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fhac", [None, D("0.00"), D("100.00"), D("2835.00"), D("5250.00"), D("999999.00")]
)
def test_net_ufmip_never_exceeds_gross_invariant(fhac):
    # The invariant the fhac_refund_credit >= 0 guard exists to protect: a refund
    # credit only ever reduces the premium. A negative FHAC figure used to make
    # min(fhac, gross) negative, so net = gross - applied came out ABOVE gross.
    r = compute_ufmip(make_inputs(
        base_loan_amount=300_000,
        transaction_type=TransactionType.REFI_RATE_TERM,
        prior_loan_fha=True,
        prior_closing_date=date(2025, 5, 10),
        prior_endorsement_date=date(2025, 7, 1),
        new_closing_date=date(2026, 6, 20),
        prior_ufmip_paid=D("5250.00"),
        fhac_refund_credit=fhac,
    ))
    assert r.status == ApplicabilityOutcome.APPLICABLE_WITH_PREMIUM
    assert r.applied_refund_credit >= D("0.00")
    assert D("0.00") <= r.net_ufmip <= r.gross_ufmip
    assert r.financed_ufmip + r.cash_ufmip == r.net_ufmip


def test_sec247_row_identity_invariant():
    for band, financed_rate in SEC_247_RATES_FINANCED.items():
        cash_rate = SEC_247_RATES_CASH[band]
        derived_cash = financed_rate / (1 + financed_rate)
        assert abs(derived_cash - cash_rate) <= D("0.00001")  # 0.001pp


def test_refund_table_minus_two_per_month_pattern_vs_grid():
    # The -2/month linear pattern is a derived convenience only; the grid governs.
    # Assert the pattern holds against the actual verbatim grid (not the other way around).
    for month in range(2, 37):
        assert refund_pct(month) == refund_pct(month - 1) - 2
    assert refund_pct(1) == D("80")
    assert refund_pct(36) == D("10")
    assert refund_pct(37) == D("0")
    assert refund_pct(0) == D("0")
