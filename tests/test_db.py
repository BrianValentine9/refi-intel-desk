"""Tests for the SQLite layer: idempotent upsert, NULL handling, latest_date."""

from __future__ import annotations

import pytest

from src.data import db
from src.data.fred_client import Observation


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def _obs():
    return [
        Observation("DGS10", "2026-01-02", 4.21),
        Observation("DGS10", "2026-01-03", None),
        Observation("DGS10", "2026-01-06", 4.25),
    ]


def test_upsert_is_idempotent(conn):
    db.upsert_observations(conn, _obs())
    first = db.count_observations(conn)
    db.upsert_observations(conn, _obs())  # same data again
    second = db.count_observations(conn)
    assert first == second == 3


def test_upsert_replaces_value_for_same_key(conn):
    db.upsert_observations(conn, [Observation("DGS10", "2026-01-02", 4.21)])
    db.upsert_observations(conn, [Observation("DGS10", "2026-01-02", 9.99)])
    assert db.count_observations(conn) == 1
    cur = conn.execute(
        "SELECT value FROM observations WHERE series_id='DGS10' AND obs_date='2026-01-02'"
    )
    assert cur.fetchone()[0] == 9.99


def test_missing_value_stored_as_null(conn):
    db.upsert_observations(conn, [Observation("DGS10", "2026-01-03", None)])
    cur = conn.execute("SELECT value FROM observations WHERE obs_date='2026-01-03'")
    assert cur.fetchone()[0] is None


def test_latest_date_is_max(conn):
    db.upsert_observations(conn, _obs())
    assert db.latest_date(conn, "DGS10") == "2026-01-06"


def test_latest_date_none_when_empty(conn):
    assert db.latest_date(conn, "DGS10") is None


def test_latest_nonnull_skips_trailing_null(conn):
    db.upsert_observations(
        conn,
        [
            Observation("DGS10", "2026-01-06", 4.25),
            Observation("DGS10", "2026-01-07", None),  # newest date, but NULL
        ],
    )
    assert db.latest_date(conn, "DGS10") == "2026-01-07"
    assert db.latest_nonnull(conn, "DGS10") == ("2026-01-06", 4.25)


def test_log_ingest_records_run(conn):
    db.log_ingest(conn, "DGS10", 3)
    cur = conn.execute("SELECT series_id, rows_upserted FROM ingest_log")
    assert cur.fetchone() == ("DGS10", 3)
