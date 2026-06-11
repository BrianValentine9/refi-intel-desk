"""Shared test fixtures and the fake HTTP response used to mock FRED."""

from __future__ import annotations

import pytest


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def sample_payload() -> dict:
    """A FRED observations payload with a normal value and a missing one."""
    return {
        "observations": [
            {"date": "2026-01-02", "value": "4.21"},
            {"date": "2026-01-03", "value": "."},  # missing -> None
            {"date": "2026-01-06", "value": "4.25"},
        ]
    }
