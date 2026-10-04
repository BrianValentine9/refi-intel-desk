"""API building blocks: input parsers and the ladder compute service (no HTTP)."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from src.api import compute, inputs
from src.api.compute import LadderService, default_rung_index
from src.core import ladder, pool
from src.data import db

SEED = Path("data") / "seed.db"


# ---- inputs ----------------------------------------------------------------

@pytest.mark.parametrize("fn,good,bad", [
    (inputs.parse_cost_bp, ["50", 100, "150", " 70 "], ["49", "155", "100.5", "", "-50", True, None, 100.0, "1e2", "abc", "9" * 5000]),
    (inputs.parse_threshold, ["12", 48, "120"], ["11", "121", "48.0", "", "-1", False]),
    (inputs.parse_rung, ["0", 16, "8"], ["17", "-1", "1.5", "", True]),
    (inputs.parse_days, ["90", 180, "365"], ["30", "91", "90.0", "", True, "-90"]),
])
def test_parsers_accept_and_reject(fn, good, bad):
    for g in good:
        assert fn(g) == int(str(g).strip())
    for b in bad:
        with pytest.raises(inputs.InputError):
            fn(b)


def test_input_error_is_value_error_and_cost_pct():
    assert issubclass(inputs.InputError, ValueError)
    assert inputs.cost_pct_from_bp(70) == 0.007
    assert inputs.cost_pct_from_bp(100) == 0.01


# ---- service with a controllable fake ladder -------------------------------

class FakeConn:
    def close(self):
        pass


class Fake:
    """Counts calls; every call blocks on ``gate`` until released."""

    def __init__(self, fail_keys=()):
        self.calls = []
        self.gate = threading.Event()
        self.started = threading.Event()
        self.fail_threshold = set(fail_keys)
        self._lock = threading.Lock()

    def __call__(self, loans, conn, *, cost_pct, threshold_months, step, rate_range):
        with self._lock:
            self.calls.append((cost_pct, threshold_months))
        self.started.set()
        self.gate.wait(5)
        if threshold_months in self.fail_threshold:
            self.fail_threshold.discard(threshold_months)
            raise RuntimeError("boom")
        return [f"rung-{cost_pct}-{threshold_months}"], 6.25, 6.5


@pytest.fixture
def make():
    made = []

    def _make(fake=None, **kw):
        fake = fake or Fake()
        svc = LadderService(loans=[], connect=FakeConn, ladder_fn=fake, **kw)
        made.append((svc, fake))
        return svc, fake

    yield _make
    for svc, fake in made:
        fake.gate.set()
        svc.close()


def test_same_key_concurrent_requests_compute_once(make):
    svc, fake = make()
    results = []
    threads = [threading.Thread(target=lambda: results.append(svc.request("d1", 100, 48))) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {r.status for r in results} == {"computing"}
    fake.gate.set()
    out = svc.wait(svc.key_for("d1", 100, 48), 5)
    assert out.status == "ready" and out.value.rungs == ["rung-0.01-48"]
    assert svc.request("d1", 100, 48).status == "ready"
    assert len(fake.calls) == 1


def test_distinct_keys_queue_then_busy(make):
    svc, fake = make(max_pending=2)
    assert svc.request("d", 100, 12).status == "computing"
    assert fake.started.wait(5)  # first key is now running, not pending
    assert svc.request("d", 100, 13).status == "computing"
    assert svc.request("d", 100, 14).status == "computing"
    assert svc.request("d", 100, 15).status == "busy"
    assert svc.request("d", 100, 13).status == "computing"  # already queued: not busy
    fake.gate.set()
    assert svc.wait(svc.key_for("d", 100, 14), 5).status == "ready"
    assert len(fake.calls) == 3


def test_lru_eviction(make):
    fake = Fake()
    fake.gate.set()
    svc, _ = make(fake, max_entries=2)
    for t in (12, 13, 14):
        svc.request("d", 100, t)
        assert svc.wait(svc.key_for("d", 100, t), 5).status == "ready"
    assert svc.peek(svc.key_for("d", 100, 12)).status == "unknown"  # evicted
    assert svc.peek(svc.key_for("d", 100, 14)).status == "ready"
    assert len(fake.calls) == 3


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_failing_key_is_cooled_down_not_hammered(make):
    class Always(Fake):
        def __call__(self, *a, **k):
            super().__call__(*a, **k)
            raise RuntimeError("boom")

    fake, clock = Always(), Clock()
    fake.gate.set()
    svc, _ = make(fake, clock=clock)
    key = svc.key_for("d", 100, 20)
    svc.request("d", 100, 20)
    assert svc.wait(key, 5).status == "error"
    for _ in range(25):  # a 1 Hz poller inside the cool-down
        out = svc.request("d", 100, 20)
        assert out.status == "error" and "boom" in out.message
    assert len(fake.calls) == 1
    clock.t += compute.ERROR_COOLDOWN_SEC + 1
    assert svc.request("d", 100, 20).status == "computing"  # exactly one retry
    assert svc.request("d", 100, 20).status in ("computing", "error")
    assert svc.wait(key, 5).status == "error"
    assert len(fake.calls) == 2


def test_fail_once_then_succeed_after_cooldown(make):
    fake, clock = Fake(fail_keys=[20]), Clock()
    fake.gate.set()
    svc, _ = make(fake, clock=clock)
    key = svc.key_for("d", 100, 20)
    svc.request("d", 100, 20)
    assert svc.wait(key, 5).status == "error"
    assert svc.request("d", 100, 20).status == "error"
    clock.t += compute.ERROR_COOLDOWN_SEC + 1
    assert svc.request("d", 100, 20).status == "computing"
    assert svc.wait(key, 5).status == "ready"  # worker survived the failure
    assert len(fake.calls) == 2


def test_unknown_and_closed_statuses(make):
    fake = Fake()
    fake.gate.set()
    svc, _ = make(fake)
    key = svc.key_for("d", 100, 48)
    assert svc.peek(key).status == "unknown"
    assert svc.wait(key, 5).status == "unknown"  # returns at once, no hang
    svc.close()
    assert svc.request("d", 100, 48).status == "closed"
    assert svc.peek(key).status == "closed"
    assert svc.wait(key).status == "closed"  # timeout=None must not hang
    assert svc.set_as_of("d2") is False
    assert fake.calls == []


def test_warm_default_keeps_only_the_latest_warm_job(make):
    svc, fake = make(max_pending=2)
    svc.request("d", 100, 12)
    assert fake.started.wait(5)
    for day in ("w1", "w2", "w3"):
        svc.warm_default(day)
    assert svc.peek(svc.default_key("w1")).status == "unknown"  # dropped
    assert svc.peek(svc.default_key("w2")).status == "unknown"
    assert svc.peek(svc.default_key("w3")).status == "computing"
    assert svc.request("d", 100, 13).status == "computing"
    assert svc.request("d", 100, 14).status == "computing"
    assert svc.request("d", 100, 15).status == "busy"  # warm does not use a normal slot
    fake.gate.set()
    assert svc.wait(svc.default_key("w3"), 5).status == "ready"
    assert svc.wait(svc.key_for("d", 100, 14), 5).status == "ready"
    assert len(fake.calls) == 4


def test_warm_default_jumps_the_queue(make):
    svc, fake = make(max_pending=2)
    svc.request("d", 100, 12)
    assert fake.started.wait(5)
    svc.request("d", 100, 13)
    svc.request("d", 100, 14)
    svc.warm_default("d")  # priority, allowed past max_pending
    fake.gate.set()
    assert svc.wait(svc.default_key("d"), 5).status == "ready"
    assert svc.wait(svc.key_for("d", 100, 14), 5).status == "ready"
    assert fake.calls[1] == (0.01, 48)  # default ran right after the one in flight


def test_set_as_of_computes_new_default_first(make):
    fake = Fake()
    fake.gate.set()
    svc, _ = make(fake)
    assert svc.current_as_of is None
    assert svc.set_as_of("d1", timeout=5) is True
    assert svc.current_as_of == "d1"
    assert svc.peek(svc.default_key("d1")).status == "ready"
    fake.gate.clear()
    assert svc.set_as_of("d2", timeout=0.2) is False  # not ready in time: stays on d1
    assert svc.current_as_of == "d1"
    fake.gate.set()
    assert svc.set_as_of("d2", timeout=5) is True and svc.current_as_of == "d2"


def test_default_rung_is_nearest_market():
    assert default_rung_index([object(), object()]) == 0


# ---- golden: real ladder on a seed copy ------------------------------------

def test_golden_default_ladder_matches_direct_build(tmp_path):
    if not SEED.is_file():
        pytest.skip("data/seed.db not present")
    target = tmp_path / "work.db"
    shutil.copy2(SEED, target)

    def connect():
        return db.connect(target, ensure_schema=False)

    conn = connect()
    try:
        loans = pool.load_pool(conn) or pool.generate_pool(pool.DEFAULT_SEED)
        direct, d_va, d_fha = ladder.build_ladder(loans, conn, cost_pct=0.01, threshold_months=48)
    finally:
        conn.close()

    svc = LadderService(loans=loans, connect=connect)
    try:
        svc.warm_default("golden")
        out = svc.wait(svc.default_key("golden"), 60)
    finally:
        svc.close()
    assert out.status == "ready", out.message
    rungs = out.value.rungs
    assert len(rungs) == 17
    assert rungs[0].trigger_rate == 6.25
    assert {r.trigger_rate: r.cumulative_count for r in rungs}[5.625] == 2537
    assert rungs == direct and (out.value.current_va, out.value.current_fha) == (d_va, d_fha)
