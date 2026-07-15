"""Bootstrap helpers — seed copy and secrets bridge (no live FRED calls)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.app import bootstrap, data_access
from src.data import db


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
