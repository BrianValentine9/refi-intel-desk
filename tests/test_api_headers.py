"""U5: frame-ancestors CSP on every response, JSON 404 under /api, staging probe, RSS field."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import DEFAULT_FRAME_ANCESTORS, create_app, rss_mb
from tests.test_api_app import client_for, empty_db, seed_db  # noqa: F401  (shared fixtures)
from tests.test_api_static import _no_keys, dist, work_db  # noqa: F401

DEFAULT_CSP = f"frame-ancestors {DEFAULT_FRAME_ANCESTORS}"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("FRAME_ANCESTORS", "RENDER_SERVICE_NAME", "HEADER_PROBE"):
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


@pytest.mark.parametrize("bad", ["'self'; script-src *", "'self'\nSet-Cookie: x=1", "https://a.example, https://b.example"])
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


def test_probe_off_by_default_and_on_live_name(work_db, dist, monkeypatch):
    monkeypatch.setenv("RENDER_SERVICE_NAME", "trigger-ladder")
    with _client() as c:
        r = c.get("/api/_probe/headers")
        assert r.status_code == 404 and r.json() == {"detail": "Not found"}


@pytest.mark.parametrize("env", [("RENDER_SERVICE_NAME", "trigger-ladder-staging"), ("HEADER_PROBE", "1")])
def test_probe_on_returns_only_listed_headers(work_db, dist, monkeypatch, env):
    monkeypatch.setenv(*env)
    with _client() as c:
        r = c.get("/api/_probe/headers", headers={
            "X-Forwarded-For": "1.2.3.4", "X-Forwarded-Proto": "https", "Cookie": "s=secret",
            "Authorization": "Bearer x", "X-Other": "no"})
        assert r.status_code == 200
        body = r.json()
        assert body["headers"] == {"x-forwarded-for": "1.2.3.4", "x-forwarded-proto": "https"}
        assert "client_host" in body
        _assert_headers(r)


def test_status_has_rss_number_or_null(work_db, dist):
    with _client() as c:
        v = c.get("/api/status").json()["process"]["rss_mb"]
        assert v is None or (isinstance(v, (int, float)) and v > 0)
    assert rss_mb() is None or rss_mb() > 0
