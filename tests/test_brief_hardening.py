"""Brief path hardening: safe rung selection, LLM fallback, value-based checker."""
from __future__ import annotations

import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evals.verify import verify_brief
from src.brief import generate
from src.brief.snapshot import build_snapshot, select_rung
from src.core import ladder, pool
from tests.test_brief_eval import _seed_db


@pytest.fixture
def snap_and_rungs(tmp_path):
    conn = _seed_db(tmp_path)
    try:
        loans = pool.load_pool(conn) or pool.generate_pool(pool.DEFAULT_SEED)
        rungs, _va, _fha = ladder.build_ladder(loans, conn, cost_pct=0.01, threshold_months=48)
        snap = build_snapshot(conn, cost_pct=0.01, threshold_months=48)
        yield conn, snap, rungs
    finally:
        conn.close()


# ---- snapshot selection -------------------------------------------------

def test_rungs_reused_without_second_ladder(snap_and_rungs, monkeypatch):
    conn, base, rungs = snap_and_rungs
    monkeypatch.setattr(ladder, "build_ladder", lambda *a, **k: pytest.fail("ladder rebuilt"))
    monkeypatch.setattr(pool, "load_pool", lambda *a, **k: pytest.fail("pool loaded"))
    snap = build_snapshot(conn, cost_pct=0.01, threshold_months=48, rungs=rungs, pool_size=200)
    assert snap.pool_size == 200 and snap.market_rung.trigger_rate == rungs[0].trigger_rate
    cheap = build_snapshot(conn, cost_pct=0.01, threshold_months=48, rungs=rungs)
    assert cheap.pool_size == 200  # COUNT(*) over the persisted loans


def test_select_by_index_and_trigger(snap_and_rungs):
    conn, _base, rungs = snap_and_rungs
    s = build_snapshot(conn, cost_pct=0.01, threshold_months=48, rungs=rungs, selected_index=3)
    assert s.selected_rung.trigger_rate == rungs[3].trigger_rate
    t = build_snapshot(conn, cost_pct=0.01, threshold_months=48, rungs=rungs,
                       selected_trigger=rungs[2].trigger_rate + 0.0004)
    assert t.selected_rung.trigger_rate == rungs[2].trigger_rate
    assert select_rung(rungs, selected_index=1, selected_trigger=999.0) is rungs[1]  # index wins


def test_select_errors_are_clear(snap_and_rungs):
    _conn, _snap, rungs = snap_and_rungs
    with pytest.raises(ValueError, match="out of range"):
        select_rung(rungs, selected_index=len(rungs))
    with pytest.raises(ValueError, match="12.345"):
        select_rung(rungs, selected_trigger=12.345)


# ---- generate_brief fallback --------------------------------------------

def _install_client(monkeypatch, behavior):
    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)
            if behavior == "ctor":
                raise RuntimeError("boom")
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kw):
            if behavior == "call":
                raise TimeoutError("slow")
            if behavior == "empty":
                return SimpleNamespace(content=[])
            if behavior == "nontext":
                return SimpleNamespace(content=[SimpleNamespace(type="tool_use")])
            return SimpleNamespace(content=[SimpleNamespace(text="  hello brief  ")])

    # The SDK may not be installed: inject a fake module (never a real client).
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeClient))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
    return seen


@pytest.mark.parametrize("behavior", ["ctor", "call", "empty", "nontext"])
def test_llm_failures_fall_back_to_template(snap_and_rungs, monkeypatch, behavior):
    _conn, snap, _rungs = snap_and_rungs
    _install_client(monkeypatch, behavior)
    text, source = generate.generate_brief(snap, mode="auto")
    assert source == "template" and text == generate.render_template_brief(snap)


def test_llm_success_and_client_limits(snap_and_rungs, monkeypatch):
    _conn, snap, _rungs = snap_and_rungs
    seen = _install_client(monkeypatch, "ok")
    assert generate.generate_brief(snap, mode="auto") == ("hello brief", "llm")
    assert seen["timeout"] == 20.0 and seen["max_retries"] == 1


# ---- checker ------------------------------------------------------------

def _brief_with(snap, sentence):
    return f"{sentence} This is a synthetic, modeled pool."


@pytest.mark.parametrize("date_text", [
    "2026-06-09", "June 9, 2026", "June 09, 2026", "Jun 9, 2026", "Jun. 9, 2026",
    "9 June 2026", "09 June 2026", "June 9",
])
def test_accepted_date_forms(snap_and_rungs, date_text):
    _conn, snap, _rungs = snap_and_rungs
    assert snap.as_of == "2026-06-09"
    result = verify_brief(_brief_with(snap, f"As of {date_text}, nothing else."), snap)
    assert result.passed and not any("as-of" in w for w in result.warnings)


@pytest.mark.parametrize("bad", ["June 10, 2026", "June 9, 2025", "June 19"])
def test_wrong_date_only_warns(snap_and_rungs, bad):
    _conn, snap, _rungs = snap_and_rungs
    result = verify_brief(_brief_with(snap, f"As of {bad}."), snap)
    assert result.passed
    assert any(f"as-of date {snap.as_of} not mentioned" in w for w in result.warnings)


def test_unsigned_delta_and_va_fha_sum(snap_and_rungs):
    _conn, snap, _rungs = snap_and_rungs
    snap = replace(snap, va_delta_7d=-0.069)
    ok = verify_brief(_brief_with(snap, "VA fell 0.069% and -0.069% as of 2026-06-09."), snap)
    assert ok.passed, ok.errors
    m = snap.market_rung
    total = m.eligible_va + m.eligible_fha
    ok = verify_brief(_brief_with(snap, f"{total:,} VA and FHA loans clear as of 2026-06-09."), snap)
    assert ok.passed, ok.errors


def test_wrong_rate_and_count_still_fail(snap_and_rungs):
    _conn, snap, _rungs = snap_and_rungs
    assert not verify_brief(_brief_with(snap, "VA is 9.999% as of 2026-06-09."), snap).passed
    allowed = snap.allowed_counts()
    wrong = next(n for n in range(1234, 99999) if n not in allowed)
    assert not verify_brief(_brief_with(snap, f"{wrong:,} loans clear as of 2026-06-09."), snap).passed
