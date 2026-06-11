"""Shared test fixtures: the fake FRED response, and core loan/scenario factories."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.core.config.assumptions import AS_OF_DATE
from src.core.models import Loan, Scenario


def _ago(days: int) -> date:
    return AS_OF_DATE - timedelta(days=days)


def make_loan(**overrides) -> Loan:
    """A well-seasoned VA fixed loan that passes every gate by default.

    Override individual fields to drive a single rule to its boundary.
    """
    defaults = dict(
        loan_id="T0001", program="VA", note_rate=7.000, balance=300_000.0,
        original_term_months=360, remaining_term_months=336,
        first_payment_date=_ago(420), closing_date=_ago(450),
        payments_made=14, consecutive_on_time=14, lates_30_in_6mo=0,
        product_type="fixed", occupancy="owner", has_second_lien=False,
        borrower_change_pending=False, va_funding_fee_exempt=False,
        fha_endorsement_date=None, fha_case_assignment_date=None,
        prior_fha_closing_date=None, escrowed=False, tags=(),
    )
    defaults.update(overrides)
    return Loan(**defaults)


def make_scenario(loan: Loan, new_note_rate: float, **overrides) -> Scenario:
    """Build a Scenario with explicit monthly figures for precise boundary tests.

    Defaults: a small P&I reduction, no MIP, no term reduction — so each test can
    isolate exactly one dimension.
    """
    defaults = dict(
        new_product_type="fixed",
        new_term_months=loan.remaining_term_months,  # no term reduction by default
        new_balance=loan.balance,
        old_monthly_PI=2000.0, new_monthly_PI=1900.0,
        old_monthly_MIP=0.0, new_monthly_MIP=0.0,
        old_annual_mip_rate=0.0, new_annual_mip_rate=0.0,
        agency_recoupment_cost=1000.0, economic_total_cost=4000.0,
        excluded_prepaids_escrow_cost=1200.0,
    )
    defaults.update(overrides)
    return Scenario(loan=loan, new_note_rate=new_note_rate, **defaults)


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def sample_payload() -> dict:
    """A FRED observations payload with a normal value and a missing one."""
    return {
        "observations": [
            {"date": "2026-01-02", "value": "4.21"},
            {"date": "2026-01-03", "value": "."},  # missing -> None
            {"date": "2026-01-06", "value": "4.25"},
        ]
    }
