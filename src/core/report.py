"""Proof-of-life report — `python -m src.core.report`.

Loads (or generates) the synthetic pool, reads the latest rates from the DB, and
prints the trigger ladder plus headline stats. No UI yet; this is the command that
proves the analysis core works end to end.
"""

from __future__ import annotations

import argparse

from src.data import db

from . import economics, ladder, pool, rules_fha, rules_va
from .config import assumptions as A
from .models import Loan

_SEASONING_REASONS = {
    "seasoning_210_days", "seasoning_six_payments",
    "fha_seasoning_six_payments", "fha_seasoning_six_months", "fha_seasoning_210_days",
}


def _fails_seasoning(loan: Loan) -> bool:
    """Seasoning is rate-independent; price at a low rate and inspect the reasons."""
    scenario = economics.build_scenario(loan, max(0.5, loan.note_rate - 1.0))
    agency = (
        rules_va.agency_clear(loan, scenario)
        if loan.program == "VA"
        else rules_fha.agency_clear(loan, scenario)
    )
    return any(reason in _SEASONING_REASONS for reason in agency.failed)


def _load_or_generate(conn, seed: int, size: int, regenerate: bool) -> list[Loan]:
    loans = pool.load_pool(conn)
    if regenerate or not loans:
        pool.persist_pool(conn, pool.generate_pool(seed, size))
        loans = pool.load_pool(conn)
    return loans


def _print_ladder(rungs: list[ladder.LadderRung]) -> None:
    header = (
        f"{'trigger':>8}{'dist':>7}{'eligible':>10}{'cumul':>8}"
        f"{'med_recoup':>12}{'med_BE':>9}{'soft':>7}{'unknown':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in rungs:
        recoup = f"{r.median_statutory_recoupment:.1f}" if r.median_statutory_recoupment is not None else "-"
        be = f"{r.median_break_even:.1f}" if r.median_break_even is not None else "-"
        print(
            f"{r.trigger_rate:>8.3f}{r.distance_from_market:>7.3f}{r.eligible_count:>10}"
            f"{r.cumulative_count:>8}{recoup:>12}{be:>9}{r.soft_blocker_count:>7}{r.unknown_count:>9}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.core.report",
        description="Print the refi trigger ladder for the synthetic pool.",
    )
    parser.add_argument("--seed", type=int, default=pool.DEFAULT_SEED)
    parser.add_argument("--pool-size", type=int, default=pool.DEFAULT_POOL_SIZE)
    parser.add_argument("--regenerate", action="store_true", help="Rebuild the pool even if one exists.")
    args = parser.parse_args(argv)

    conn = db.connect()
    try:
        loans = _load_or_generate(conn, args.seed, args.pool_size, args.regenerate)
        rungs, current_va, current_fha = ladder.build_ladder(loans, conn)
    finally:
        conn.close()

    seasoning_failures = sum(_fails_seasoning(loan) for loan in loans)
    legacy = [loan for loan in loans if pool.TAG_LEGACY_TAIL in loan.tags]
    market_trigger = rungs[0].trigger_rate  # current market — the realistic "today"
    legacy_fail_l2 = 0
    for loan in legacy:
        rate = ladder.new_rate_for(loan, market_trigger, current_va, current_fha)
        scenario = economics.build_scenario(loan, rate)
        if not economics.economically_clear(scenario).clear:
            legacy_fail_l2 += 1

    print("refi-intel-desk - trigger ladder")
    print(f"as-of {A.AS_OF_DATE.isoformat()}  |  seed {args.seed}")
    print(f"current market: VA {current_va:.3f}%  FHA {current_fha:.3f}%  (latest DB index)\n")
    _print_ladder(rungs)
    print()
    print(f"pool size                         {len(loans)}")
    print(
        f"failing seasoning (Layer 1)       {seasoning_failures} "
        f"({seasoning_failures / len(loans):.1%})"
    )
    if legacy:
        print(
            f"legacy tail failing Layer 2       {legacy_fail_l2}/{len(legacy)} "
            f"({legacy_fail_l2 / len(legacy):.1%}) at current market"
        )


if __name__ == "__main__":
    main()
