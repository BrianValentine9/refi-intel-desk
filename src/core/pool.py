"""Synthetic loan pool — deterministic, seeded generator + SQLite persistence.

Same seed produces a byte-identical pool (asserted in tests). Composition and the
seeded edge-case strata follow CC-BRIEF-03 §2 and docs/domain-rules.md §6. This is
synthetic data: no real borrower, lead, or employer data is used.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import date, timedelta

from src.data import db

from .config.assumptions import AS_OF_DATE
from .config.mip_schedule import fha_mip_rates
from .models import Loan

DEFAULT_SEED = 20260610
DEFAULT_POOL_SIZE = 5000

_RATE_STEP = 0.125

# Stratum tags so tests can find seeded edge cases.
TAG_LEGACY_TAIL = "legacy_tail"
TAG_VA_ARM_TO_FIXED = "va_arm_to_fixed"
TAG_TERM_REDUCTION = "term_reduction"
TAG_FHA_PRE_2009 = "fha_pre_2009"
TAG_FHA_REFI_WITHIN_3YR = "fha_refi_within_3yr"
TAG_SECOND_LIEN = "second_lien"
TAG_BORROWER_REMOVAL = "borrower_removal"
TAG_VA_FEE_EXEMPT = "va_fee_exempt"
TAG_PAYMENT_DEFECT = "payment_defect"
TAG_NON_OWNER_FHA = "non_owner_fha"
TAG_SEASONING_BOUNDARY = "seasoning_boundary"

ALL_TAGS = (
    TAG_LEGACY_TAIL, TAG_VA_ARM_TO_FIXED, TAG_TERM_REDUCTION, TAG_FHA_PRE_2009,
    TAG_FHA_REFI_WITHIN_3YR, TAG_SECOND_LIEN, TAG_BORROWER_REMOVAL,
    TAG_VA_FEE_EXEMPT, TAG_PAYMENT_DEFECT, TAG_NON_OWNER_FHA, TAG_SEASONING_BOUNDARY,
)


def _snap_rate(value: float) -> float:
    return round(round(value / _RATE_STEP) * _RATE_STEP, 3)


def _date_minus_days(days: int) -> date:
    return AS_OF_DATE - timedelta(days=days)


def _fha_contract_fields(
    closing_date: date, balance: float, *, endorsement_date: date | None = None
) -> dict:
    """Coherent FHA UFMIP contract fields for one row.

    Every FHA row in this pool is an FHA-to-FHA refinance, so the loan being
    refinanced *is* the prior FHA loan: endorsement follows its closing, the UFMIP
    refund clock anchors on that same closing (domain-rules §2.2.10), and the
    termination reason is the payoff that this refinance performs.

    ``prior_ufmip_paid`` is a SYNTHETIC proxy: the era UFMIP rate applied to the
    current balance, because the pool does not model the prior loan's original
    amount. ``fhac_refund_credit`` stays None because the desk has no FHA Connection feed,
    so the calculator computes the credit from the local rule.
    """
    endorsement = endorsement_date or min(closing_date + timedelta(days=30), AS_OF_DATE)
    return dict(
        fha_endorsement_date=endorsement,
        prior_fha_closing_date=closing_date,
        transaction_type="REFI_STREAMLINE",
        prior_insurance_termination_reason="REFINANCE_PAYOFF",
        prior_ufmip_paid=round(balance * fha_mip_rates(endorsement)["ufmip_rate"], 2),
    )


def _base_loan(rng: random.Random) -> Loan:
    """One ordinary loan drawn from the main band (or legacy tail)."""
    program = "VA" if rng.random() < 0.60 else "FHA"
    legacy = rng.random() < 0.10
    if legacy:
        note_rate = _snap_rate(rng.uniform(2.250, 4.875))
    else:
        note_rate = _snap_rate(rng.triangular(5.250, 7.875, 6.500))

    balance = float(round(rng.uniform(150_000, 550_000), -2))
    original_term_months = 360 if rng.random() < 0.85 else 180
    months_aged = rng.randint(7, min(140, original_term_months - 24))
    remaining_term_months = max(60, original_term_months - months_aged)

    # Seasoning variance: a deliberate slice fails the 210-day gate.
    if rng.random() < 0.15:
        days_seasoned = rng.randint(120, 209)
    else:
        days_seasoned = rng.randint(210, 2400)
    first_payment_date = _date_minus_days(days_seasoned)
    closing_date = _date_minus_days(days_seasoned + 30)

    payments_made = max(0, days_seasoned // 30)
    consecutive_on_time = payments_made if rng.random() < 0.9 else rng.randint(0, 5)

    lates_roll = rng.random()
    lates_30_in_6mo = 0 if lates_roll < 0.85 else (1 if lates_roll < 0.95 else 2)

    product_type = "arm" if rng.random() < 0.05 else "fixed"
    occ_roll = rng.random()
    occupancy = "owner" if occ_roll < 0.75 else ("prior" if occ_roll < 0.9 else "non_owner")

    tags: list[str] = []
    if legacy:
        tags.append(TAG_LEGACY_TAIL)

    fha_case_assignment_date = None
    fha_fields: dict = dict(
        fha_endorsement_date=None, prior_fha_closing_date=None, transaction_type=None,
        prior_insurance_termination_reason=None, prior_ufmip_paid=None,
    )
    if program == "FHA":
        # Case assignment belongs to the NEW transaction, so it sits just behind
        # the as-of date; everything else is anchored on the prior loan's closing.
        fha_case_assignment_date = _date_minus_days(rng.randint(0, 40))
        fha_fields = _fha_contract_fields(
            closing_date,
            balance,
            endorsement_date=min(closing_date + timedelta(days=rng.randint(15, 60)), AS_OF_DATE),
        )

    return Loan(
        loan_id="",
        program=program,
        note_rate=note_rate,
        balance=balance,
        original_term_months=original_term_months,
        remaining_term_months=remaining_term_months,
        first_payment_date=first_payment_date,
        closing_date=closing_date,
        payments_made=payments_made,
        consecutive_on_time=consecutive_on_time,
        lates_30_in_6mo=lates_30_in_6mo,
        product_type=product_type,
        occupancy=occupancy,
        has_second_lien=rng.random() < 0.10,
        borrower_change_pending=rng.random() < 0.05,
        va_funding_fee_exempt=(program == "VA" and rng.random() < 0.25),
        fha_case_assignment_date=fha_case_assignment_date,
        escrowed=rng.random() < 0.7,
        tags=tuple(tags),
        **fha_fields,
    )


def _seeded_edge_cases(rng: random.Random) -> list[Loan]:
    """Guaranteed, tagged edge-case loans so every stratum is always present."""
    seasoned_first_pay = _date_minus_days(420)
    seasoned_closing = _date_minus_days(450)
    recent_case = _date_minus_days(10)

    def base(**kw) -> Loan:
        defaults = dict(
            loan_id="", program="VA", note_rate=7.000, balance=300_000.0,
            original_term_months=360, remaining_term_months=336,
            first_payment_date=seasoned_first_pay, closing_date=seasoned_closing,
            payments_made=14, consecutive_on_time=14, lates_30_in_6mo=0,
            product_type="fixed", occupancy="owner", has_second_lien=False,
            borrower_change_pending=False, va_funding_fee_exempt=False,
            fha_endorsement_date=None, fha_case_assignment_date=None,
            prior_fha_closing_date=None, escrowed=True, tags=(),
            transaction_type=None, prior_ufmip_paid=None,
            prior_insurance_termination_reason=None, fhac_refund_credit=None,
        )
        defaults.update(kw)
        return Loan(**defaults)

    def fha(**kw) -> Loan:
        """An FHA row whose UFMIP contract fields stay coherent with its dates."""
        closing = kw.get("closing_date", seasoned_closing)
        balance = kw.get("balance", 300_000.0)
        contract = _fha_contract_fields(
            closing, balance, endorsement_date=kw.pop("endorsement_date", None)
        )
        fields = dict(program="FHA", fha_case_assignment_date=recent_case)
        fields.update(contract)
        fields.update(kw)
        return base(**fields)

    return [
        base(product_type="arm", tags=(TAG_VA_ARM_TO_FIXED,)),
        base(remaining_term_months=336, tags=(TAG_TERM_REDUCTION,)),
        # Pre-2009 legacy: a genuinely old loan, so its closing precedes the
        # 2009-01-15 endorsement that earns the 0.01% streamline rate.
        fha(closing_date=date(2008, 12, 10), endorsement_date=date(2009, 1, 15),
            first_payment_date=date(2009, 2, 1), payments_made=209,
            consecutive_on_time=209, remaining_term_months=151,
            tags=(TAG_FHA_PRE_2009,)),
        # Inside the 3-year UFMIP refund window (closed 400 days ago).
        fha(closing_date=_date_minus_days(400), first_payment_date=_date_minus_days(370),
            tags=(TAG_FHA_REFI_WITHIN_3YR,)),
        base(has_second_lien=True, tags=(TAG_SECOND_LIEN,)),
        fha(borrower_change_pending=True, tags=(TAG_BORROWER_REMOVAL,)),
        base(va_funding_fee_exempt=True, tags=(TAG_VA_FEE_EXEMPT,)),
        base(lates_30_in_6mo=1, tags=(TAG_PAYMENT_DEFECT,)),
        base(lates_30_in_6mo=2, tags=(TAG_PAYMENT_DEFECT,)),
        fha(occupancy="non_owner", tags=(TAG_NON_OWNER_FHA,)),
        # Seasoning boundary pairs (both programs, just on either side of the gate).
        base(first_payment_date=_date_minus_days(209), payments_made=8,
             consecutive_on_time=8, tags=(TAG_SEASONING_BOUNDARY,)),
        base(first_payment_date=_date_minus_days(210), payments_made=8,
             consecutive_on_time=8, tags=(TAG_SEASONING_BOUNDARY,)),
        base(payments_made=5, consecutive_on_time=5, tags=(TAG_SEASONING_BOUNDARY,)),
        base(payments_made=6, consecutive_on_time=6, tags=(TAG_SEASONING_BOUNDARY,)),
        fha(closing_date=_date_minus_days(209), fha_case_assignment_date=AS_OF_DATE,
            payments_made=8, tags=(TAG_SEASONING_BOUNDARY,)),
    ]


def generate_pool(seed: int = DEFAULT_SEED, n: int = DEFAULT_POOL_SIZE) -> list[Loan]:
    """Generate a deterministic synthetic pool of ``n`` loans for ``seed``."""
    rng = random.Random(seed)
    loans = _seeded_edge_cases(rng)
    while len(loans) < n:
        loans.append(_base_loan(rng))
    return [replace(loan, loan_id=f"L{idx:05d}") for idx, loan in enumerate(loans)]


# --- Persistence -----------------------------------------------------------

_LOANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS loans (
    loan_id TEXT PRIMARY KEY,
    program TEXT NOT NULL,
    note_rate REAL NOT NULL,
    balance REAL NOT NULL,
    original_term_months INTEGER NOT NULL,
    remaining_term_months INTEGER NOT NULL,
    first_payment_date TEXT NOT NULL,
    closing_date TEXT NOT NULL,
    payments_made INTEGER NOT NULL,
    consecutive_on_time INTEGER NOT NULL,
    lates_30_in_6mo INTEGER NOT NULL,
    product_type TEXT NOT NULL,
    occupancy TEXT NOT NULL,
    has_second_lien INTEGER NOT NULL,
    borrower_change_pending INTEGER NOT NULL,
    va_funding_fee_exempt INTEGER NOT NULL,
    fha_endorsement_date TEXT,
    fha_case_assignment_date TEXT,
    prior_fha_closing_date TEXT,
    escrowed INTEGER NOT NULL,
    tags TEXT NOT NULL,
    transaction_type TEXT,
    prior_ufmip_paid REAL,
    prior_insurance_termination_reason TEXT,
    fhac_refund_credit REAL
);
"""

