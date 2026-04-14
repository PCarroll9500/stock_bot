"""
tests/test_config_and_stop_loss.py

Tests for stop-loss/trailing-stop configuration, price calculation, and order type selection.
"""

import json
from pathlib import Path


# ── Config file sanity ────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parents[1] / "src" / "stock_bot" / "config" / "picker_config.json"


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


class TestConfigValues:
    """Verify picker_config.json has the expected values."""

    def test_trailing_stop_set(self):
        assert _config()["trailing_stop_pct"] == 2.5

    def test_stop_loss_not_set(self):
        """stop_loss_pct removed in favour of trailing_stop_pct."""
        assert _config().get("stop_loss_pct") is None

    def test_take_profit_null(self):
        assert _config()["take_profit_pct"] is None

    def test_num_stocks(self):
        assert _config()["num_stocks"] == 15

    def test_min_score(self):
        assert _config()["min_score"] == 7

    def test_score_floor(self):
        assert _config()["score_floor"] == 7

    def test_aggressive_min_score(self):
        assert _config()["aggressive_min_score"] == 8

    def test_min_expected_gain(self):
        assert _config()["min_expected_gain_pct"] == 1.5

    def test_limit_order_buffer(self):
        assert _config()["limit_order_buffer_pct"] == 5.0

    def test_fill_wait_seconds(self):
        assert _config()["fill_wait_seconds"] == 30

    def test_sell_wait_seconds(self):
        assert _config()["sell_wait_seconds"] == 45

    def test_realloc_fill_wait_seconds(self):
        assert _config()["realloc_fill_wait_seconds"] == 20

    def test_min_picks(self):
        assert _config()["min_picks"] == 3

    def test_multi_day_hold_min_score(self):
        assert _config()["multi_day_hold_min_score"] == 9

    def test_multi_day_max_days(self):
        assert _config()["multi_day_max_days"] == 3

    def test_required_keys_present(self):
        cfg = _config()
        required = [
            "aggressive_mode", "num_stocks", "min_score", "score_floor",
            "min_expected_gain_pct", "limit_order_buffer_pct", "trailing_stop_pct",
            "fill_wait_seconds", "sell_wait_seconds", "realloc_fill_wait_seconds",
            "min_picks", "multi_day_hold_min_score", "multi_day_max_days",
            "scanner", "news", "pre_score_trend_filters",
        ]
        for key in required:
            assert key in cfg, f"Missing key: {key}"

    def test_trailing_and_stop_loss_not_both_set(self):
        """Config must never set both trailing_stop_pct and stop_loss_pct."""
        cfg = _config()
        assert not (cfg.get("trailing_stop_pct") and cfg.get("stop_loss_pct")), \
            "trailing_stop_pct and stop_loss_pct cannot both be set"


# ── Trailing-stop price calculation ──────────────────────────────────────────

class TestTrailingStopCalc:
    """Replicate trailing stop trail amount formula from buy_stocks.py."""

    def _trail_amount(self, ref_price: float, trailing_stop_pct: float) -> float:
        """Trail amount = ref_price * pct/100, rounded to 2dp."""
        return round(ref_price * trailing_stop_pct / 100, 2)

    def test_2_5_pct_of_100(self):
        assert self._trail_amount(100.0, 2.5) == 2.5

    def test_2_5_pct_of_50(self):
        assert self._trail_amount(50.0, 2.5) == 1.25

    def test_trail_amount_positive(self):
        for price in [10.0, 50.0, 100.0, 500.0, 1500.0]:
            assert self._trail_amount(price, 2.5) > 0

    def test_trail_amount_two_decimals(self):
        result = self._trail_amount(33.33, 2.5)
        assert result == round(result, 2)


# ── Stop-loss price calculation (still used in tests / edge cases) ────────────

class TestStopLossPriceCalc:
    """Replicate buy_stocks.py stop-loss price formula (used when stop_loss_pct set)."""

    def _sl(self, ref_price: float, stop_loss_pct: float) -> float:
        return round(ref_price * (1 - stop_loss_pct / 100), 2)

    def test_2_5_pct_below_100(self):
        assert self._sl(100.0, 2.5) == 97.5

    def test_2_5_pct_below_17_24(self):
        assert self._sl(17.24, 2.5) == 16.81

    def test_stop_is_below_entry(self):
        for price in [10.0, 50.0, 100.0, 500.0, 1500.0]:
            assert self._sl(price, 2.5) < price

    def test_stop_price_two_decimals(self):
        result = self._sl(33.33, 2.5)
        assert result == round(result, 2)


# ── Order type detection ──────────────────────────────────────────────────────

class TestOrderTypeSelection:
    """Verify correct order type is chosen based on config combination."""

    def _order_type(self, stop_loss_pct, take_profit_pct, trailing_stop_pct=None):
        if trailing_stop_pct is not None:
            return "trailing"
        has_bracket = stop_loss_pct is not None and take_profit_pct is not None
        if has_bracket:
            return "bracket"
        elif stop_loss_pct is not None:
            return "stop_only"
        elif take_profit_pct is not None:
            return "tp_only"
        else:
            return "market"

    def test_current_config_is_trailing(self):
        cfg = _config()
        assert self._order_type(
            cfg.get("stop_loss_pct"), cfg.get("take_profit_pct"), cfg.get("trailing_stop_pct")
        ) == "trailing"

    def test_both_set_is_bracket(self):
        assert self._order_type(2.5, 5.0) == "bracket"

    def test_neither_set_is_market(self):
        assert self._order_type(None, None) == "market"

    def test_tp_only(self):
        assert self._order_type(None, 5.0) == "tp_only"

    def test_stop_only(self):
        assert self._order_type(2.5, None) == "stop_only"
