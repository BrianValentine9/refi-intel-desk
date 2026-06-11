"""Thin client over the FRED ``series/observations`` endpoint.

Responsibilities are deliberately narrow: fetch raw observations for one series,
parse them into ``Observation`` records, and be polite to the API (rate limit +
retry with backoff). Persistence and orchestration live elsewhere.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

# Load .env once on import so FRED_API_KEY is available when running the CLI.
load_dotenv()

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_RETRY_STATUS = {429, 500, 502, 503, 504}
_POLITE_INTERVAL_SECONDS = 0.5  # keeps us at <= 2 requests/second
_REQUEST_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Observation:
    """A single dated value for a series. ``value`` is None when missing."""

    series_id: str
    obs_date: str  # ISO yyyy-mm-dd
    value: float | None


class FredApiKeyError(RuntimeError):
    """Raised when FRED_API_KEY is not configured."""


def _get_api_key() -> str:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise FredApiKeyError("Set FRED_API_KEY — see .env.example")
    return key


def _parse_observations(series_id: str, payload: dict) -> list[Observation]:
    """Turn a FRED JSON payload into Observation records.

    FRED encodes a missing value as the string ``"."`` — that becomes None
    (never 0), so a gap is never mistaken for a real reading.
    """
    observations: list[Observation] = []
    for obs in payload.get("observations", []):
        raw = obs.get("value")
        value = None if raw in (".", "", None) else float(raw)
        observations.append(Observation(series_id, obs["date"], value))
    return observations


def fetch_observations(series_id: str, start_date: str | None = None) -> list[Observation]:
    """Fetch observations for ``series_id``, optionally from ``start_date`` forward.

    Retries up to 3 times with exponential backoff on transient (429/5xx)
    errors, and sleeps briefly after a successful call to respect the rate limit.
    """
    params = {
        "series_id": series_id,
        "api_key": _get_api_key(),
        "file_type": "json",
    }
    if start_date:
        params["observation_start"] = start_date

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        response = requests.get(
            FRED_OBSERVATIONS_URL, params=params, timeout=_REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code == 200:
            parsed = _parse_observations(series_id, response.json())
            time.sleep(_POLITE_INTERVAL_SECONDS)
            return parsed
        if response.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
            time.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            continue
        response.raise_for_status()

    # Loop only exits via return or raise above; this satisfies type checkers.
    raise RuntimeError(f"Exhausted retries fetching {series_id}")
