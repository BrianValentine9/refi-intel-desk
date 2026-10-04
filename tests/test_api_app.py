"""HTTP layer: routes, statuses, refresh scheduler. TestClient with the real lifespan.

Most tests inject a LadderService with a fast fake ladder; one runs the real engine.
The working DB is a tmp copy of the committed seed (REFI_DB_PATH); keys are unset.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import compute
from src.api.app import create_app
from src.api.compute import LadderService
from src.api.refresh import RefreshScheduler
from src.app import bootstrap
from src.core.ladder import LadderRung

SEED = Path("data") / "seed.db"


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)


@pytest.fixture
def seed_db(tmp_path, monkeypatch):
    path = tmp_path / "work" / "ladder.db"
    path.parent.mkdir()
    path.write_bytes(SEED.read_bytes())  # bytes copy; the seed itself is never opened
    monkeypatch.setenv("REFI_DB_PATH", str(path))
    return path


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    path = tmp_path / "empty" / "ladder.db"
    monkeypatch.setenv("REFI_DB_PATH", str(path))
    monkeypatch.setattr(bootstrap, "SEED_DB_PATH", tmp_path / "no-such-seed.db")
    return path


def fake_rungs():
    out = []
    for i in range(17):
        out.append(LadderRung(
            trigger_rate=round(6.25 - 0.125 * i, 3), distance_from_market=0.025 + 0.125 * i,
            newly_eligible=10, eligible_count=10 * (i + 1), cumulative_count=10 * (i + 1),
            eligible_va=6 * (i + 1), eligible_fha=4 * (i + 1),
            median_statutory_recoupment=float("inf") if i == 0 else 20.5,
            median_break_even=float("nan") if i == 1 else 31.2,
            soft_blocker_count=1, unknown_count=2, hard_blocker_count=0,
        ))
    return out


class FakeConn:
    def close(self):
        pass


class Fake:
    """Fast fake ladder; with gated=True every call blocks until ``gate`` is set."""

    def __init__(self, gated=False):
        self.calls = []
        self.gate = threading.Event()
        if not gated:
            self.gate.set()
        self.fail = False

    def __call__(self, loans, conn, *, cost_pct, threshold_months, step, rate_range):
        self.calls.append((cost_pct, threshold_months))
        self.gate.wait(5)
        if self.fail:
            raise RuntimeError("boom")
        return fake_rungs(), 6.25, 6.5


@pytest.fixture
def client_for(seed_db):
    opened = []

    def _open(fake=None, service_kw=None, **app_kw):
        fake = fake or Fake()
        svc = LadderService(loans=[], connect=FakeConn, ladder_fn=fake, **(service_kw or {}))
        app_kw.setdefault("start_background", False)
        client = TestClient(create_app(ladder_service=svc, **app_kw))
        client.__enter__()
        opened.append((client, fake))
        return client, fake, svc

    yield _open
    for client, fake in opened:
        fake.gate.set()
        client.__exit__(None, None, None)


def wait_ready(client, url="/api/ladder", tries=100):
    r = None
    for _ in range(tries):
        r = client.get(url)
        if r.status_code == 200:
            return r
        time.sleep(0.05)
    raise AssertionError(f"never ready: {r.status_code} {r.text}")


def strict_json(text):
    return json.loads(text, parse_constant=lambda x: pytest.fail(f"non-JSON constant {x}"))


# ---- import / health / not ready -------------------------------------------

def test_import_has_no_side_effects(tmp_path):
    probe = tmp_path / "probe"
    code = (
        "import os, sys, threading\n"
        f"os.environ['REFI_DB_PATH'] = r'{probe / 'x.db'}'\n"
        "before = threading.active_count()\n"
        "import src.api.app\n"
        "assert threading.active_count() == before, 'thread started on import'\n"
        "assert 'streamlit' not in sys.modules and 'src.app.dashboard' not in sys.modules\n"
        f"assert not os.path.exists(r'{probe}'), 'files created on import'\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=Path.cwd())
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr


def test_health_and_not_ready_on_empty_db(empty_db):
    with TestClient(create_app(start_background=False)) as c:
        assert c.get("/api/health").json() == {"status": "ok"}
        r = c.get("/_stcore/health")
        assert r.status_code == 200 and r.text == "ok" and r.headers["content-type"].startswith("text/plain")
        st = c.get("/api/status").json()
        assert st["ready"] is False and st["as_of"] is None and st["ladder_warm"] is False
        assert st["pool_size"] is None and st["pool_seed"] is None
        for url in ("/api/metrics", "/api/series?days=90", "/api/ladder", "/api/brief"):
            r = c.get(url)
            assert r.status_code == 503 and r.json() == {"status": "not_ready"}, url
        b = c.get("/api/bootstrap")
        assert b.status_code == 200 and b.json()["ladder"] == {"status": "not_ready"}


# ---- shapes ----------------------------------------------------------------

def test_status_metrics_series_shapes(client_for):
    c, _fake, _svc = client_for()
    wait_ready(c)
    st = c.get("/api/status").json()
    assert st["ready"] is True and st["as_of"] and st["ladder_warm"] is True
    assert isinstance(st["pool_size"], int) and st["pool_size"] > 0 and st["pool_seed"] == 20260610
    assert set(st["refresh"]) == {"last_run_at", "last_result", "in_progress"}
    m = c.get("/api/metrics").json()
    assert m["as_of"] == st["as_of"]
    assert [(t["id"], t["label"]) for t in m["tiles"]] == [
        ("DGS10", "10-Yr Treasury"), ("OBMMIVA30YF", "30-Yr VA"),
        ("OBMMIFHA30YF", "30-Yr FHA"), ("OBMMIC30YF", "30-Yr Conforming")]
    assert all(isinstance(t["value"], float) and set(t) == {"id", "label", "value", "delta_7d"} for t in m["tiles"])
    s = c.get("/api/series?days=180").json()
    assert s["days"] == 180 and set(s["series"]) == {"DGS10", "OBMMIVA30YF", "OBMMIFHA30YF"}
    assert all(len(v) > 50 and len(v[0]) == 2 for v in s["series"].values())
    assert c.get("/api/series").json()["days"] == 90  # days omitted -> default 90


def test_series_bad_days_422(client_for):
    c, _f, _s = client_for()
    assert c.get("/api/series?days=30").status_code == 422
    assert "days" in c.get("/api/series?days=abc").json()["detail"]


# ---- ladder ----------------------------------------------------------------

@pytest.mark.parametrize("qs", [
    "cost_bp=55", "cost_bp=49", "cost_bp=151", "cost_bp=abc", "cost_bp=", "threshold=11", "threshold=121",
    "threshold=x", "cost_bp=100.5",
])
def test_ladder_422(client_for, qs):
    c, fake, _s = client_for()
    r = c.get(f"/api/ladder?{qs}")
    assert r.status_code == 422 and "detail" in r.json()


@pytest.mark.parametrize("qs", ["rung=17", "rung=-1", "rung=abc", "cost_bp=55", "threshold=121"])
def test_brief_422(client_for, qs):
    c, _f, _s = client_for()
    assert c.get(f"/api/brief?{qs}").status_code == 422


def test_ladder_202_then_200_and_json_safe(client_for):
    c, fake, _s = client_for(Fake(gated=True))
    r = c.get("/api/ladder")
    assert r.status_code == 202 and r.json() == {"status": "computing"} and r.headers["retry-after"] == "1"
    fake.gate.set()
    ok = wait_ready(c)
    body = strict_json(ok.text)  # inf / NaN in the output would fail here
    assert body["status"] == "ready" and body["cost_bp"] == 100 and body["threshold"] == 48
    assert body["default_rung"] == 0 and len(body["rungs"]) == 17
    assert body["rungs"][0]["index"] == 0 and body["rungs"][0]["median_statutory_recoupment"] is None
    assert body["rungs"][1]["median_break_even"] is None
    assert body["current_va"] == 6.25 and body["current_fha"] == 6.5
    assert fake.calls[0] == (0.01, 48)  # defaults: 100 bp, 48 months


def test_ladder_uses_server_as_of_not_client(client_for):
    c, fake, _s = client_for()
    wait_ready(c)
    as_of = c.get("/api/status").json()["as_of"]
    r = c.get("/api/ladder?as_of=1999-01-01")  # unknown param is ignored
    assert r.status_code == 200 and r.json()["as_of"] == as_of
    assert len(fake.calls) == 1


def test_busy_503_with_retry_after(client_for):
    c, fake, _s = client_for(Fake(gated=True))
    time.sleep(0.1)  # the default warm job is running (blocked on the gate)
    assert c.get("/api/ladder?cost_bp=50").status_code == 202  # queued
    assert c.get("/api/ladder?cost_bp=60").status_code == 202  # queued
    r = c.get("/api/ladder?cost_bp=70")
    assert r.status_code == 503 and r.json() == {"status": "busy"} and r.headers["retry-after"] == "2"


def test_error_with_cooldown_retry_after(client_for):
    now = [1000.0]
    fake = Fake()
    fake.fail = True
    c, fake, svc = client_for(fake, service_kw={"clock": lambda: now[0]})
    r = None
    for _ in range(100):
        r = c.get("/api/ladder?cost_bp=60")
        if r.status_code == 503:
            break
        time.sleep(0.05)
    assert r.status_code == 503 and r.json()["status"] == "error" and "boom" in r.json()["message"]
    assert r.headers["retry-after"] == str(int(compute.ERROR_COOLDOWN_SEC))
    now[0] += 12
    r = c.get("/api/ladder?cost_bp=60")
    assert r.status_code == 503 and r.headers["retry-after"] == "18"
    assert len(fake.calls) == 2  # default warm + this key; no retry inside the cool-down
    now[0] += 30
    fake.fail = False
    assert c.get("/api/ladder?cost_bp=60").status_code == 202  # one retry after the cool-down
    wait_ready(c, "/api/ladder?cost_bp=60")


def test_closed_service_503(client_for):
    c, _f, svc = client_for()
    svc.close()
    r = c.get("/api/ladder?cost_bp=60")
    assert r.status_code == 503 and r.json()["status"] == "closed"


# ---- bootstrap -------------------------------------------------------------

def test_bootstrap_computing_then_ready(client_for):
    c, fake, _s = client_for(Fake(gated=True))
    r = c.get("/api/bootstrap")
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"status", "metrics", "series", "ladder"}
    assert b["ladder"] == {"status": "computing"} and b["status"]["ladder_warm"] is False
    assert len(b["metrics"]["tiles"]) == 4 and b["series"]["days"] == 90
    fake.gate.set()
    wait_ready(c)
    b = c.get("/api/bootstrap").json()
    assert b["ladder"]["status"] == "ready" and len(b["ladder"]["rungs"]) == 17
    assert b["status"]["ladder_warm"] is True


# ---- brief -----------------------------------------------------------------

def test_brief_202_when_ladder_not_ready(client_for):
    c, _fake, _s = client_for(Fake(gated=True))
    r = c.get("/api/brief")
    assert r.status_code == 202 and r.json() == {"status": "computing"}


def test_brief_template_rung_and_deterministic(client_for):
    c, fake, _s = client_for()
    wait_ready(c)
    n_calls = len(fake.calls)
    r = c.get("/api/brief?rung=3")
    assert r.status_code == 200
    b = r.json()
    assert b["source"] == "template" and b["passed"] is True and b["rung"] == 3
    ladder = c.get("/api/ladder").json()
    assert b["trigger_rate"] == ladder["rungs"][3]["trigger_rate"]
    assert set(b) == {"source", "passed", "summary", "errors", "warnings", "brief", "rung", "trigger_rate", "as_of",
                    "cost_bp", "threshold", "ai"}
    assert b["cost_bp"] == 100 and b["threshold"] == 48
    assert c.get("/api/brief?rung=3").json() == b
    assert len(fake.calls) == n_calls  # a repeat does not recompute
    assert c.get("/api/brief").json()["rung"] == 0


def test_brief_ai_shape_default_is_no_key(client_for):
    c, _fake, _s = client_for()
    wait_ready(c)
    b = c.get("/api/brief").json()
    assert b["ai"] == {"scope": "default_assumptions", "reason": "no_key"}
    assert wait_ready(c, "/api/brief?cost_bp=50").json()["ai"]["reason"] == "scope"  # scope is checked before the key


def test_brief_ai_reasons_and_status_with_fake_guard(client_for, monkeypatch):
    from src.api.brief_guard import BriefGuard, BriefSettings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-value-do-not-leak")
    calls = []

    def fake_gen(snapshot, *, mode):
        calls.append(mode)
        return "Model text, synthetic pool. Not advice.", "llm"

    guard = BriefGuard(BriefSettings(scope="default_assumptions", daily_cap=1), generate=fake_gen)
    c, _fake, _s = client_for(brief_guard=guard)
    wait_ready(c)
    first = c.get("/api/brief").json()
    assert first["source"] == "llm" and first["ai"] == {"scope": "default_assumptions", "reason": None}
    assert first["brief"].startswith("Model text") and "passed" in first
    assert c.get("/api/brief").json()["source"] == "llm" and len(calls) == 1  # cache hit
    assert c.get("/api/brief?rung=3").json()["ai"]["reason"] == "daily_cap"  # new key, cap spent
    assert wait_ready(c, "/api/brief?cost_bp=150").json()["ai"]["reason"] == "scope"
    st = c.get("/api/status").json()["brief_ai"]
    assert st == {"scope": "default_assumptions", "available": True, "used_today": 1, "cap": 1, "cooling_down": False}
    assert "fake-test-value" not in c.get("/api/status").text + c.get("/api/brief").text


def test_brief_failure_reports_cooldown_without_error_text(client_for, monkeypatch):
    from src.api.brief_guard import BriefGuard, BriefSettings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-value")

    def boom(snapshot, *, mode):
        raise RuntimeError("sk-ant-leaky-detail")

    c, _fake, _s = client_for(brief_guard=BriefGuard(BriefSettings(scope="all"), generate=boom))
    wait_ready(c)
    r = c.get("/api/brief")
    assert r.status_code == 200 and r.json()["source"] == "template" and r.json()["ai"]["reason"] == "cooldown"
    assert "leaky" not in r.text
    assert c.get("/api/status").json()["brief_ai"]["cooling_down"] is True


def test_brief_ip_header_is_passed_only_when_configured(client_for, monkeypatch):
    from src.api.brief_guard import BriefGuard, BriefSettings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-value")
    seen = []
    g = BriefGuard(BriefSettings(scope="all", ip_header="X-Forwarded-For"), generate=lambda s, *, mode: ("ok text", "llm"))
    orig = g.request

    def spy(snapshot, **kw):
        seen.append(kw["ip_header_value"])
        return orig(snapshot, **kw)

    g.request = spy
    c, _fake, _s = client_for(brief_guard=g)
    wait_ready(c)
    c.get("/api/brief", headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"})
    assert seen == ["1.1.1.1, 2.2.2.2"]


# ---- real engine end to end ------------------------------------------------

def test_real_ladder_end_to_end(seed_db):
    with TestClient(create_app(start_background=False)) as c:
        r = wait_ready(c, tries=600)
        body = strict_json(r.text)
        assert len(body["rungs"]) == 17 and body["rungs"][0]["trigger_rate"] == 6.25
        rung = next(x for x in body["rungs"] if x["trigger_rate"] == 5.625)
        assert rung["cumulative_count"] == 2537
        assert c.get("/api/status").json()["ladder_warm"] is True
        brief = c.get("/api/brief").json()
        assert brief["source"] == "template" and brief["passed"] is True


# ---- refresh scheduler -----------------------------------------------------

def test_scheduler_one_at_a_time_and_swallows_errors():
    started, release = threading.Event(), threading.Event()
    runs = []

    def run():
        runs.append(1)
        started.set()
        release.wait(5)
        raise RuntimeError("boom")

    s = RefreshScheduler(run_fn=run, interval=0.01, first_delay=0)
    s.start()
    assert started.wait(5)
    time.sleep(0.15)  # the interval elapsed several times while the first run is blocked
    assert len(runs) == 1 and s.run_once() is False  # one at a time
    release.set()
    for _ in range(100):
        if s.last_result:
            break
        time.sleep(0.02)
    assert s.last_result == "error: RuntimeError" and s.last_run_at
    s.stop()
    n = len(runs)
    time.sleep(0.1)
    assert len(runs) == n  # stopped


def _gen_files(db_path):
    """Generation files next to the working DB (everything except the base file and its sidecars)."""
    return sorted(p.name for p in db_path.parent.iterdir()
                  if p.name != db_path.name and not p.name.startswith(db_path.name + "-"))


def _later(as_of, days=3):
    from datetime import date, timedelta
    return (date.fromisoformat(as_of) + timedelta(days=days)).isoformat()


@pytest.fixture
def refresh_env(monkeypatch):
    """A fake FRED key (never used for a network call: ingest.run is replaced) and no throttle."""
    from src.data import ingest

    monkeypatch.setenv("FRED_API_KEY", "fake-key-not-used")
    state = {"mode": "move", "calls": 0}

    def fake_ingest(series_ids, *, db_path, **kw):
        import sqlite3
        state["calls"] += 1
        monkeypatch.setattr(bootstrap, "_last_refresh_attempt", None)  # allow the next run in tests
        if state["mode"] == "noop":
            return []
        conn = sqlite3.connect(db_path)
        try:
            from src.app import data_access as da
            latest = [conn.execute("SELECT obs_date, value FROM observations WHERE series_id=? AND value IS NOT NULL "
                                   "ORDER BY obs_date DESC LIMIT 1", (s,)).fetchone() for s in da.REQUIRED_SERIES]
            new = _later(max(d for d, _ in latest))
            for sid, (_d, v) in zip(da.REQUIRED_SERIES, latest):
                conn.execute("INSERT INTO observations(series_id, obs_date, value) VALUES (?,?,?)", (sid, new, round(v + 0.30, 3)))
            conn.commit()
        finally:
            conn.close()
        if state["mode"] == "fail":
            raise RuntimeError("ingest blew up")
        return []

    monkeypatch.setattr(ingest, "run", fake_ingest)
    monkeypatch.setattr(bootstrap, "_last_refresh_attempt", None)
    return state


def snapshot_views(c):
    return {
        "status": c.get("/api/status").json(),
        "metrics": c.get("/api/metrics").json(),
        "series": c.get("/api/series").json(),
        "brief": c.get("/api/brief").json(),
        "ladder": c.get("/api/ladder").json(),
    }


def test_refresh_serves_one_consistent_version_until_new_ladder_ready(client_for, seed_db, refresh_env):
    c, fake, _svc = client_for(Fake(), start_background=True, refresh_first_delay=3600, refresh_interval=3600)
    wait_ready(c)
    before = snapshot_views(c)
    old = before["status"]["as_of"]
    old_vals = [t["value"] for t in before["metrics"]["tiles"]]
    assert before["brief"]["passed"] and f"As of {old}" in before["brief"]["brief"]

    fake.gate.clear()  # hold the new default ladder
    sched = c.app.state.api.scheduler
    t = threading.Thread(target=sched.run_once)
    t.start()
    for _ in range(100):
        if sched.in_progress and len(_gen_files(seed_db)) == 1 and fake.calls:
            break
        time.sleep(0.05)
    time.sleep(0.3)
    during = snapshot_views(c)
    assert during["status"]["refresh"]["in_progress"] is True
    assert {k: v["as_of"] for k, v in during.items() if "as_of" in v} == {k: old for k in ("status", "metrics", "series", "brief", "ladder")}
    assert [t_["value"] for t_ in during["metrics"]["tiles"]] == old_vals
    assert during["series"]["series"]["DGS10"][-1][0] <= old
    assert during["brief"]["passed"] and f"As of {old}" in during["brief"]["brief"]
    assert during["brief"]["brief"] == before["brief"]["brief"]

    fake.gate.set()
    t.join(10)
    after = snapshot_views(c)
    new = _later(old)
    assert {k: v["as_of"] for k, v in after.items()} == {k: new for k in after}
    assert [t_["value"] for t_ in after["metrics"]["tiles"]] == [round(v + 0.30, 3) for v in old_vals]
    assert after["series"]["series"]["DGS10"][-1][0] == new
    assert after["brief"]["passed"], (after["brief"]["errors"], after["brief"]["brief"])
    assert f"As of {new}" in after["brief"]["brief"]
    assert after["status"]["refresh"] == {"last_run_at": after["status"]["refresh"]["last_run_at"],
                                          "last_result": "swapped", "in_progress": False}
    assert after["status"]["ladder_warm"] is True
    # the working DB the app started on was never written to
    import sqlite3
    conn = sqlite3.connect(seed_db)
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE obs_date=?", (new,)).fetchone()[0] == 0
    conn.close()


def test_refresh_unchanged_as_of_discards_new_file(client_for, seed_db, refresh_env):
    refresh_env["mode"] = "noop"
    c, fake, _svc = client_for(Fake(), start_background=True, refresh_first_delay=3600)
    wait_ready(c)
    old = c.get("/api/status").json()["as_of"]
    sched = c.app.state.api.scheduler
    sched.run_once()
    assert refresh_env["calls"] == 1 and sched.last_result == "no change"
    assert c.get("/api/status").json()["as_of"] == old and _gen_files(seed_db) == []


def test_refresh_ingest_failure_keeps_old_version(client_for, seed_db, refresh_env):
    refresh_env["mode"] = "fail"
    c, fake, _svc = client_for(Fake(), start_background=True, refresh_first_delay=3600)
    wait_ready(c)
    old = c.get("/api/status").json()["as_of"]
    sched = c.app.state.api.scheduler
    sched.run_once()
    assert sched.last_result == "error: RuntimeError"
    st = c.get("/api/status").json()
    assert st["as_of"] == old and st["refresh"]["in_progress"] is False
    assert _gen_files(seed_db) == []
    assert c.get("/api/brief").json()["passed"] is True


def test_refresh_set_as_of_timeout_keeps_old_version(client_for, seed_db, refresh_env, monkeypatch):
    from src.api import app as app_mod
    monkeypatch.setattr(app_mod, "SET_AS_OF_TIMEOUT_SEC", 0.3)
    c, fake, _svc = client_for(Fake(), start_background=True, refresh_first_delay=3600)
    wait_ready(c)
    old = c.get("/api/status").json()["as_of"]
    vals = [t["value"] for t in c.get("/api/metrics").json()["tiles"]]
    fake.gate.clear()  # the new default ladder never finishes in time
    sched = c.app.state.api.scheduler
    sched.run_once()
    assert sched.last_result == "new default ladder not ready"
    assert c.get("/api/status").json()["as_of"] == old
    assert [t["value"] for t in c.get("/api/metrics").json()["tiles"]] == vals
    assert _gen_files(seed_db) == []
    fake.gate.set()


def test_refresh_keeps_only_two_generations(client_for, seed_db, refresh_env):
    c, fake, _svc = client_for(Fake(), start_background=True, refresh_first_delay=3600)
    wait_ready(c)
    sched = c.app.state.api.scheduler
    for _ in range(3):
        sched.run_once()
        assert sched.last_result == "swapped"
    assert len(_gen_files(seed_db)) == 2
    assert c.get("/api/brief").json()["passed"] is True


def test_shutdown_during_refresh_creates_no_service(empty_db, monkeypatch):
    from src.api import app as app_mod
    release, entered = threading.Event(), threading.Event()

    def slow_ensure():
        entered.set()
        release.wait(5)
        return True, "2026-07-29"

    monkeypatch.setattr(bootstrap, "ensure_database", slow_ensure)
    app = create_app(start_background=False)
    with TestClient(app):
        state = app.state.api
        assert state.service is None and state.version is None
        t = threading.Thread(target=app_mod._refresh_once, args=(state,))
        t.start()
        assert entered.wait(5)
    release.set()  # the app has shut down; the late refresh must not build anything
    t.join(5)
    assert state.service is None and state.version is None and state.stopping is True


def test_real_refresh_path_with_no_key_makes_no_network_call(client_for, seed_db, monkeypatch):
    from src.data import ingest

    def boom(*a, **k):
        raise AssertionError("ingest.run must not be called without a FRED key")

    monkeypatch.setattr(ingest, "run", boom)
    monkeypatch.setattr(bootstrap, "_last_refresh_attempt", None)
    c, _fake, _svc = client_for(start_background=True, refresh_first_delay=3600)
    sched = c.app.state.api.scheduler
    assert sched.run_once() is True and sched.last_result == "no change"
    assert _gen_files(seed_db) == []


# ---- gzip ------------------------------------------------------------------

def test_gzip_on_large_response(client_for):
    c, _f, _s = client_for()
    wait_ready(c)
    r = c.get("/api/ladder", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    assert c.get("/api/health", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding") is None


# ---- generation files across restarts, and the ghost-file guard -----------

def test_startup_discards_generation_files_from_a_previous_process(seed_db):
    stem = seed_db.stem
    gens = [seed_db.with_name(f"{stem}.20260101000000.db"), seed_db.with_name(f"{stem}.20260102000000.1.db")]
    keep = [seed_db.with_name(f"{stem}.notes.db"), seed_db.with_name(f"{stem}.2026.db"), seed_db.with_name("other.20260101000000.db")]
    for p in gens + keep:
        p.write_bytes(b"x")
    with TestClient(create_app(start_background=False)) as c:
        assert c.get("/api/health").status_code == 200
    assert not any(p.exists() for p in gens)
    assert all(p.exists() for p in keep) and seed_db.exists()


def test_connect_refuses_a_missing_path_without_creating_it(tmp_path):
    from src.api import app as app_mod
    ghost = tmp_path / "gone.db"
    with pytest.raises(FileNotFoundError):
        app_mod._connect(ghost)
    assert not ghost.exists()


def test_request_on_a_pruned_version_is_503_and_creates_no_file(client_for, seed_db):
    c, _fake, _svc = client_for()
    wait_ready(c)
    seed_db_copy = seed_db.read_bytes()
    state = c.app.state.api
    gone = seed_db.with_name("pruned.db")
    from src.api.app import DataVersion
    state.version = DataVersion(gone, state.version.as_of)
    for url in ("/api/metrics", "/api/series", "/api/bootstrap", "/api/brief"):
        r = c.get(url)
        assert r.status_code == 503, url
    assert not gone.exists()
    assert seed_db.read_bytes() == seed_db_copy
