"""BriefGuard: scope, cache, daily cap, one call in flight, cool-down, per-IP limit. Fake generators only:
no Anthropic client is ever built for a paid call, and no key is read from the real environment."""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.api import brief_guard
from src.api.brief_guard import BriefGuard, BriefSettings, client_ip
from src.brief.snapshot import build_snapshot
from src.core import ladder, pool
from tests.test_brief_eval import _seed_db


@pytest.fixture(scope="module")
def snap(tmp_path_factory):
    conn = _seed_db(tmp_path_factory.mktemp("guard"))
    try:
        loans = pool.load_pool(conn) or pool.generate_pool(pool.DEFAULT_SEED)
        rungs, _a, _b = ladder.build_ladder(loans, conn, cost_pct=0.01, threshold_months=48)
        return build_snapshot(conn, cost_pct=0.01, threshold_months=48, rungs=rungs, selected_index=2)
    finally:
        conn.close()


def with_trigger(snap, rate):
    """Same as-of, a different selected trigger (a different cache key)."""
    return replace(snap, selected_rung=replace(snap.selected_rung, trigger_rate=rate))


def other_snap(snap):
    return with_trigger(snap, snap.selected_rung.trigger_rate - 0.125)


class Gen:
    """Fake generate_brief: returns text, an unusable 'template', or raises; may block on an Event."""

    def __init__(self, mode="text"):
        self.mode = mode
        self.calls = 0
        self.started = threading.Event()
        self.gate = threading.Event()
        self.gate.set()

    def __call__(self, snapshot, *, mode):
        self.calls += 1
        self.started.set()
        self.gate.wait(5)
        if self.mode == "raise":
            raise RuntimeError("secret-looking failure text")
        if self.mode == "template":  # generate_brief's own silent fallback
            return "fallback template", "template"
        if self.mode == "empty":
            return "   ", "llm"
        return f"AI text {self.calls}", "llm"


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(settings=None, gen=None, clock=None, key=True, **kw):
    gen = gen or Gen()
    clock = clock or Clock()
    g = BriefGuard(settings or BriefSettings(scope="all"), generate=gen, clock=clock, key_present=lambda: key, **kw)
    return g, gen, clock


def ask(g, snap, bp=100, thr=48, ip=None):
    return g.request(snap, cost_bp=bp, threshold=thr, ip_header_value=ip)


# ---- settings -----------------------------------------------------------------

def test_settings_defaults_and_invalid_values():
    d = BriefSettings.from_env({})
    assert (d.scope, d.daily_cap, d.cooldown_sec, d.ip_header, d.issues) == ("default_assumptions", 20, 900, None, ())
    bad = BriefSettings.from_env({"BRIEF_AI_SCOPE": "yes", "BRIEF_DAILY_CAP": "-3", "BRIEF_COOLDOWN_SEC": "abc"})
    assert (bad.scope, bad.daily_cap, bad.cooldown_sec) == ("default_assumptions", 0, 900)
    assert set(bad.issues) == {"BRIEF_AI_SCOPE", "BRIEF_DAILY_CAP", "BRIEF_COOLDOWN_SEC"}
    assert BriefSettings.from_env({"BRIEF_DAILY_CAP": "xyz"}).daily_cap == 0
    ok = BriefSettings.from_env({"BRIEF_AI_SCOPE": "OFF", "BRIEF_DAILY_CAP": "0", "BRIEF_IP_HEADER": " X-Forwarded-For "})
    assert (ok.scope, ok.daily_cap, ok.ip_header, ok.issues) == ("off", 0, "X-Forwarded-For", ())


# ---- scope and key ------------------------------------------------------------

def test_scope_default_assumptions(snap):
    g, gen, _ = make(BriefSettings(scope="default_assumptions"))
    assert ask(g, snap, 100, 48)[1:] == ("llm", None)
    for bp, thr in ((50, 48), (100, 36), (150, 120)):
        text, source, reason = ask(g, snap, bp, thr)
        assert (source, reason) == ("template", "scope") and text.startswith("As of")
    assert gen.calls == 1


def test_scope_all_and_off(snap):
    g, _gen, _ = make(BriefSettings(scope="all"))
    assert ask(g, snap, 50, 12)[1:] == ("llm", None)
    g2, gen2, _ = make(BriefSettings(scope="off"))
    assert ask(g2, snap)[1:] == ("template", "off") and gen2.calls == 0


