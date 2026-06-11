"""Call-clear (Layer 3) tests — flag categorization."""

from __future__ import annotations

from datetime import date

from src.core import callclear
from src.core.config.assumptions import AS_OF_DATE
from src.core.models import ClearanceResult
from tests.conftest import make_loan, make_scenario


def test_second_lien_is_an_unknown():
    loan = make_loan(has_second_lien=True)
    result = callclear.evaluate(loan, make_scenario(loan, 6.0), ClearanceResult(clear=True))
    assert "subordination_unknown" in result.unknowns


def test_seasoning_failure_is_a_hard_blocker():
    loan = make_loan()
    agency = ClearanceResult(clear=False, failed=["seasoning_210_days"])
    result = callclear.evaluate(loan, make_scenario(loan, 6.0), agency)
    assert "seasoning_defect" in result.hard_blockers
    assert result.clear is False


def test_two_lates_is_a_hard_blocker():
    loan = make_loan(lates_30_in_6mo=2)
    result = callclear.evaluate(loan, make_scenario(loan, 6.0), ClearanceResult(clear=True))
    assert "payment_history_defect" in result.hard_blockers


def test_fha_always_carries_manual_underwrite_unknown():
    loan = make_loan(program="FHA", fha_endorsement_date=date(2015, 1, 1),
                     fha_case_assignment_date=AS_OF_DATE)
    result = callclear.evaluate(loan, make_scenario(loan, 6.0), ClearanceResult(clear=True))
    assert "manual_underwrite_path" in result.unknowns


def test_term_reset_is_a_soft_blocker():
    loan = make_loan(remaining_term_months=200)
    scen = make_scenario(loan, 6.0, new_term_months=360)
    result = callclear.evaluate(loan, scen, ClearanceResult(clear=True))
    assert "term_reset_optics" in result.soft_blockers


def test_clean_loan_is_call_clear():
    loan = make_loan()  # no second lien, no lates, owner-occupied, well seasoned
    result = callclear.evaluate(loan, make_scenario(loan, 6.0), ClearanceResult(clear=True))
    assert result.clear is True
    assert result.hard_blockers == []
