"""Trigger ladder tests — monotonic counts, range, program-basis pricing."""

from __future__ import annotations

from src.core import ladder, pool
from src.data import db
from src.data.fred_client import Observation
from tests.conftest import make_loan


def _db_with_rates(tmp_path):
    conn = db.connect(tmp_path / "ladder.db")
    db.upsert_observations(
        conn,
        [
            Observation("OBMMIVA30YF", "2026-06-09", 6.131),
            Observation("OBMMIFHA30YF", "2026-06-09", 6.284),
        ],
    )
    return conn


def test_cumulative_and_eligible_counts_are_monotonic(tmp_path):
    conn = _db_with_rates(tmp_path)
    rungs, _, _ = ladder.build_ladder(pool.generate_pool(n=300), conn)
    conn.close()
    cumulative = [r.cumulative_count for r in rungs]
    eligible = [r.eligible_count for r in rungs]
    assert cumulative == sorted(cumulative)  # non-decreasing as triggers descend
    assert eligible == sorted(eligible)


def test_ladder_spans_configured_range(tmp_path):
    conn = _db_with_rates(tmp_path)
    rungs, current_va, _ = ladder.build_ladder(pool.generate_pool(n=100), conn)
    conn.close()
    assert len(rungs) == int(round(2.00 / 0.125)) + 1
    assert rungs[0].distance_from_market < rungs[-1].distance_from_market
    assert rungs[0].trigger_rate > rungs[-1].trigger_rate


def test_new_rate_preserves_program_basis():
    va = make_loan(program="VA")
    fha = make_loan(program="FHA")
    assert ladder.new_rate_for(va, 6.000, 6.131, 6.284) == 6.000
    # FHA priced at the trigger plus the observed FHA-over-VA basis spread.
    assert ladder.new_rate_for(fha, 6.000, 6.131, 6.284) == round(6.000 + (6.284 - 6.131), 3)


def test_newly_eligible_from_cumulative():
    assert ladder.newly_eligible_from_cumulative([3, 3, 7, 10]) == [3, 0, 4, 3]
    assert ladder.newly_eligible_from_cumulative([]) == []
    cumulative = [2, 5, 5, 9]
    deltas = ladder.newly_eligible_from_cumulative(cumulative)
    assert deltas[0] == cumulative[0]  # first rung delta == its cumulative
    assert sum(deltas) == cumulative[-1]  # deltas sum to the final cumulative


def test_build_ladder_deltas_and_program_split(tmp_path):
    conn = _db_with_rates(tmp_path)
    rungs, _, _ = ladder.build_ladder(pool.generate_pool(n=300), conn)
    conn.close()
    assert rungs[0].newly_eligible == rungs[0].cumulative_count
    assert sum(r.newly_eligible for r in rungs) == rungs[-1].cumulative_count
    for rung in rungs:
        assert rung.eligible_va + rung.eligible_fha == rung.eligible_count


def test_threshold_threading_tightens_eligibility(tmp_path):
    conn = _db_with_rates(tmp_path)
    loans = pool.generate_pool(n=300)
    strict, _, _ = ladder.build_ladder(loans, conn, threshold_months=1)
    loose, _, _ = ladder.build_ladder(loans, conn, threshold_months=48)
    conn.close()
    # A 1-month break-even is essentially unreachable, so far fewer loans clear.
    assert strict[-1].eligible_count < loose[-1].eligible_count


def test_missing_rates_raise(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    try:
        import pytest

        with pytest.raises(RuntimeError, match="Rate indices"):
            ladder.build_ladder(pool.generate_pool(n=50), conn)
    finally:
        conn.close()