# Column count of the current schema. A database written before the FHA UFMIP
# contract landed has fewer, and load_pool reports it empty so callers regenerate.
_LOAN_COLUMNS = 25


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _to_row(loan: Loan) -> tuple:
    return (
        loan.loan_id, loan.program, loan.note_rate, loan.balance,
        loan.original_term_months, loan.remaining_term_months,
        _iso(loan.first_payment_date), _iso(loan.closing_date),
        loan.payments_made, loan.consecutive_on_time, loan.lates_30_in_6mo,
        loan.product_type, loan.occupancy, int(loan.has_second_lien),
        int(loan.borrower_change_pending), int(loan.va_funding_fee_exempt),
        _iso(loan.fha_endorsement_date), _iso(loan.fha_case_assignment_date),
        _iso(loan.prior_fha_closing_date), int(loan.escrowed), ",".join(loan.tags),
        loan.transaction_type, loan.prior_ufmip_paid,
        loan.prior_insurance_termination_reason, loan.fhac_refund_credit,
    )


def _from_row(row: tuple) -> Loan:
    def d(value):
        return date.fromisoformat(value) if value else None

    return Loan(
        loan_id=row[0], program=row[1], note_rate=row[2], balance=row[3],
        original_term_months=row[4], remaining_term_months=row[5],
        first_payment_date=date.fromisoformat(row[6]), closing_date=date.fromisoformat(row[7]),
        payments_made=row[8], consecutive_on_time=row[9], lates_30_in_6mo=row[10],
        product_type=row[11], occupancy=row[12], has_second_lien=bool(row[13]),
        borrower_change_pending=bool(row[14]), va_funding_fee_exempt=bool(row[15]),
        fha_endorsement_date=d(row[16]), fha_case_assignment_date=d(row[17]),
        prior_fha_closing_date=d(row[18]), escrowed=bool(row[19]),
        tags=tuple(t for t in row[20].split(",") if t),
        transaction_type=row[21], prior_ufmip_paid=row[22],
        prior_insurance_termination_reason=row[23], fhac_refund_credit=row[24],
    )


