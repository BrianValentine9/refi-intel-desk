"""Synthetic pool tests — determinism, strata presence, distribution, persistence."""

from __future__ import annotations

from src.core import pool
from src.data import db


def _rows(loans):
    return [pool._to_row(loan) for loan in loans]


def test_same_seed_is_byte_identical():
    assert _rows(pool.generate_pool()) == _rows(pool.generate_pool())


def test_different_seed_differs():
    assert _rows(pool.generate_pool(seed=1)) != _rows(pool.generate_pool(seed=2))


def test_pool_size_is_5000():
    assert len(pool.generate_pool()) == 5000


def test_all_strata_present():
    tags = {tag for loan in pool.generate_pool() for tag in loan.tags}
    for tag in pool.ALL_TAGS:
        assert tag in tags, f"missing stratum: {tag}"


def test_program_distribution_is_sane():
    loans = pool.generate_pool()
    va_share = sum(1 for loan in loans if loan.program == "VA") / len(loans)
    assert 0.55 <= va_share <= 0.65


def test_legacy_tail_is_about_ten_percent():
    loans = pool.generate_pool()
    legacy = sum(1 for loan in loans if pool.TAG_LEGACY_TAIL in loan.tags)
    assert 0.07 <= legacy / len(loans) <= 0.13


def test_persist_round_trip(tmp_path):
    loans = pool.generate_pool(seed=1, n=60)
    conn = db.connect(tmp_path / "t.db")
    pool.persist_pool(conn, loans)
    loaded = pool.load_pool(conn)
    conn.close()
    assert _rows(loans) == _rows(loaded)


def test_persist_is_idempotent(tmp_path):
    loans = pool.generate_pool(seed=1, n=60)
    conn = db.connect(tmp_path / "t.db")
    pool.persist_pool(conn, loans)
    pool.persist_pool(conn, loans)  # regenerate replaces, never appends
    assert len(pool.load_pool(conn)) == 60
    conn.close()
