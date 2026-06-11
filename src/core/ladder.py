"""Trigger ladder — CC-BRIEF-03 §7.

Sweeps candidate trigger rates down from the current market rate and, at each rung,
counts loans that are agency-clear AND economically-clear. Call-clear flag counts are
carried alongside, never merged into the eligible count. The three layers stay
distinct (docs/domain-rules.md §1).

Pricing model (a synthetic modeling choice, not regulation): the trigger sweeps the
VA offered rate; FHA loans are priced at the trigger plus the observed FHA-over-VA
basis spread between OBMMIFHA30YF and OBMMIVA30YF. VA loans are evaluated against the
VA index, FHA against the FHA index.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

from src.data import db

from . import callclear, economics, rules_fha, rules_va
from .config import assumptions as A
from .models import Loan

VA_INDEX = "OBMMIVA30YF"
FHA_INDEX = "OBMMIFHA30YF"


@dataclass(frozen=True)
class LadderRung:
    trigger_rate: float
    distance_from_market: float
    eligible_count: int
    cumulative_count: int
    median_statutory_recoupment: float | None
    median_break_even: float | None
    # Call-clear flag counts among eligible loans — carried alongside, not merged.
    soft_blocker_count: int
    unknown_count: int
    hard_blocker_count: int


def current_market_rates(conn) -> tuple[float, float]:
    """Latest VA and FHA index values from the database."""
    va = db.latest_nonnull(conn, VA_INDEX)
    fha = db.latest_nonnull(conn, FHA_INDEX)
    if va is None or fha is None:
        raise RuntimeError("Rate indices not in DB — run `python -m src.data.ingest` first")
    return va[1], fha[1]


def new_rate_for(loan: Loan, trigger: float, current_va: float, current_fha: float) -> float:
    """Offered new note rate for a loan at a trigger, preserving program basis."""
    if loan.program == "VA":
        return round(trigger, 3)
    return round(trigger + (current_fha - current_va), 3)


def evaluate_loan(loan: Loan, new_rate: float):
    """Run all three layers for one loan at one new rate. Returns a small tuple."""
    scenario = economics.build_scenario(loan, new_rate)
    if loan.program == "VA":
        agency = rules_va.agency_clear(loan, scenario)
        recoupment = rules_va.va_statutory_recoupment_months(scenario)
    else:
        agency = rules_fha.agency_clear(loan, scenario)
        recoupment = math.inf
    economic = economics.economically_clear(scenario)
    call = callclear.evaluate(loan, scenario, agency)
    break_even = economics.economic_break_even_months(scenario)
    eligible = agency.clear and economic.clear
    return eligible, recoupment, break_even, call


def build_ladder(
    loans: list[Loan], conn, *, step: float = A.LADDER_STEP, rate_range: float = A.LADDER_RANGE
) -> tuple[list[LadderRung], float, float]:
    """Build the full ladder. Returns (rungs, current_va, current_fha)."""
    current_va, current_fha = current_market_rates(conn)
    start = round(round(current_va / step) * step, 3)
    n_steps = int(round(rate_range / step))

    rungs: list[LadderRung] = []
    cumulative_ids: set[str] = set()

    for k in range(n_steps + 1):
        trigger = round(start - k * step, 3)
        eligible_ids: list[str] = []
        recoupments: list[float] = []
        break_evens: list[float] = []
        soft = unknown = hard = 0

        for loan in loans:
            new_rate = new_rate_for(loan, trigger, current_va, current_fha)
            eligible, recoupment, break_even, call = evaluate_loan(loan, new_rate)
            if eligible:
                eligible_ids.append(loan.loan_id)
                soft += len(call.soft_blockers)
                unknown += len(call.unknowns)
                hard += len(call.hard_blockers)
                if math.isfinite(recoupment):
                    recoupments.append(recoupment)
                if math.isfinite(break_even):
                    break_evens.append(break_even)

        cumulative_ids |= set(eligible_ids)
        rungs.append(
            LadderRung(
                trigger_rate=trigger,
                distance_from_market=round(current_va - trigger, 3),
                eligible_count=len(eligible_ids),
                cumulative_count=len(cumulative_ids),
                median_statutory_recoupment=round(median(recoupments), 1) if recoupments else None,
                median_break_even=round(median(break_evens), 1) if break_evens else None,
                soft_blocker_count=soft,
                unknown_count=unknown,
                hard_blocker_count=hard,
            )
        )
    return rungs, current_va, current_fha
