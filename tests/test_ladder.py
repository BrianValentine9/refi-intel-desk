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


def test_missing_rates_raise(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    try:
        import pytest

        with pytest.raises(RuntimeError, match="Rate indices"):
            ladder.build_ladder(pool.generate_pool(n=50), conn)
    finally:
        conn.close()
