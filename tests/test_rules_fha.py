"""FHA Streamline agency-clear golden boundary tests — domain-rules §2.2."""

from __future__ import annotations

from datetime import date

from src.core import rules_fha
from src.core.config.assumptions import AS_OF_DATE
from tests.conftest import make_loan, make_scenario


def fha_loan(**overrides):
    base = dict(
        program="FHA", note_rate=7.000,
        fha_endorsement_date=date(2015, 1, 1), fha_case_assignment_date=AS_OF_DATE,
    )
    base.update(overrides)
    return make_loan(**base)


def _agency(loan, new_rate=6.0, **scen_kw):
    return rules_fha.agency_clear(loan, make_scenario(loan, new_rate, **scen_kw))


# --- Combined-rate NTB: 0.49 vs 0.50 (domain-rules §2.2.4) ---

def test_combined_rate_drop_threshold():
    loan = fha_loan(note_rate=7.000)
    mip = dict(old_annual_mip_rate=0.0055, new_annual_mip_rate=0.0055)
    assert "ntb_combined_rate_050" in _agency(loan, 6.510, **mip).failed
    assert "ntb_combined_rate_050" not in _agency(loan, 6.500, **mip).failed


# --- Term-reduction path: new P+I+MIP +$50 vs +$51 (domain-rules §2.2.4) ---

def test_term_reduction_pimip_tolerance():
    loan = fha_loan(note_rate=7.000, remaining_term_months=336)
    common = dict(
        new_term_months=300,  # a term reduction
        old_annual_mip_rate=0.0055, new_annual_mip_rate=0.0055,
        old_monthly_PI=2000.0, old_monthly_MIP=100.0, new_monthly_MIP=100.0,
    )
    ok = _agency(loan, 6.5, new_monthly_PI=2050.0, **common)  # +$50 over old P+I+MIP
    assert "ntb_term_reduction" not in ok.failed
    bad = _agency(loan, 6.5, new_monthly_PI=2051.0, **common)  # +$51
    assert "ntb_term_reduction" in bad.failed


# --- Seasoning at case assignment (domain-rules §2.2.5) ---

def test_fha_seasoning_six_payments():
    assert "fha_seasoning_six_payments" in _agency(fha_loan(payments_made=5)).failed
    assert "fha_seasoning_six_payments" not in _agency(fha_loan(payments_made=6)).failed


def test_fha_seasoning_210_days_since_prior_closing():
    near = fha_loan(closing_date=AS_OF_DATE.replace(day=1))  # < 210 days before case assignment
    assert "fha_seasoning_210_days" in _agency(near).failed


# --- Payment history: one vs two 30-day lates (domain-rules §2.2.6) ---

def test_payment_history_one_vs_two_lates():
    assert "payment_history_lates" not in _agency(fha_loan(lates_30_in_6mo=1)).failed
    assert "payment_history_lates" in _agency(fha_loan(lates_30_in_6mo=2)).failed


# --- Non-owner-occupied must be fixed-rate only (domain-rules §2.2.9) ---

def test_non_owner_requires_fixed():
    loan = fha_loan(occupancy="non_owner")
    assert "non_owner_requires_fixed" in _agency(loan, new_product_type="arm").failed
    assert "non_owner_requires_fixed" not in _agency(loan, new_product_type="fixed").failed


def test_fully_clean_fha_loan_is_agency_clear():
    loan = fha_loan(note_rate=7.000)
    result = _agency(loan, 6.000, old_annual_mip_rate=0.0055, new_annual_mip_rate=0.0055)
    assert result.clear is True
    assert result.failed == []
