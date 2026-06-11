"""App smoke tests — module imports without a Streamlit server; missing-DB handling."""

from __future__ import annotations

import importlib

from src.app import data_access
from src.data import db


def test_dashboard_imports_without_server():
    module = importlib.import_module("src.app.dashboard")
    assert callable(module.main)
    for name in [
        "render_masthead", "render_metrics", "render_chart",
        "render_ladder", "render_sidebar", "render_footer",
    ]:
        assert hasattr(module, name), f"missing render helper: {name}"


def test_database_not_ready_on_empty_db(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    try:
        assert data_access.database_ready(conn) is False  # drives the helpful message
    finally:
        conn.close()
