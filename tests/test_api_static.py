"""Static serving of the front-end export: cache headers, route order, 404 page, missing dir.

No Node at test time: a tiny fake export lives in tmp_path and WEB_DIST points at it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.app import bootstrap
from tests.test_api_app import SEED  # noqa: F401  (same seed copy rules as the API tests)


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)


@pytest.fixture
def work_db(tmp_path, monkeypatch):
    path = tmp_path / "work" / "ladder.db"
    path.parent.mkdir()
    path.write_bytes(SEED.read_bytes())
    monkeypatch.setenv("REFI_DB_PATH", str(path))
    return path


@pytest.fixture
def dist(tmp_path, monkeypatch):
    d = tmp_path / "out"
    (d / "_next" / "static" / "chunks").mkdir(parents=True)
    (d / "index.html").write_text("<html><body>INDEX PAGE</body></html>", encoding="utf-8")
    (d / "404.html").write_text("<html><body>FAKE 404 PAGE</body></html>", encoding="utf-8")
    (d / "_next" / "static" / "chunks" / "a.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setenv("WEB_DIST", str(d))
    return d


def _client(**kw):
    return TestClient(create_app(start_background=False, **kw))


def test_index_is_served_no_cache(work_db, dist):
    with _client() as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "INDEX PAGE" in r.text
        assert r.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_immutable(work_db, dist):
    with _client() as c:
        r = c.get("/_next/static/chunks/a.js")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_api_routes_still_win_over_the_mount(work_db, dist):
    with _client() as c:
        assert c.get("/api/health").json() == {"status": "ok"}
        r = c.get("/api/status")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
        assert "ready" in r.json()
        h = c.get("/_stcore/health")
        assert h.status_code == 200 and h.text == "ok"


def test_unknown_path_gets_the_404_page(work_db, dist):
    with _client() as c:
        for path in ("/nope/", "/nope", "/_next/static/chunks/missing.js"):
            r = c.get(path)
            assert r.status_code == 404, path
            assert "FAKE 404 PAGE" in r.text, path


def test_unknown_api_path_is_not_the_index(work_db, dist):
    with _client() as c:
        r = c.get("/api/nope")
        assert r.status_code == 404
        assert "INDEX PAGE" not in r.text


def test_missing_dist_dir_still_starts_and_api_works(work_db, tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_DIST", str(tmp_path / "does-not-exist"))
    with _client() as c:
        r = c.get("/")
        assert r.status_code == 404
        assert r.json() == {"detail": "Not Found"}
        assert c.get("/api/health").json() == {"status": "ok"}
        assert c.get("/api/status").status_code == 200


def test_default_dist_is_repo_web_out(monkeypatch):
    from src.api import app as api_app

    monkeypatch.delenv("WEB_DIST", raising=False)
    assert api_app.web_dist_dir().parts[-2:] == ("web", "out")
