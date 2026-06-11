"""Tests for the FRED client: parsing, the missing-value rule, retries, key check."""

from __future__ import annotations

import pytest

from src.data import fred_client
from src.data.fred_client import (
    FredApiKeyError,
    Observation,
    fetch_observations,
)
from tests.conftest import FakeResponse


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Never actually sleep during tests (backoff + politeness)."""
    monkeypatch.setattr(fred_client.time, "sleep", lambda *_: None)


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")


def test_parses_normal_and_missing_values(monkeypatch, api_key, sample_payload):
    monkeypatch.setattr(
        "requests.get", lambda *a, **k: FakeResponse(200, sample_payload)
    )
    obs = fetch_observations("DGS10")
    assert obs == [
        Observation("DGS10", "2026-01-02", 4.21),
        Observation("DGS10", "2026-01-03", None),  # "." -> None, not 0
        Observation("DGS10", "2026-01-06", 4.25),
    ]


def test_empty_observation_list(monkeypatch, api_key):
    monkeypatch.setattr(
        "requests.get", lambda *a, **k: FakeResponse(200, {"observations": []})
    )
    assert fetch_observations("DGS10") == []


def test_missing_value_is_none_never_zero(monkeypatch, api_key):
    payload = {"observations": [{"date": "2026-02-01", "value": "."}]}
    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse(200, payload))
    (obs,) = fetch_observations("DGS10")
    assert obs.value is None


def test_retries_then_succeeds_on_transient_error(monkeypatch, api_key, sample_payload):
    statuses = [503, 429, 200]

    def fake_get(*args, **kwargs):
        code = statuses.pop(0)
        return FakeResponse(code, sample_payload if code == 200 else None)

    monkeypatch.setattr("requests.get", fake_get)
    obs = fetch_observations("DGS10")
    assert len(obs) == 3
    assert statuses == []  # all three attempts consumed


def test_start_date_passed_as_observation_start(monkeypatch, api_key, sample_payload):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured.update(params or {})
        return FakeResponse(200, sample_payload)

    monkeypatch.setattr("requests.get", fake_get)
    fetch_observations("DGS10", start_date="2025-01-01")
    assert captured["observation_start"] == "2025-01-01"
    assert captured["file_type"] == "json"


def test_missing_api_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(FredApiKeyError, match="Set FRED_API_KEY"):
        fetch_observations("DGS10")
