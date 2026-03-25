"""
tests/test_config_and_stop_loss.py

Tests for stop-loss configuration, price calculation, and order type selection.
"""

import json
from pathlib import Path


# ── Config file sanity ────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parents[1] / "src" / "stock_bot" / "config" / "picker_config.json"


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


class TestConfigValues:
    """Verify picker_config.json has the expected values for tomorrow."""

    def test_stop_loss_set(self):
        assert _config()["stop_loss_pct"] == 2.5

    def test_take_profit_null(self):
        assert _config()["take_profit_pct"] is None

    def test_num_stocks(self):
        assert _config()["num_stocks"] == 10

    def test_min_score(self):
        assert _config()["min_score"] == 7

    def test_score_floor(self):
        assert _config()["score_floor"] == 7

    def test_aggressive_min_score(self):
        assert _config()["aggressive_min_score"] == 7

    def test_min_expected_gain(self):
        assert _config()["min_expected_gain_pct"] == 2.5

    def test_limit_order_buffer(self):
        assert _config()["limit_order_buffer_pct"] == 1.0

    def test_fill_wait_seconds(self):
        assert _config()["fill_wait_seconds"] == 60

    def test_sell_wait_seconds(self):
        assert _config()["sell_wait_seconds"] == 45

    def test_realloc_fill_wait_seconds(self):
        assert _config()["realloc_fill_wait_seconds"] == 20

    def test_required_keys_present(self):
        cfg = _config()
        required = [
            "aggressive_mode", "num_stocks", "min_score", "score_floor",
            "min_expected_gain_pct", "limit_order_buffer_pct", "stop_loss_pct",
            "fill_wait_seconds", "sell_wait_seconds", "realloc_fill_wait_seconds",
            "scanner", "news", "pre_score_trend_filters",
        ]
        for key in required:
            assert key in cfg, f"Missing key: {key}"


# ── Stop-loss price calculation ───────────────────────────────────────────────

class TestStopLossPriceCalc:
    """Replicate buy_stocks.py stop-loss price formula."""

    def _sl(self, ref_price: float, stop_loss_pct: float) -> float:
        return round(ref_price * (1 - stop_loss_pct / 100), 2)

    def test_2_5_pct_below_100(self):
        assert self._sl(100.0, 2.5) == 97.5

    def test_2_5_pct_below_17_24(self):
        # VG from real session — stop at $16.81
        assert self._sl(17.24, 2.5) == 16.81

    def test_stop_is_below_entry(self):
        for price in [10.0, 50.0, 100.0, 500.0, 1500.0]:
            sl = self._sl(price, 2.5)
            assert sl < price

    def test_stop_uses_limit_price_as_ref_when_limit_set(self):
        """When a limit order is used, the stop-loss reference is the limit price,
        not the live market price. This gives a tighter real-money loss bound."""
        limit_price = 101.0  # 1% buffer above $100 preflight
        sl = self._sl(limit_price, 2.5)
        assert sl == round(101.0 * 0.975, 2)

    def test_stop_price_two_decimals(self):
        """IBKR requires auxPrice to be rounded to 2 decimal places."""
        import math
        result = self._sl(33.33, 2.5)
        assert result == round(result, 2)
        assert isinstance(result, float)


# ── Stop-loss vs bracket detection ───────────────────────────────────────────

class TestOrderTypeSelection:
    """Verify correct order type is chosen based on config combination."""

    def _order_type(self, stop_loss_pct, take_profit_pct):
        """Replicate the if/elif logic in buy_stocks.py."""
        has_bracket = stop_loss_pct is not None and take_profit_pct is not None
        if has_bracket:
            return "bracket"
        elif stop_loss_pct is not None:
            return "stop_only"
        elif take_profit_pct is not None:
            return "tp_only"
        else:
            return "market"

    def test_current_config_is_stop_only(self):
        cfg = _config()
        assert self._order_type(cfg["stop_loss_pct"], cfg["take_profit_pct"]) == "stop_only"

    def test_both_set_is_bracket(self):
        assert self._order_type(2.5, 5.0) == "bracket"

    def test_neither_set_is_market(self):
        assert self._order_type(None, None) == "market"

    def test_tp_only(self):
        assert self._order_type(None, 5.0) == "tp_only"
