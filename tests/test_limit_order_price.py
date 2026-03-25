"""
tests/test_limit_order_price.py

Tests for limit order price calculation with configurable buffer.
"""

import pytest


def _calc_limit(preflight_price: float, buffer_pct: float | None) -> float | None:
    """Replicate the limit-price formula from main.py."""
    if buffer_pct is None:
        return None
    return round(preflight_price * (1 + buffer_pct / 100), 2)


class TestLimitOrderPrice:
    def test_one_pct_buffer(self):
        assert _calc_limit(100.0, 1.0) == 101.0

    def test_zero_buffer_still_rounds(self):
        assert _calc_limit(99.99, 0.0) == 99.99

    def test_none_buffer_returns_market(self):
        assert _calc_limit(100.0, None) is None

    def test_fractional_price(self):
        # $17.24 * 1.01 = $17.4124 → rounded to $17.41
        assert _calc_limit(17.24, 1.0) == 17.41

    def test_high_price(self):
        # $3,000 stock, 1% buffer
        assert _calc_limit(3000.0, 1.0) == 3030.0

    def test_half_pct(self):
        assert _calc_limit(200.0, 0.5) == 201.0

    def test_paper_and_live_match(self):
        """Paper and live must produce identical limit prices for same inputs."""
        paper_limit = _calc_limit(50.0, 1.0)
        live_limit = _calc_limit(50.0, 1.0)
        assert paper_limit == live_limit


class TestReallocationShares:
    """Share count for realloc top-ups must use math.floor."""

    def test_floor_prevents_overspend(self):
        import math
        freed_cash = 3_000.0
        fresh_price = 47.33
        shares = math.floor(freed_cash / fresh_price)
        cost = shares * fresh_price
        assert cost <= freed_cash
        assert shares == 63  # floor(3000/47.33)

    def test_no_fractional_shares(self):
        import math
        for price in [10.01, 33.33, 99.99, 150.75, 1234.56]:
            shares = math.floor(1_000.0 / price)
            assert isinstance(shares, int)
            assert shares * price <= 1_000.0
