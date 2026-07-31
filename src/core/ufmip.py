"""FHA UFMIP reference calculator, grounded in HUD Handbook 4000.1.

An authored reference implementation of the upfront mortgage-insurance-premium
computation: rate selection, gross premium, the refund-credit offset applied on
refinances, and the financed-or-cash split. It is a reference for checking the
arithmetic against the Handbook, not a production underwriting authority.

Public contract: compute_ufmip(inputs: UfmipInputs) -> UfmipResult. Every branch
below is annotated with the rule step it implements; the rule model is grounded in
Handbook 4000.1 Appendix 1.0 (rate schedule effective 2023-03-20).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR
from enum import Enum

CENT = Decimal("0.01")
DOLLAR = Decimal("1")

RULE_VERSION = "4000.1-update17-appendix1.0-2023-03-20"
RATE_TABLE_EFFECTIVE_DATE = date(2023, 3, 20)

STREAMLINE_LEGACY_CUTOFF = date(2009, 5, 31)
STREAMLINE_SEASONING_DAYS = 210
MAX_POLICY_TERM_MONTHS = 360


# --- Applicability outcome ---------------------------------------------------
class ApplicabilityOutcome(str, Enum):
    APPLICABLE_WITH_PREMIUM = "APPLICABLE_WITH_PREMIUM"
    APPLICABLE_ZERO_PREMIUM = "APPLICABLE_ZERO_PREMIUM"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INVALID_INPUT = "INVALID_INPUT"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"


class TransactionType(str, Enum):
    PURCHASE = "PURCHASE"
    REFI_STREAMLINE = "REFI_STREAMLINE"
    REFI_SIMPLE = "REFI_SIMPLE"
    REFI_RATE_TERM = "REFI_RATE_TERM"
    REFI_CASH_OUT = "REFI_CASH_OUT"


REFI_TRANSACTION_TYPES = {
    TransactionType.REFI_STREAMLINE,
    TransactionType.REFI_SIMPLE,
    TransactionType.REFI_RATE_TERM,
    TransactionType.REFI_CASH_OUT,
}


class TerminationReason(str, Enum):
    REFINANCE_PAYOFF = "REFINANCE_PAYOFF"
    CLAIM = "CLAIM"
    FORECLOSURE_DIL = "FORECLOSURE_DIL"
    OTHER = "OTHER"


class RefundSource(str, Enum):
    FHAC = "FHAC"
    LOCAL_RULE = "LOCAL_RULE"
    LOCAL_ASSUMPTION = "LOCAL_ASSUMPTION"
    NONE = "NONE"


class Assumption(str, Enum):
    REFUND_CLOCK_CLOSING_ANCHOR = "REFUND_CLOCK_CLOSING_ANCHOR"
    CASH_OUT_REFUND_ELIGIBLE = "CASH_OUT_REFUND_ELIGIBLE"


# --- Program classification map ----------------------------------------------
IN_SCOPE_PROGRAMS = {
    "203B", "203B_REPAIR_ESCROW", "203H", "203K_STANDARD", "203K_LIMITED",
    "234C", "SEC247", "SEC248", "EEM_203B", "EEM_203K",
    "HUD_REO_GNND", "HUD_REO_100DOWN",
}
OUT_OF_SCOPE_PROGRAMS = {"HECM", "TITLE_I", "MULTIFAMILY", "NON_FHA"}
RECOGNIZED_UNPRICED_PROGRAMS = {"SEC223E"}

# Section 247 UFMIP matrix (term bands compared in months, upper-bound inclusive).
SEC_247_BANDS = (
    (216, "LE216"),
    (264, "217_264"),
    (300, "265_300"),
    (None, "GT300"),
)
SEC_247_RATES_FINANCED = {
    "LE216": Decimal("0.024"), "217_264": Decimal("0.030"),
    "265_300": Decimal("0.036"), "GT300": Decimal("0.038"),
}
SEC_247_RATES_CASH = {
    "LE216": Decimal("0.02344"), "217_264": Decimal("0.02913"),
    "265_300": Decimal("0.03475"), "GT300": Decimal("0.03661"),
}

STANDARD_RATE = Decimal("0.0175")
STREAMLINE_LEGACY_RATE = Decimal("0.0001")

# UFMIP refund grid, verbatim (index 0 = schedule month 1).
REFUND_TABLE_PCT = (
    80, 78, 76, 74, 72, 70, 68, 66, 64, 62, 60, 58,   # year 1
    56, 54, 52, 50, 48, 46, 44, 42, 40, 38, 36, 34,   # year 2
    32, 30, 28, 26, 24, 22, 20, 18, 16, 14, 12, 10,   # year 3
)


@dataclass
class UfmipInputs:
    base_loan_amount: int
    program: str
    transaction_type: TransactionType
    new_closing_date: date
    case_assignment_date: date
    loan_term_months: int
    finance_ufmip: bool | None = None
    prior_loan_fha: bool | None = None
    prior_endorsement_date: date | None = None
    prior_closing_date: date | None = None
    prior_ufmip_paid: Decimal | None = None
    new_disbursement_date: date | None = None
    prior_insurance_termination_reason: TerminationReason | None = None
    fhac_refund_credit: Decimal | None = None


@dataclass
class UfmipResult:
    status: ApplicabilityOutcome
    rate: Decimal | None = None
    rate_rule_id: str | None = None
    rate_reason: str = ""
    rule_version: str = RULE_VERSION
    gross_ufmip: Decimal | None = None
    refund_clock_start_date: date | None = None
    refund_clock_end_date: date | None = None
    completed_months: int | None = None
    refund_schedule_month: int | None = None
    refund_source: RefundSource = RefundSource.NONE
    refund_eligibility_reason: str = ""
    computed_refund_credit: Decimal | None = None
    applied_refund_credit: Decimal | None = None
    forfeited_refund_credit: Decimal | None = None
    fhac_discrepancy: Decimal | None = None
    net_ufmip: Decimal | None = None
    financed_ufmip: Decimal | None = None
    cash_ufmip: Decimal | None = None
    total_mortgage: int | None = None
    assumptions_applied: list[Assumption] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)


def _round_cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _round_down_dollars(value: Decimal) -> int:
    return int(value.quantize(DOLLAR, rounding=ROUND_FLOOR))


def _is_last_day_of_month(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def completed_months(start: date, end: date) -> int:
    """Completed whole months, anniversary method with end-of-month clamp."""
    m = 12 * (end.year - start.year) + (end.month - start.month)
    if end.day < start.day and not _is_last_day_of_month(end):
        m -= 1
    return m


def refund_pct(schedule_month: int) -> Decimal:
    """Verbatim refund-grid lookup (not the -2/month linear formula)."""
    if 1 <= schedule_month <= 36:
        return Decimal(REFUND_TABLE_PCT[schedule_month - 1])
    return Decimal("0")


def _sec247_band(term_months: int) -> str:
    for ceiling, label in SEC_247_BANDS:
        if ceiling is None or term_months <= ceiling:
            return label
    return "GT300"  # unreachable; SEC_247_BANDS always terminates on None


def _sec247_rate(loan_term_months: int, finance_ufmip: bool) -> tuple[Decimal, str, str]:
    band = _sec247_band(loan_term_months)
    rates = SEC_247_RATES_FINANCED if finance_ufmip else SEC_247_RATES_CASH
    rate = rates[band]
    fin_tag = "FIN" if finance_ufmip else "CASH"
    rule_id = f"SEC247_{band}_{fin_tag}"
    reason = f"Sec. 247 matrix, term band {band}, {'financed' if finance_ufmip else 'not financed'}"
    return rate, rule_id, reason


def _determine_rate(inputs: UfmipInputs) -> tuple[Decimal, str, str]:
    """Precedence-ordered rate decision table, rows 2-4 (row 1 / Sec. 248 handled earlier)."""
    if inputs.program == "SEC247":
        return _sec247_rate(inputs.loan_term_months, bool(inputs.finance_ufmip))

    if (
        inputs.transaction_type in (TransactionType.REFI_STREAMLINE, TransactionType.REFI_SIMPLE)
        and inputs.prior_endorsement_date is not None
        and inputs.prior_endorsement_date <= STREAMLINE_LEGACY_CUTOFF
    ):
        return (
            STREAMLINE_LEGACY_RATE,
            "LEGACY_REFI_001",
            "row 3: streamline/simple, prior endorsed on/before 2009-05-31 -> 0.01%",
        )

    return STANDARD_RATE, "STD_175", "row 4: standard 1.75%"


def _refund_clock_end(inputs: UfmipInputs) -> date:
    return max(inputs.new_closing_date, inputs.new_disbursement_date or inputs.new_closing_date)


def _validate(inputs: UfmipInputs) -> tuple[list[str], list[str], date | None, date | None, int | None, int | None]:
    """Input validation. Chronology is validated before any month derivation.

    Returns (errors, warnings, refund_clock_start, refund_clock_end, completed_months, refund_schedule_month).
    The clock fields are computed here (needed to decide whether prior_ufmip_paid is
    required) but only surfaced on the result when the transaction is actually a
    refi with prior_loan_fha=true (null on purchases / no prior FHA loan).
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(inputs.base_loan_amount, int) or inputs.base_loan_amount <= 0:
        errors.append("base_loan_amount must be a whole-dollar integer > 0")

    is_refi = inputs.transaction_type in REFI_TRANSACTION_TYPES

    if is_refi and inputs.prior_loan_fha is None:
        errors.append("prior_loan_fha is required for refinance transaction types")

    if inputs.transaction_type in (TransactionType.REFI_STREAMLINE, TransactionType.REFI_SIMPLE):
        if inputs.prior_loan_fha is not True:
            errors.append("prior_loan_fha must be true for REFI_STREAMLINE / REFI_SIMPLE")

    clock_start: date | None = None
    clock_end: date | None = None
    n_months: int | None = None
    schedule_month: int | None = None

    if inputs.prior_loan_fha:
        if inputs.prior_closing_date is None:
            errors.append("prior_closing_date is required when prior_loan_fha is true")
        if inputs.prior_endorsement_date is None:
            errors.append("prior_endorsement_date is required when prior_loan_fha is true")

        if inputs.prior_closing_date is not None and inputs.prior_endorsement_date is not None:
            clock_end = _refund_clock_end(inputs)
            if inputs.prior_closing_date > inputs.prior_endorsement_date:
                errors.append("prior_closing_date must be on or before prior_endorsement_date")
            if inputs.prior_closing_date >= clock_end:
                errors.append("prior_closing_date must be before the refund clock end date")
            if inputs.prior_endorsement_date > inputs.new_closing_date:
                errors.append("prior_endorsement_date must not be after new_closing_date (future-dated prior)")

            if not errors:
                clock_start = inputs.prior_closing_date
                n_months = completed_months(clock_start, clock_end)
                schedule_month = n_months + 1

    if inputs.case_assignment_date > inputs.new_closing_date:
        errors.append("case_assignment_date must be on or before new_closing_date")

    window_open = (
        is_refi
        and inputs.prior_loan_fha
        and schedule_month is not None
        and schedule_month <= 36
    )
    if window_open and inputs.fhac_refund_credit is None:
        if inputs.prior_ufmip_paid is None or inputs.prior_ufmip_paid < 0:
            errors.append(
                "prior_ufmip_paid must be present and >= 0 when the refund window is "
                "potentially open and no FHAC figure is supplied"
            )
        if inputs.prior_insurance_termination_reason is None:
            errors.append(
                "prior_insurance_termination_reason is required when the refund window is "
                "potentially open and no FHAC figure is supplied (null is only acceptable "
                "when FHAC has already adjudicated eligibility)"
            )

    if inputs.fhac_refund_credit is not None and inputs.fhac_refund_credit < 0:
        errors.append("fhac_refund_credit must be >= 0 when present")

    if inputs.prior_ufmip_paid is not None and inputs.prior_ufmip_paid < 0:
        errors.append("prior_ufmip_paid must be >= 0 when present")

    if inputs.program != "SEC248" and inputs.finance_ufmip is None:
        errors.append("finance_ufmip is required for all in-scope programs except SEC248")
    if inputs.program == "SEC248" and inputs.finance_ufmip is not None:
        warnings.append("FINANCE_UFMIP_IGNORED_SEC248")

    if not (isinstance(inputs.loan_term_months, int) and inputs.loan_term_months > 0):
        errors.append("loan_term_months must be an integer > 0")
    elif inputs.loan_term_months > MAX_POLICY_TERM_MONTHS:
        warnings.append("TERM_EXCEEDS_POLICY")

    if inputs.transaction_type == TransactionType.REFI_STREAMLINE and inputs.prior_closing_date is not None:
        seasoning_days = (inputs.case_assignment_date - inputs.prior_closing_date).days
        if seasoning_days < STREAMLINE_SEASONING_DAYS:
            warnings.append("STREAMLINE_SEASONING_SHORT")

    if inputs.transaction_type == TransactionType.PURCHASE and (
        inputs.prior_loan_fha or inputs.prior_closing_date or inputs.prior_endorsement_date
        or inputs.prior_ufmip_paid is not None
    ):
        warnings.append("STALE_PRIOR_FIELDS")

    return errors, warnings, clock_start, clock_end, n_months, schedule_month


