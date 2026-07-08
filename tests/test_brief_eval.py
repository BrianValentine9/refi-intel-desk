"""Morning brief snapshot, template generation, and eval harness tests."""

from __future__ import annotations

from src.brief.generate import generate_brief, render_template_brief
from src.brief.snapshot import build_snapshot
from src.core import pool
from src.data import db
from src.data.fred_client import Observation
from evals.verify import verify_brief


def _seed_db(tmp_path):
    conn = db.connect(tmp_path / "brief.db")
    obs = [
        Observation("DGS10", "2026-06-09", 4.25),
        Observation("DGS10", "2026-06-02", 4.30),
        Observation("OBMMIVA30YF", "2026-06-09", 6.131),
        Observation("OBMMIVA30YF", "2026-06-02", 6.200),
        Observation("OBMMIFHA30YF", "2026-06-09", 6.284),
        Observation("OBMMIFHA30YF", "2026-06-02", 6.350),
        Observation("OBMMIC30YF", "2026-06-09", 6.400),
        Observation("OBMMIC30YF", "2026-06-02", 6.450),
    ]
    db.upsert_observations(conn, obs)
    pool.persist_pool(conn, pool.generate_pool(n=200))
    return conn


def test_template_brief_passes_eval(tmp_path):
    conn = _seed_db(tmp_path)
    try:
        snapshot = build_snapshot(conn, cost_pct=0.01, threshold_months=48, seed=pool.DEFAULT_SEED)
        brief = render_template_brief(snapshot)
        result = verify_brief(brief, snapshot)
        assert result.passed, result.errors
    finally:
        conn.close()


def test_generate_brief_template_mode(tmp_path):
    conn = _seed_db(tmp_path)
    try:
        snapshot = build_snapshot(conn, cost_pct=0.01, threshold_months=48)
        brief, source = generate_brief(snapshot, mode="template")
        assert source == "template"
        assert "Treasury" in brief
        assert verify_brief(brief, snapshot).passed
    finally:
        conn.close()


def test_eval_catches_invented_rate(tmp_path):
    conn = _seed_db(tmp_path)
    try:
        snapshot = build_snapshot(conn, cost_pct=0.01, threshold_months=48)
        bad = "Market VA rate is 9.999% on a synthetic pool."
        result = verify_brief(bad, snapshot)
        assert not result.passed
        assert any("9.999" in err for err in result.errors)
    finally:
        conn.close()
