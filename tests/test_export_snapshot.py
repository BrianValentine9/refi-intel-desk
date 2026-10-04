"""The landing snapshot export must equal the ladder core on the committed DB."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import export_landing_snapshot as ex
from src.core import ladder, pool
from src.data import db

SEED = Path("data") / "seed.db"
FROZEN_SEED = Path("tests") / "fixtures" / "seed_2026-07-29.db"  # pins the July-29 hero figures


def _require_seed():
    if not SEED.is_file():
        pytest.skip("data/seed.db not present")


def test_export_matches_core():
    _require_seed()
    snap = ex.build_snapshot(SEED)

    conn = db.connect(SEED, readonly=True)
    try:
        loans = pool.load_pool(conn)
        if not loans:
            loans = pool.generate_pool(pool.DEFAULT_SEED)
        rungs, _va, _fha = ladder.build_ladder(
            loans=loans, conn=conn, cost_pct=ex.COST_PCT, threshold_months=ex.THRESHOLD_MONTHS
        )
    finally:
        conn.close()

    assert len(snap["rungs"]) == len(rungs)
    for s, r in zip(snap["rungs"], rungs):
        assert s["rate"] == r.trigger_rate
        assert s["cumulative"] == r.cumulative_count
        assert s["new"] == r.newly_eligible
        assert s["recoup"] == r.median_statutory_recoupment
        assert s["be"] == r.median_break_even


def test_flip_reproduces_hero_figures():
    snap = ex.build_snapshot(FROZEN_SEED)  # opened read-only by build_snapshot
    assert snap["pool_size"] == 5000
    assert snap["flip_rate"] == 5.625
    flip = next(r for r in snap["rungs"] if r["flip"])
    # 2551 before FHA UFMIP moved onto the Handbook 4000.1 reference calculator:
    # every FHA row now carries a real refund clock, so the FHA cohort finances a
    # smaller net UFMIP and a handful of rungs shift.
    assert flip["cumulative"] == 2537