def _table_columns(conn) -> int:
    return len(list(conn.execute("PRAGMA table_info(loans)")))


def persist_pool(conn, loans: list[Loan]) -> int:
    """Replace the loans table contents with ``loans`` (idempotent for a seed).

    Drops first rather than DELETEing, so a database carrying the pre-UFMIP-contract
    schema is migrated in place instead of failing on the wider insert.
    """
    conn.executescript("DROP TABLE IF EXISTS loans;" + _LOANS_SCHEMA)
    conn.executemany(
        "INSERT INTO loans VALUES (" + ",".join(["?"] * _LOAN_COLUMNS) + ")",
        [_to_row(loan) for loan in loans],
    )
    conn.commit()
    return len(loans)


def load_pool(conn) -> list[Loan]:
    """Load the persisted pool, or an empty list if none has been generated.

    An older, narrower loans table also reads as empty: every caller regenerates
    from the seed when this returns nothing, which is the migration path.
    """
    conn.executescript(_LOANS_SCHEMA)
    if _table_columns(conn) != _LOAN_COLUMNS:
        return []
    cur = conn.execute("SELECT * FROM loans ORDER BY loan_id")
    return [_from_row(row) for row in cur.fetchall()]


def generate_and_persist(seed: int = DEFAULT_SEED, n: int = DEFAULT_POOL_SIZE, db_path=db.DEFAULT_DB_PATH) -> int:
    """Generate a pool for ``seed`` and write it to the database. Returns row count."""
    loans = generate_pool(seed, n)
    conn = db.connect(db_path)
    try:
        return persist_pool(conn, loans)
    finally:
        conn.close()
