"""Bootstrap helpers - seed copy, secrets bridge, and staleness refresh (mocked ingest)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.app import bootstrap, data_access  # noqa: F401 (data_access kept for parity/imports)
from src.data import db  # noqa: F401


def _reset_throttle() -> None:
    bootstrap._last_refresh_attempt = None


def test_copy_seed_makes_database_ready(tmp_path, monkeypatch):
    repo_seed = Path("data") / "seed.db"
    if not repo_seed.is_file():
        pytest.skip("data/seed.db not present")

    target = tmp_path / "refi.db"
    monkeypatch.setenv("REFI_DB_PATH", str(target))
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    ready, as_of = bootstrap.ensure_database()
    assert ready is True
    assert as_of is not None
    assert target.is_file()


def test_ensure_database_empty_without_seed_or_key(tmp_path, monkeypatch):
    monkeypatch.setenv("REFI_DB_PATH", str(tmp_path / "missing.db"))
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(bootstrap, "SEED_DB_PATH", tmp_path / "no-seed.db")

    ready, as_of = bootstrap.ensure_database()
    assert ready is False
    assert as_of is None


def test_apply_streamlit_secrets_noop_without_secrets(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    bootstrap.apply_streamlit_secrets()  # must not raise


def test_is_stale_thresholds():
    assert bootstrap._is_stale("2020-01-01") is True
    assert bootstrap._is_stale(date.today().isoformat()) is False
    assert bootstrap._is_stale(None) is False


def test_refresh_runs_when_stale_and_keyed(tmp_path, monkeypatch):
    _reset_throttle()
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(bootstrap.ingest, "run", lambda *a, **k: calls.append((a, k)))
    bootstrap._maybe_refresh_stale(tmp_path / "refi.db", "2020-01-01")
    assert len(calls) == 1


def test_no_refresh_when_fresh(tmp_path, monkeypatch):
    _reset_throttle()
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(bootstrap.ingest, "run", lambda *a, **k: calls.append(1))
    bootstrap._maybe_refresh_stale(tmp_path / "refi.db", date.today().isoformat())
    assert calls == []


def test_no_refresh_without_key(tmp_path, monkeypatch):
    _reset_throttle()
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(bootstrap.ingest, "run", lambda *a, **k: calls.append(1))
    bootstrap._maybe_refresh_stale(tmp_path / "refi.db", "2020-01-01")
    assert calls == []


def test_refresh_error_is_swallowed(tmp_path, monkeypatch):
    _reset_throttle()
    monkeypatch.setenv("FRED_API_KEY", "test-key")

    def boom(*a, **k):
        raise RuntimeError("FRED unavailable")

    monkeypatch.setattr(bootstrap.ingest, "run", boom)
    bootstrap._maybe_refresh_stale(tmp_path / "refi.db", "2020-01-01")  # must not raise


def test_refresh_is_throttled(tmp_path, monkeypatch):
    _reset_throttle()
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(bootstrap.ingest, "run", lambda *a, **k: calls.append(1))
    bootstrap._maybe_refresh_stale(tmp_path / "refi.db", "2020-01-01")
    bootstrap._maybe_refresh_stale(tmp_path / "refi.db", "2020-01-01")
    assert len(calls) == 1  # second attempt within the window is throttled


def test_ensure_database_ready_when_refresh_raises(tmp_path, monkeypatch):
    repo_seed = Path("data") / "seed.db"
    if not repo_seed.is_file():
        pytest.skip("data/seed.db not present")
    _reset_throttle()
    target = tmp_path / "refi.db"
    monkeypatch.setenv("REFI_DB_PATH", str(target))
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    monkeypatch.setattr(bootstrap, "_is_stale", lambda as_of: True)  # force the refresh path

    def boom(*a, **k):
        raise RuntimeError("FRED unavailable")

    monkeypatch.setattr(bootstrap.ingest, "run", boom)
    ready, as_of = bootstrap.ensure_database()
    assert ready is True
    assert as_of is not None
