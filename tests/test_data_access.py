"""Tests for the dashboard's read-only data-access helpers."""

from __future__ import annotations

import pytest

from src.app import data_access
from src.data import db
from src.data.fred_client import Observation


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "da.db")
    # DGS10 daily-ish with a weekend gap and a trailing NULL.
    db.upsert_observations(
        connection,
        [
            Observation("DGS10", "2026-06-01", 4.40),
            Observation("DGS10", "2026-06-02", 4.45),
            Observation("DGS10", "2026-06-08", 4.50),  # latest real value
            Observation("DGS10", "2026-06-09", None),  # trailing NULL
        ],
    )
    yield connection
    connection.close()


def test_get_latest_skips_trailing_null(conn):
    assert data_access.get_latest(conn, "DGS10") == ("2026-06-08", 4.50)


def test_get_prior_is_business_day_tolerant(conn):
    # 7 days before 2026-06-08 is 2026-06-01 (a target that exists exactly).
    assert data_access.get_prior(conn, "DGS10", days=7) == ("2026-06-01", 4.40)
    # 5 days before -> 2026-06-03 (no row); falls back to the prior trading day 06-02.
    assert data_access.get_prior(conn, "DGS10", days=5) == ("2026-06-02", 4.45)


def test_delta_vs_prior(conn):
    assert data_access.delta_vs_prior(conn, "DGS10", days=7) == 0.10  # 4.50 - 4.40


def test_get_range_returns_window_oldest_first(conn):
    rows = data_access.get_range(conn, "DGS10", n_days=7)  # cutoff 2026-06-01
    assert rows == [("2026-06-01", 4.40), ("2026-06-02", 4.45), ("2026-06-08", 4.50)]


def test_get_range_narrow_window(conn):
    rows = data_access.get_range(conn, "DGS10", n_days=3)  # cutoff 2026-06-05
    assert rows == [("2026-06-08", 4.50)]


def test_database_ready_false_when_series_missing(conn):
    assert data_access.database_ready(conn) is False  # only DGS10 present


def test_database_ready_true_when_all_present(conn):
    for series in data_access.REQUIRED_SERIES:
        db.upsert_observations(conn, [Observation(series, "2026-06-08", 6.0)])
    assert data_access.database_ready(conn) is True


def test_as_of_date_is_latest_across_series(conn):
    db.upsert_observations(conn, [Observation("OBMMIVA30YF", "2026-06-09", 6.1)])
    assert data_access.as_of_date(conn) == "2026-06-09"