def test_no_key_means_template_and_no_call(snap):
    g, gen, _ = make(key=False)
    assert ask(g, snap)[1:] == ("template", "no_key") and gen.calls == 0
    assert g.status()["available"] is False


def test_key_placeholder_is_not_a_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "your_key_here")
    assert brief_guard._env_key_present() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-value")
    assert brief_guard._env_key_present() is True


# ---- cache --------------------------------------------------------------------

def test_cache_hit_is_free_and_not_counted(snap):
    g, gen, _ = make(BriefSettings(scope="all", daily_cap=1))
    first = ask(g, snap)
    assert first[1:] == ("llm", None) and g.status()["used_today"] == 1
    for _ in range(3):
        assert ask(g, snap) == first
    assert gen.calls == 1 and g.status()["used_today"] == 1
    # the cap is spent, but a cached key still answers; a new key does not
    assert ask(g, other_snap(snap))[1:] == ("template", "daily_cap")


def test_template_results_are_never_cached(snap):
    g, gen, _ = make(BriefSettings(scope="all", cooldown_sec=0), gen=Gen("template"))
    ask(g, snap)
    assert g._cache == {}
    gen.mode = "text"
    assert ask(g, snap)[1:] == ("llm", None)
    assert len(g._cache) == 1
    g2, _, _ = make(BriefSettings(scope="off"))
    ask(g2, snap)
    assert g2._cache == {}


def test_cache_is_bounded_lru(snap, monkeypatch):
    monkeypatch.setattr(brief_guard, "CACHE_MAX", 2)
    g, gen, _ = make(BriefSettings(scope="all", daily_cap=99))
    a, b, c = snap, other_snap(snap), with_trigger(snap, 1.0)
    ask(g, a)
    ask(g, b)
    ask(g, a)
    ask(g, c)  # b is the least recently used and is evicted
    assert gen.calls == 3 and len(g._cache) == 2
    ask(g, a)
    assert gen.calls == 3
    ask(g, b)
    assert gen.calls == 4


def test_cache_key_has_model_and_prompt_hash(snap):
    g, _gen, _ = make()
    ask(g, snap)
    (key,) = g._cache
    assert key[:4] == (snap.as_of, 100, 48, round(snap.selected_rung.trigger_rate, 3))
    assert key[4] == brief_guard.MODEL and key[5] == brief_guard.PROMPT_HASH and len(key[5]) == 12


# ---- cap ----------------------------------------------------------------------

def test_cap_reserved_before_call_and_not_refunded(snap):
    seen = []
    inner = Gen("raise")

    def probe(snapshot, *, mode):
        seen.append(g.status()["used_today"])  # the count already includes this attempt
        return inner(snapshot, mode=mode)

    g, _, _ = make(BriefSettings(scope="all", cooldown_sec=0), gen=probe)
    ask(g, snap)
    assert seen == [1] and g.status()["used_today"] == 1  # the failure did not refund it


def test_cap_reached_gives_template_without_a_call(snap):
    g, gen, _ = make(BriefSettings(scope="all", daily_cap=2))
    ask(g, snap)
    ask(g, other_snap(snap))
    assert ask(g, with_trigger(snap, 1.0))[1:] == ("template", "daily_cap") and gen.calls == 2
    g0, gen0, _ = make(BriefSettings(scope="all", daily_cap=0))
    assert ask(g0, snap)[1:] == ("template", "daily_cap") and gen0.calls == 0


def test_utc_day_rollover_resets_count(snap):
    clock = Clock(1_700_000_000.0)  # 2023-11-14 22:13 UTC
    g, gen, _ = make(BriefSettings(scope="all", daily_cap=1), clock=clock)
    ask(g, snap)
    assert ask(g, other_snap(snap))[1:] == ("template", "daily_cap")
    clock.t += 3 * 3600  # past UTC midnight
    assert g.status()["used_today"] == 0
    assert ask(g, other_snap(snap))[1:] == ("llm", None) and gen.calls == 2


# ---- failure and cool-down ------------------------------------------------------

@pytest.mark.parametrize("mode", ["raise", "template", "empty"])
def test_failure_starts_cooldown_then_one_call_after(snap, mode):
    g, gen, clock = make(BriefSettings(scope="all", cooldown_sec=900), gen=Gen(mode))
    text, source, reason = ask(g, snap)
    assert (source, reason) == ("template", "cooldown") and "secret" not in text
    assert gen.calls == 1 and g.status()["cooling_down"] is True
    for _ in range(3):
        assert ask(g, snap)[1:] == ("template", "cooldown")
    assert gen.calls == 1  # no call during the cool-down
    clock.t += 901
    gen.mode = "text"
    assert ask(g, snap)[1:] == ("llm", None) and gen.calls == 2
    assert g.status()["cooling_down"] is False


