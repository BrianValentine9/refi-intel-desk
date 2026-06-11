"""Tests for ingest orchestration: incremental start date + backfill mode.

The HTTP layer is replaced with a fake that records the start_date it was asked
for, so we can assert the incremental logic without touching the network.
"""

from __future__ import annotations

import pytest

from src.data import db, ingest
from src.data.fred_client import Observation


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


def _fake_fetch(recorder):
    def fetch(series_id, start_date=None):
        recorder.append((series_id, start_date))
        return [Observation(series_id, "2026-01-06", 4.25)]

    return fetch


def test_incremental_uses_latest_date_as_start(monkeypatch, db_path):
    # Seed an existing row so latest_date has something to return.
    conn = db.connect(db_path)
    db.upsert_observations(conn, [Observation("DGS10", "2026-01-02", 4.21)])
    conn.close()

    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(ingest, "fetch_observations", _fake_fetch(calls))

    ingest.run(["DGS10"], db_path=db_path)
    assert calls == [("DGS10", "2026-01-02")]  # resumed from latest stored date


def test_incremental_on_empty_db_starts_from_none(monkeypatch, db_path):
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(ingest, "fetch_observations", _fake_fetch(calls))

    ingest.run(["DGS10"], db_path=db_path)
    assert calls == [("DGS10", None)]  # full history when nothing stored yet


def test_backfill_uses_years_ago_as_start(monkeypatch, db_path):
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(ingest, "fetch_observations", _fake_fetch(calls))

    ingest.run(["DGS10"], backfill_years=5, db_path=db_path)
    (_, start), = calls
    assert start is not None and start < "2022-01-01"  # ~5 years back


def test_run_logs_each_ingest_and_summarizes(monkeypatch, db_path):
    monkeypatch.setattr(ingest, "fetch_observations", _fake_fetch([]))

    results = ingest.run(["DGS10"], backfill_years=1, db_path=db_path)
    assert results[0].series_id == "DGS10"
    assert results[0].latest_value == 4.25

    conn = db.connect(db_path)
    cur = conn.execute("SELECT COUNT(*) FROM ingest_log WHERE series_id='DGS10'")
    assert cur.fetchone()[0] == 1
    conn.close()


def test_run_is_idempotent_across_two_passes(monkeypatch, db_path):
    monkeypatch.setattr(ingest, "fetch_observations", _fake_fetch([]))

    ingest.run(["DGS10"], backfill_years=1, db_path=db_path)
    ingest.run(["DGS10"], backfill_years=1, db_path=db_path)

    conn = db.connect(db_path)
    assert db.count_observations(conn, "DGS10") == 1  # no duplication
    conn.close()
