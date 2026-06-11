"""Trigger ladder — CC-BRIEF-03 §7, extended for the dashboard (CC-BRIEF-04).

Sweeps candidate trigger rates down from the current market rate and, at each rung,
counts loans that are agency-clear AND economically-clear. Call-clear flag counts and
the VA/FHA split are carried alongside, never merged into the eligible count. The three
layers stay distinct (docs/domain-rules.md §1).

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
    newly_eligible: int          # loans first clearing at this rung (cumulative delta)
    eligible_count: int
    cumulative_count: int
    eligible_va: int
    eligible_fha: int
    median_statutory_recoupment: float | None
    median_break_even: float | None
    # Call-clear flag counts among eligible loans — carried alongside, not merged.
    soft_blocker_count: int
    unknown_count: int
    hard_blocker_count: int


def newly_eligible_from_cumulative(cumulatives: list[int]) -> list[int]:
    """Per-rung newly-eligible counts as successive differences of cumulative counts.

    The first rung's delta equals its own cumulative; each later rung is the increase
    over the previous rung. Sum of deltas equals the final cumulative.
    """
    deltas: list[int] = []
    prev = 0
    for cumulative in cumulatives:
        deltas.append(cumulative - prev)
        prev = cumulative
    return deltas


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


def evaluate_loan(
    loan: Loan,
    new_rate: float,
    *,
    cost_pct: float = A.RECOUPMENT_COST_PCT_BASE,
    threshold_months: int = A.BREAK_EVEN_THRESHOLD_MONTHS,
):
    """Run all three layers for one loan at one new rate. Returns a small tuple."""
    scenario = economics.build_scenario(loan, new_rate, cost_pct=cost_pct)
    if loan.program == "VA":
        agency = rules_va.agency_clear(loan, scenario)
        recoupment = rules_va.va_statutory_recoupment_months(scenario)
    else:
        agency = rules_fha.agency_clear(loan, scenario)
        recoupment = math.inf
    economic = economics.economically_clear(scenario, threshold_months=threshold_months)
    call = callclear.evaluate(loan, scenario, agency)
    break_even = economics.economic_break_even_months(scenario)
    eligible = agency.clear and economic.clear
    return eligible, recoupment, break_even, call


def build_ladder(
    loans: list[Loan],
    conn,
    *,
    cost_pct: float = A.RECOUPMENT_COST_PCT_BASE,
    threshold_months: int = A.BREAK_EVEN_THRESHOLD_MONTHS,
    step: float = A.LADDER_STEP,
    rate_range: float = A.LADDER_RANGE,
) -> tuple[list[LadderRung], float, float]:
    """Build the full ladder. Returns (rungs, current_va, current_fha)."""
    current_va, current_fha = current_market_rates(conn)
    start = round(round(current_va / step) * step, 3)
    n_steps = int(round(rate_range / step))

    # Precompute each loan's rate-independent base once, then only re-price the new
    # P&I per rung — the ladder evaluates tens of thousands of loan-rung scenarios.
    bases = [economics.precompute_base(loan, cost_pct=cost_pct) for loan in loans]

    raw: list[dict] = []
    cumulative_ids: set[str] = set()

    for k in range(n_steps + 1):
        trigger = round(start - k * step, 3)
        eligible_ids: list[str] = []
        va = fha = soft = unknown = hard = 0
        recoupments: list[float] = []
        break_evens: list[float] = []

        for loan, base in zip(loans, bases):
            new_rate = new_rate_for(loan, trigger, current_va, current_fha)
            scenario = economics.scenario_at_rate(loan, base, new_rate)
            if loan.program == "VA":
                agency = rules_va.agency_clear(loan, scenario)
                recoupment = rules_va.va_statutory_recoupment_months(scenario)
            else:
                agency = rules_fha.agency_clear(loan, scenario)
                recoupment = math.inf
            economic = economics.economically_clear(scenario, threshold_months=threshold_months)
            call = callclear.evaluate(loan, scenario, agency)
            break_even = economics.economic_break_even_months(scenario)
            eligible = agency.clear and economic.clear
            if eligible:
                eligible_ids.append(loan.loan_id)
                if loan.program == "VA":
                    va += 1
                else:
                    fha += 1
                soft += len(call.soft_blockers)
                unknown += len(call.unknowns)
                hard += len(call.hard_blockers)
                if math.isfinite(recoupment):
                    recoupments.append(recoupment)
                if math.isfinite(break_even):
                    break_evens.append(break_even)

        cumulative_ids |= set(eligible_ids)
        raw.append(
            {
                "trigger": trigger,
                "distance": round(current_va - trigger, 3),
                "eligible": len(eligible_ids),
                "cumulative": len(cumulative_ids),
                "va": va,
                "fha": fha,
                "med_recoup": round(median(recoupments), 1) if recoupments else None,
                "med_be": round(median(break_evens), 1) if break_evens else None,
                "soft": soft,
                "unknown": unknown,
                "hard": hard,
            }
        )

    deltas = newly_eligible_from_cumulative([r["cumulative"] for r in raw])
    rungs = [
        LadderRung(
            trigger_rate=r["trigger"],
            distance_from_market=r["distance"],
            newly_eligible=delta,
            eligible_count=r["eligible"],
            cumulative_count=r["cumulative"],
            eligible_va=r["va"],
            eligible_fha=r["fha"],
            median_statutory_recoupment=r["med_recoup"],
            median_break_even=r["med_be"],
            soft_blocker_count=r["soft"],
            unknown_count=r["unknown"],
            hard_blocker_count=r["hard"],
        )
        for r, delta in zip(raw, deltas)
    ]
    return rungs, current_va, current_fha
