"""Storage and boot hardening: seed stays untouched, working DB is WAL, copy is atomic."""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from src.app import bootstrap
from src.core import pool
from src.data import db

SEED = Path("data") / "seed.db"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_seed():
    if not SEED.is_file():
        pytest.skip("data/seed.db not present")


def test_seed_untouched_by_readonly_open():
    _require_seed()
    before = _sha(SEED)
    assert SEED.read_bytes()[18:20] == b"\x01\x01"  # rollback journal, not WAL
    conn = db.connect(SEED, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] > 0
    finally:
        conn.close()
    assert _sha(SEED) == before
    assert SEED.read_bytes()[18:20] == b"\x01\x01"


def test_readonly_connect_refuses_write():
    _require_seed()
    conn = db.connect(SEED, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE nope (x INTEGER)")
    finally:
        conn.close()


def test_readonly_does_not_create_directories(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        db.connect(tmp_path / "newdir" / "x.db", readonly=True)
    assert not (tmp_path / "newdir").exists()


def test_init_working_db_sets_wal(tmp_path):
    _require_seed()
    target = tmp_path / "work.db"
    shutil.copy2(SEED, target)
    db.init_working_db(target)
    conn = db.connect(target, ensure_schema=False)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_init_working_db_refuses_seed():
    _require_seed()
    with pytest.raises(ValueError):
        db.init_working_db(SEED)


def test_bootstrap_copy_discards_stale_sidecars(tmp_path, monkeypatch):
    _require_seed()
    target = tmp_path / "work.db"
    target.write_bytes(b"not a database")
    (tmp_path / "work.db-wal").write_bytes(b"junk wal bytes" * 100)
    (tmp_path / "work.db-shm").write_bytes(b"junk shm bytes" * 100)
    monkeypatch.setenv("REFI_DB_PATH", str(target))
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(bootstrap, "SEED_DB_PATH", SEED.resolve())

    assert bootstrap._copy_seed_if_needed(target) is True

    assert not (tmp_path / "work.db.tmp").exists()
    conn = db.connect(target, ensure_schema=False)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert len(pool.load_pool(conn)) == 5000
    finally:
        conn.close()
    ready, as_of = bootstrap._ready_as_of()
    assert ready and as_of


def test_refresh_lock_and_attribute_exist():
    assert isinstance(bootstrap._refresh_lock, type(threading.Lock()))
    assert hasattr(bootstrap, "_last_refresh_attempt")


def test_secrets_from_env_is_streamlit_free(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "secret-value")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = bootstrap.secrets_from_env()
    assert result == {"FRED_API_KEY": True, "ANTHROPIC_API_KEY": False}
    assert "secret-value" not in repr(result)
    code = (
        "import sys; from src.app import bootstrap; bootstrap.secrets_from_env(); "
        "sys.exit(1 if 'streamlit' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


@pytest.mark.parametrize("folder", ["ha#sh", "pe%41rcent", "sp ace"])
def test_readonly_handles_special_characters_in_path(tmp_path, folder):
    _require_seed()
    d = tmp_path / folder
    d.mkdir()
    shutil.copy2(SEED, d / "seed.db")
    conn = db.connect(d / "seed.db", readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] > 0
    finally:
        conn.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == [folder]


def _unready_wal_db(tmp_path):
    """Working DB in WAL with an empty schema, plus a second connection holding
    committed-but-not-checkpointed frames (so -wal stays live)."""
    target = tmp_path / "work.db"
    db.init_working_db(target)
    holder = db.connect(target, ensure_schema=False)
    holder.execute("PRAGMA wal_autocheckpoint=0")
    holder.execute("INSERT INTO ingest_log VALUES ('X', 'now', 1)")
    holder.commit()
    return target, holder


def _fresh_env(monkeypatch, target):
    monkeypatch.setenv("REFI_DB_PATH", str(target))
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(bootstrap, "SEED_DB_PATH", SEED.resolve())
    bootstrap._last_refresh_attempt = None


@pytest.mark.skipif(sys.platform != "win32", reason="live WAL file lock is Windows behaviour")
def test_ensure_database_survives_live_wal_holder(tmp_path, monkeypatch):
    _require_seed()
    target, holder = _unready_wal_db(tmp_path)
    _fresh_env(monkeypatch, target)
    try:
        assert bootstrap.ensure_database() == (False, None)
        assert not (tmp_path / "work.db.tmp").exists()
        assert holder.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0] == 1
    finally:
        holder.close()


def test_copy_failure_returns_false_and_leaves_no_tmp(tmp_path, monkeypatch):
    _require_seed()
    target = tmp_path / "work.db"
    db.init_working_db(target)
    _fresh_env(monkeypatch, target)

    def boom(*a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(bootstrap.os, "replace", boom)
    assert bootstrap.ensure_database() == (False, None)
    assert not (tmp_path / "work.db.tmp").exists()
