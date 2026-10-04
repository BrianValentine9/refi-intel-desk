"""U5/U6: frame-ancestors CSP on every response, JSON 404 under /api, HEAD on health/status, RSS field."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import DEFAULT_FRAME_ANCESTORS, create_app, rss_mb
from tests.test_api_app import client_for, empty_db, seed_db  # noqa: F401  (shared fixtures)
from tests.test_api_static import _no_keys, dist, work_db  # noqa: F401

DEFAULT_CSP = f"frame-ancestors {DEFAULT_FRAME_ANCESTORS}"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("FRAME_ANCESTORS",):
        monkeypatch.delenv(k, raising=False)


def _client():
    return TestClient(create_app(start_background=False))


def _assert_headers(r, csp=DEFAULT_CSP):
    assert r.headers["content-security-policy"] == csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "x-frame-options" not in r.headers


def test_default_csp_on_every_kind_of_response(work_db, dist):
    with _client() as c:
        for url in ("/", "/api/health", "/_next/static/chunks/a.js", "/nope.html", "/api/nope", "/api/status"):
            _assert_headers(c.get(url))
        assert c.get("/nope.html").status_code == 404
        r = c.get("/api/ladder")  # 202 while the default ladder computes, else 200
        assert r.status_code in (200, 202, 503)
        _assert_headers(r)


def test_csp_on_202_and_503(client_for):
    from tests.test_api_app import Fake
    client, fake, _svc = client_for(Fake(gated=True))
    r = client.get("/api/ladder")
    assert r.status_code == 202
    _assert_headers(r)
    _assert_headers(client.get("/api/series?days=abc"))  # 422
    fake.gate.set()


def test_csp_on_503_not_ready(empty_db):
    with TestClient(create_app(start_background=False)) as c:
        r = c.get("/api/metrics")
        assert r.status_code == 503
        _assert_headers(r)


def test_frame_ancestors_override(work_db, dist, monkeypatch):
    monkeypatch.setenv("FRAME_ANCESTORS", "'self' http://127.0.0.1:8672")
    with _client() as c:
        _assert_headers(c.get("/"), "frame-ancestors 'self' http://127.0.0.1:8672")
        assert c.get("/api/status").json()["config_issues"] == []


@pytest.mark.parametrize("bad", ["'self'; script-src *", "'self'\nSet-Cookie: x=1", "https://a.example, https://b.example",
                                 "https://café.example", "'self' https://€.example", "'self' script-src *",
                                 "https://" + "a" * 2000 + ".example"])
def test_injection_attempt_falls_back_to_default(work_db, dist, monkeypatch, bad):
    monkeypatch.setenv("FRAME_ANCESTORS", bad)
    with _client() as c:
        _assert_headers(c.get("/"))
        assert c.get("/api/status").json()["config_issues"] == ["frame_ancestors_invalid"]


@pytest.mark.parametrize("path", ["/api/nope", "/api", "/api/", "/api/nope/deeper"])
def test_unknown_api_paths_are_json_404(work_db, dist, path):
    with _client() as c:
        r = c.get(path)
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert r.json() == {"detail": "Not found"}
    with _client() as c:
        assert c.get("/missing-page").status_code == 404
        assert "FAKE 404 PAGE" in c.get("/missing-page").text  # non-API 404 unchanged


def test_status_has_rss_number_or_null(work_db, dist):
    with _client() as c:
        v = c.get("/api/status").json()["process"]["rss_mb"]
        assert v is None or (isinstance(v, (int, float)) and v > 0)
    assert rss_mb() is None or rss_mb() > 0


def test_frame_ancestors_1000_chars_is_still_allowed(work_db, dist, monkeypatch):
    ok = "https://" + "a" * 980 + ".example"
    assert len(ok) <= 1000
    monkeypatch.setenv("FRAME_ANCESTORS", ok)
    with _client() as c:
        _assert_headers(c.get("/"), f"frame-ancestors {ok}")


@pytest.mark.parametrize("path", ["/api/health", "/_stcore/health", "/api/status"])
def test_head_matches_get_headers_without_body(work_db, dist, path):
    with _client() as c:
        g = c.get(path)
        h = c.head(path)
        assert h.status_code == 200 == g.status_code
        assert h.content == b""
        _assert_headers(h)
        assert h.headers["content-type"] == g.headers["content-type"]
        if path != "/api/status":  # the status body carries uptime, so only the stable ones match in length
            assert h.headers["content-length"] == g.headers["content-length"]