def test_cooldown_does_not_block_cache_hits(snap):
    g, gen, _ = make(BriefSettings(scope="all"))
    good = ask(g, snap)
    gen.mode = "raise"
    ask(g, other_snap(snap))  # fails, cool-down starts
    assert ask(g, snap) == good


def test_real_generate_brief_failure_is_detected(snap, monkeypatch):
    """Through the real generate_brief with a fake anthropic module: no network, fake key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-value")
    made = []

    class Boom:
        def __init__(self, **kw):
            made.append(kw)
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kw):
            raise RuntimeError("down")

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=Boom))
    g = BriefGuard(BriefSettings(scope="all"), clock=Clock())
    assert ask(g, snap)[1:] == ("template", "cooldown") and len(made) == 1
    assert ask(g, snap)[1:] == ("template", "cooldown") and len(made) == 1


# ---- single flight --------------------------------------------------------------

def run_thread(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()))
    t.start()
    return t, out


def test_different_key_in_flight_is_busy_same_key_shares(snap):
    gen = Gen()
    gen.gate.clear()
    g, _, _ = make(BriefSettings(scope="all", daily_cap=5), gen=gen, wait_sec=5)
    t1, o1 = run_thread(lambda: ask(g, snap))
    assert gen.started.wait(5)
    assert ask(g, other_snap(snap))[1:] == ("template", "busy")
    t2, o2 = run_thread(lambda: ask(g, snap))
    time.sleep(0.1)
    gen.gate.set()
    t1.join(5)
    t2.join(5)
    assert o1["r"] == o2["r"] and o1["r"][1:] == ("llm", None)
    assert gen.calls == 1 and g.status()["used_today"] == 1  # the waiter neither called nor counted


def test_waiter_times_out_to_busy(snap):
    gen = Gen()
    gen.gate.clear()
    g, _, _ = make(BriefSettings(scope="all"), gen=gen, wait_sec=0.1)
    t1, _o = run_thread(lambda: ask(g, snap))
    assert gen.started.wait(5)
    assert ask(g, snap)[1:] == ("template", "busy")
    gen.gate.set()
    t1.join(5)


def test_waiter_on_a_failed_flight_gets_template(snap):
    gen = Gen("raise")
    gen.gate.clear()
    g, _, _ = make(BriefSettings(scope="all"), gen=gen, wait_sec=5)
    t1, o1 = run_thread(lambda: ask(g, snap))
    assert gen.started.wait(5)
    t2, o2 = run_thread(lambda: ask(g, snap))
    time.sleep(0.1)
    gen.gate.set()
    t1.join(5)
    t2.join(5)
    assert o2["r"][1] == "template" and o1["r"][1:] == ("template", "cooldown") and gen.calls == 1


# ---- per-IP limit ----------------------------------------------------------------

def six(snap):
    return [with_trigger(snap, 1.0 + i) for i in range(6)]


def test_ip_limit_off_by_default(snap):
    g, gen, _ = make(BriefSettings(scope="all", daily_cap=50))
    for s in six(snap):
        assert ask(g, s, ip="9.9.9.9")[1:] == ("llm", None)
    assert gen.calls == 6


def test_client_ip_uses_last_hop():
    assert client_ip("1.1.1.1, 2.2.2.2,3.3.3.3") == "3.3.3.3"
    assert client_ip("4.4.4.4") == "4.4.4.4"
    assert client_ip(None) == "unknown" and client_ip(" , ") == "unknown"


def test_ip_limit_on_with_header_and_spoofed_first_hop_ignored(snap):
    g, gen, clock = make(BriefSettings(scope="all", daily_cap=50, ip_header="X-Forwarded-For"))
    s = six(snap)
    results = [ask(g, s[i], ip=f"spoof-{i}, 7.7.7.7")[1:] for i in range(4)]
    assert results == [("llm", None)] * 3 + [("template", "ip_limit")]
    assert gen.calls == 3
    assert ask(g, s[4], ip="spoof, 8.8.8.8")[1:] == ("llm", None)  # another real client is fine
    clock.t += 3601
    assert ask(g, s[5], ip="7.7.7.7")[1:] == ("llm", None)  # the window has passed