def compute_ufmip(inputs: UfmipInputs) -> UfmipResult:
    # Step 1: product routing happens first (out-of-scope short-circuits everything
    # else, including rate-table selection - a HECM never consults rate config).
    if inputs.program in OUT_OF_SCOPE_PROGRAMS:
        return UfmipResult(
            status=ApplicabilityOutcome.NOT_APPLICABLE,
            rule_version=RULE_VERSION,
            rate_reason="recognized out-of-scope product; not a forward-mortgage UFMIP case",
        )

    if inputs.program in RECOGNIZED_UNPRICED_PROGRAMS:
        return UfmipResult(
            status=ApplicabilityOutcome.INVALID_INPUT,
            rule_version=RULE_VERSION,
            validation_errors=[
                f"SEC223E_MANUAL_REVIEW: program {inputs.program!r} is recognized-but-unpriced "
                "legacy authority; route to manual review, never a silent 1.75% default"
            ],
        )

    if inputs.program not in IN_SCOPE_PROGRAMS:
        return UfmipResult(
            status=ApplicabilityOutcome.INVALID_INPUT,
            rule_version=RULE_VERSION,
            validation_errors=[f"unrecognized program code: {inputs.program!r}"],
        )

    # Step 0: select the rate-table version (in-scope programs only).
    if inputs.case_assignment_date < RATE_TABLE_EFFECTIVE_DATE:
        return UfmipResult(
            status=ApplicabilityOutcome.CONFIGURATION_ERROR,
            rule_version=RULE_VERSION,
            validation_errors=[
                f"no configured rate-table version covers case_assignment_date {inputs.case_assignment_date}"
            ],
        )

    # Step 1 (cont'd): full input validation, chronology before month math.
    errors, warnings, clock_start, clock_end, n_months, schedule_month = _validate(inputs)
    if errors:
        return UfmipResult(
            status=ApplicabilityOutcome.INVALID_INPUT,
            rule_version=RULE_VERSION,
            validation_errors=errors,
            warnings=warnings,
        )

    if inputs.program == "SEC248":
        return UfmipResult(
            status=ApplicabilityOutcome.APPLICABLE_ZERO_PREMIUM,
            rate=Decimal("0.00"),
            rate_rule_id="SEC248_ZERO",
            rate_reason="row 1: Sec. 248 -> no UFMIP",
            rule_version=RULE_VERSION,
            gross_ufmip=Decimal("0.00"),
            refund_source=RefundSource.NONE,
            computed_refund_credit=Decimal("0.00"),
            applied_refund_credit=Decimal("0.00"),
            forfeited_refund_credit=Decimal("0.00"),
            net_ufmip=Decimal("0.00"),
            financed_ufmip=Decimal("0.00"),
            cash_ufmip=Decimal("0.00"),
            total_mortgage=inputs.base_loan_amount,
            warnings=warnings,
        )

    # Step 1.5 + Step 2: rate + gross UFMIP.
    rate, rate_rule_id, rate_reason = _determine_rate(inputs)
    gross_ufmip = _round_cents(rate * inputs.base_loan_amount)

    # Step 3: refund credit (the five-condition eligibility predicate).
    is_refi = inputs.transaction_type in REFI_TRANSACTION_TYPES
    assumptions: list[Assumption] = []
    refund_source = RefundSource.NONE
    refund_eligibility_reason = "not a refinance" if not is_refi else ""
    computed_refund_credit = Decimal("0.00")
    applied_refund_credit = Decimal("0.00")
    forfeited_refund_credit = Decimal("0.00")
    fhac_discrepancy: Decimal | None = None
    result_clock_start = result_clock_end = None
    result_n_months = result_schedule_month = None

    if is_refi and inputs.prior_loan_fha:
        result_clock_start, result_clock_end = clock_start, clock_end
        result_n_months, result_schedule_month = n_months, schedule_month
        assumptions.append(Assumption.REFUND_CLOCK_CLOSING_ANCHOR)

        termination_ok = (
            inputs.prior_insurance_termination_reason == TerminationReason.REFINANCE_PAYOFF
            or (inputs.prior_insurance_termination_reason is None and inputs.fhac_refund_credit is not None)
        )
        window_ok = (schedule_month is not None and schedule_month <= 36) or inputs.fhac_refund_credit is not None
        eligible = termination_ok and window_ok

        if not termination_ok:
            refund_eligibility_reason = "prior insurance not terminated by refinance payoff"
        elif not window_ok:
            refund_eligibility_reason = f"refund window expired (schedule month {schedule_month} > 36)"
        else:
            refund_eligibility_reason = "eligible"

        if eligible:
            if inputs.transaction_type == TransactionType.REFI_CASH_OUT and inputs.fhac_refund_credit is None:
                assumptions.append(Assumption.CASH_OUT_REFUND_ELIGIBLE)

            if inputs.prior_ufmip_paid is not None and schedule_month is not None:
                computed_refund_credit = _round_cents(
                    (refund_pct(schedule_month) / Decimal(100)) * inputs.prior_ufmip_paid
                )

            if inputs.fhac_refund_credit is not None:
                applied_refund_credit = min(inputs.fhac_refund_credit, gross_ufmip)
                refund_source = RefundSource.FHAC
                if inputs.prior_ufmip_paid is not None:
                    fhac_discrepancy = computed_refund_credit - inputs.fhac_refund_credit
                    # Both figures are already cent-rounded; any nonzero gap warns (no dollar band).
                    if fhac_discrepancy != 0:
                        warnings.append("FHAC_DISCREPANCY")
            else:
                applied_refund_credit = min(computed_refund_credit, gross_ufmip)
                refund_source = (
                    RefundSource.LOCAL_ASSUMPTION
                    if inputs.transaction_type == TransactionType.REFI_CASH_OUT
                    else RefundSource.LOCAL_RULE
                )

            forfeited_refund_credit = max(Decimal("0.00"), computed_refund_credit - applied_refund_credit)

    # Step 4: net UFMIP.
    net_ufmip = gross_ufmip - applied_refund_credit

    # Step 5 / 6: financed-or-cash, single round-down.
    if not inputs.finance_ufmip:
        cash_ufmip = net_ufmip
        financed_ufmip = Decimal("0.00")
        total_mortgage = inputs.base_loan_amount
    else:
        total_mortgage = _round_down_dollars(Decimal(inputs.base_loan_amount) + net_ufmip)
        financed_ufmip = Decimal(total_mortgage - inputs.base_loan_amount)
        cash_ufmip = net_ufmip - financed_ufmip

    return UfmipResult(
        status=ApplicabilityOutcome.APPLICABLE_WITH_PREMIUM,
        rate=rate,
        rate_rule_id=rate_rule_id,
        rate_reason=rate_reason,
        rule_version=RULE_VERSION,
        gross_ufmip=gross_ufmip,
        refund_clock_start_date=result_clock_start,
        refund_clock_end_date=result_clock_end,
        completed_months=result_n_months,
        refund_schedule_month=result_schedule_month,
        refund_source=refund_source,
        refund_eligibility_reason=refund_eligibility_reason,
        computed_refund_credit=computed_refund_credit,
        applied_refund_credit=applied_refund_credit,
        forfeited_refund_credit=forfeited_refund_credit,
        fhac_discrepancy=fhac_discrepancy,
        net_ufmip=net_ufmip,
        financed_ufmip=financed_ufmip,
        cash_ufmip=cash_ufmip,
        total_mortgage=total_mortgage,
        assumptions_applied=assumptions,
        warnings=warnings,
    )
