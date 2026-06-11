"""Shared dataclasses for the analysis core.

Kept dependency-free so every other core module can import these without cycles.
The three-layer clearance design (agency / economic / call) is described in
docs/domain-rules.md §1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Loan:
    """One synthetic loan. Schema per CC-BRIEF-03 §2 and docs/domain-rules.md §2.

    Dates are real ``date`` objects in memory; persistence stores them as ISO
    strings. ``tags`` records which seeded edge-case strata a loan belongs to so
    tests can find them.
    """

    loan_id: str
    program: str  # "VA" | "FHA"
    note_rate: float  # percent, e.g. 6.5
    balance: float
    original_term_months: int
    remaining_term_months: int
    first_payment_date: date
    closing_date: date  # closing of the loan being refinanced
    payments_made: int
    consecutive_on_time: int
    lates_30_in_6mo: int
    product_type: str  # "fixed" | "arm"
    occupancy: str  # "owner" | "prior" | "non_owner"
    has_second_lien: bool
    borrower_change_pending: bool
    va_funding_fee_exempt: bool
    fha_endorsement_date: date | None
    fha_case_assignment_date: date | None
    prior_fha_closing_date: date | None  # for UFMIP refund credit
    escrowed: bool
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClearanceResult:
    """Machine-readable verdict for one layer — never a bare boolean (brief §1)."""

    clear: bool
    failed: list[str] = field(default_factory=list)
    values: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CallClearResult:
    """Layer 3 verdict: execution risk as categorized flags, not a rule engine."""

    clear: bool  # True when there are no hard blockers
    hard_blockers: list[str] = field(default_factory=list)
    soft_blockers: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Scenario:
    """A priced refi scenario for one loan at one new note rate.

    Holds the old and new monthly figures plus the three separate cost buckets
    (docs/domain-rules.md §3). Rule functions read these computed numbers and
    stay pure.
    """

    loan: Loan
    new_note_rate: float
    new_product_type: str
    new_term_months: int
    new_balance: float

    old_monthly_PI: float
    new_monthly_PI: float
    old_monthly_MIP: float
    new_monthly_MIP: float
    old_annual_mip_rate: float  # as a rate, e.g. 0.0055
    new_annual_mip_rate: float

    # Three cost buckets — always separate (domain-rules §3).
    agency_recoupment_cost: float
    economic_total_cost: float
    excluded_prepaids_escrow_cost: float

    @property
    def old_monthly_PIMIP(self) -> float:
        return round(self.old_monthly_PI + self.old_monthly_MIP, 2)

    @property
    def new_monthly_PIMIP(self) -> float:
        return round(self.new_monthly_PI + self.new_monthly_MIP, 2)

    @property
    def monthly_savings_PI(self) -> float:
        return round(self.old_monthly_PI - self.new_monthly_PI, 2)

    @property
    def monthly_savings_PIMIP(self) -> float:
        return round(self.old_monthly_PIMIP - self.new_monthly_PIMIP, 2)

    @property
    def old_combined_rate(self) -> float:
        """Note rate + annual MIP rate, in percentage points (domain-rules §2.2.3)."""
        return self.loan.note_rate + self.old_annual_mip_rate * 100

    @property
    def new_combined_rate(self) -> float:
        return self.new_note_rate + self.new_annual_mip_rate * 100
