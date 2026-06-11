"""Amortization and rounding tests."""

from __future__ import annotations

import pytest

from src.core.amort import monthly_pi, round_half_up


def test_known_amortization():
    # $200,000 @ 6.00% over 360 months = $1,199.10.
    assert monthly_pi(200_000, 6.0, 360) == 1199.10


def test_zero_rate_is_straight_line():
    assert monthly_pi(120_000, 0.0, 360) == 333.33  # 120000/360, rounded half-up


def test_round_half_up_rounds_up_on_tie():
    assert round_half_up(1.005) == 1.01
    assert round_half_up(2.675) == 2.68
    assert round_half_up(2.674) == 2.67


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        monthly_pi(100_000, 5.0, 0)
    with pytest.raises(ValueError):
        monthly_pi(-1, 5.0, 360)
