"""VA IRRRL agency-clear golden boundary tests — domain-rules §2.1."""

from __future__ import annotations

from datetime import timedelta

from src.core import rules_va
from src.core.config.assumptions import AS_OF_DATE
from tests.conftest import make_loan, make_scenario


def _agency(loan, new_rate=6.0, **scen_kw):
    return rules_va.agency_clear(loan, make_scenario(loan, new_rate, **scen_kw))


# --- Seasoning: 209 vs 210 days, 5 vs 6 payments (domain-rules §2.1.3) ---

def test_seasoning_209_days_fails_210_passes():
    fail = _agency(make_loan(first_payment_date=AS_OF_DATE - timedelta(days=209)))
    assert "seasoning_210_days" in fail.failed
    ok = _agency(make_loan(first_payment_date=AS_OF_DATE - timedelta(days=210)))
    assert "seasoning_210_days" not in ok.failed


def test_seasoning_five_payments_fails_six_passes():
    assert "seasoning_six_payments" in _agency(make_loan(consecutive_on_time=5)).failed
    assert "seasoning_six_payments" not in _agency(make_loan(consecutive_on_time=6)).failed


# --- NTB: fixed->fixed 0.49 vs 0.50; fixed->ARM 1.99 vs 2.00 (domain-rules §2.1.4) ---

def test_ntb_fixed_to_fixed_threshold():
    loan = make_loan(note_rate=7.000)
    assert "ntb_not_met" in _agency(loan, new_rate=6.510).failed  # 0.49 drop
    assert "ntb_not_met" not in _agency(loan, new_rate=6.500).failed  # 0.50 drop


def test_ntb_fixed_to_arm_threshold():
    loan = make_loan(note_rate=7.000)
    assert "ntb_not_met" in _agency(loan, new_rate=5.010, new_product_type="arm").failed
    assert "ntb_not_met" not in _agency(loan, new_rate=5.000, new_product_type="arm").failed


def test_ntb_arm_to_fixed_passes_regardless_of_rate():
    # Architect ruling §7a: moving ARM->fixed is itself the tangible benefit, so
    # NTB passes even when the new fixed rate equals or exceeds the old ARM rate.
    loan = make_loan(note_rate=7.000, product_type="arm")
    for new_rate in (6.000, 7.000, 7.500):  # lower, equal, higher
        assert "ntb_not_met" not in _agency(loan, new_rate=new_rate, new_product_type="fixed").failed


def test_arm_to_fixed_higher_rate_governed_by_recoupment_backstop():
    # New fixed rate above the old ARM rate => new P&I >= old P&I, so the
    # zero-eligible-fee recoupment backstop (not NTB) decides agency clearance.
    loan = make_loan(note_rate=7.000, product_type="arm")
    common = dict(new_rate=7.500, new_product_type="fixed",
                  old_monthly_PI=1900.0, new_monthly_PI=2000.0)

    with_costs = _agency(loan, agency_recoupment_cost=1.0, **common)
    assert "ntb_not_met" not in with_costs.failed
    assert "recoupment_costs_without_pi_reduction" in with_costs.failed
    assert with_costs.clear is False

    zero_costs = _agency(loan, agency_recoupment_cost=0.0, **common)
    assert zero_costs.clear is True  # NTB ok + no eligible fees => agency-clear


# --- Recoupment: exactly 36.0 vs 36.1; zero-cost rule (domain-rules §2.1.5) ---

def test_recoupment_exactly_36_passes_361_fails():
    loan = make_loan()
    # reduction = 100/mo; 3600/100 = 36.0 (pass), 3610/100 = 36.1 (fail)
    ok = _agency(loan, old_monthly_PI=2000.0, new_monthly_PI=1900.0, agency_recoupment_cost=3600.0)
    assert "recoupment_over_36" not in ok.failed
    bad = _agency(loan, old_monthly_PI=2000.0, new_monthly_PI=1900.0, agency_recoupment_cost=3610.0)
    assert "recoupment_over_36" in bad.failed


def test_no_pi_reduction_requires_zero_eligible_cost():
    loan = make_loan()
    one_dollar = _agency(loan, old_monthly_PI=2000.0, new_monthly_PI=2000.0, agency_recoupment_cost=1.0)
    assert "recoupment_costs_without_pi_reduction" in one_dollar.failed
    zero = _agency(loan, old_monthly_PI=2000.0, new_monthly_PI=2000.0, agency_recoupment_cost=0.0)
    assert "recoupment_costs_without_pi_reduction" not in zero.failed


def test_statutory_recoupment_months_formula():
    loan = make_loan()
    scen = make_scenario(loan, 6.0, old_monthly_PI=2000.0, new_monthly_PI=1900.0,
                         agency_recoupment_cost=3600.0)
    assert rules_va.va_statutory_recoupment_months(scen) == 36.0


def test_fully_clean_loan_is_agency_clear():
    result = _agency(make_loan(), old_monthly_PI=2000.0, new_monthly_PI=1850.0,
                     agency_recoupment_cost=1500.0)
    assert result.clear is True
    assert result.failed == []
